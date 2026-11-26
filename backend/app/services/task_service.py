"""
服务层 - 测试任务管理
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, delete
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime, timezone
from app.models.models import (
    TestTask, TaskStep, TaskIssue, TaskAction,
    TaskStatus, StepStatus, IssueStatus,
    User, Project, Product, TaskCaseLink, ProjectMember,
    TaskVersionConfig, TaskVersionItem,
)
from app.schemas.schemas import (
    TaskCreate, TaskUpdate, TaskStepUpdate, IssueCreate, IssueUpdate,
    IssueResponse, IssueTreeResponse, IssueTreeNode, IssueTreeTask,
    IssueTreeProject, IssueTreeProduct
)
from app.core.security import _user_roles
from app.core.step_perms import can_operate_step, deny_detail


def _now() -> datetime:
    """统一取 UTC 时间。

    容器与数据库时区均为 UTC，历史代码用 naive `datetime.now()` 写入 timestamptz
    也是按 UTC 落库，这里显式带 tzinfo 只是为了能安全地序列化进 JSON（rounds），
    落库瞬间值不变。
    """
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _append_round(rounds, start: datetime) -> list:
    """开启新一轮：环节每被打回后重新点「开始」，就多一轮记录"""
    lst = list(rounds) if isinstance(rounds, list) else []
    lst.append({"n": len(lst) + 1, "start": _iso(start), "end": None, "outcome": "in_progress"})
    return lst


def _close_round(rounds, end: datetime, outcome: str, fallback_start=None) -> list:
    """收口当前轮：completed=本轮提交完成 / rejected=本轮结果被打回。

    已有 end 的轮次不覆盖 end（例如环节8先完成、随后被环节9打回，
    完成时刻要保留原值，只把结局从 completed 改成 rejected）。
    rounds 为空但 begin_date 有值（历史存量数据）时补记第一轮，避免老任务看不到第一轮。
    """
    lst = list(rounds) if isinstance(rounds, list) else []
    if lst:
        last = dict(lst[-1])
        if not last.get("end"):
            last["end"] = _iso(end)
        last["outcome"] = outcome
        lst[-1] = last
    elif fallback_start is not None:
        lst.append({"n": 1, "start": _iso(fallback_start), "end": _iso(end), "outcome": outcome})
    return lst


# 流程步骤定义（9步；原第10步"任务结束"已裁撤——2026-09-23 用户定夺"感觉没什么用"，
# 数据审核(9)双审通过即任务完结(done)。存量任务的 step10 行由一次性迁移清除。）
FLOW_STEPS = [
    {"step": 1, "name": "任务发起", "type": "create", "roles": ["pl"]},
    {"step": 2, "name": "测试设计", "type": "design", "roles": ["tse"]},
    {"step": 3, "name": "版本配套", "type": "confirm", "roles": ["version"]},
    {"step": 4, "name": "环境确认", "type": "execute", "roles": ["version"]},
    {"step": 5, "name": "环境审核", "type": "review", "roles": ["pl"]},
    {"step": 6, "name": "测试用例分配", "type": "assign", "roles": ["version"]},
    {"step": 7, "name": "执行测试", "type": "execute", "roles": ["executor"]},  # 合并原"问题记录"功能
    {"step": 8, "name": "测试完成", "type": "execute", "roles": ["executor"]},
    {"step": 9, "name": "数据审核", "type": "review", "roles": ["version", "tse"]},
]

# 改派级联映射（环节号 → 任务级角色字段, 中文名）：与建任务初始化 owner_map 同源。
# 3/4/6/8 ← version_owner；7 ← executor_id；5 ← owner(PL)。
# 流程含义：环境审核(5)只有PL有权；7是任务主执行人(executor_id)；8测试完成归版本负责人汇总。
CASCADE_FIELD = {
    2: ("tse_id", "TSE"),
    3: ("version_owner", "版本负责人"),
    4: ("version_owner", "版本负责人"),
    6: ("version_owner", "版本负责人"),
    8: ("version_owner", "版本负责人"),
    7: ("executor_id", "测试执行人"),
    5: ("owner", "PL负责人"),
}
# 说明:原10"任务结束"的键随环节裁撤移除(其归宿是任务级 status=done,非数据行)。
# 2026-11-26：环节2 纳入改派（TSE 填错/离职时原先没有任何入口能救，只能改库）。

# 环节家族 ↔ 项目角色(project_role)：改派候选人资格的唯一口径（2026-09-22 需求1/2）。
# 与 CASCADE_FIELD 家族一一同源——改出去的人必然同时合法占据对应"任务级角色字段"，
# 环节资格与级联同步永不打架。
FAMILY_PROJECT_ROLE = {
    "version_owner": ("version_owner", "版本负责人"),
    "executor_id": ("executor", "测试执行人"),
    "owner": ("pl", "PL（项目负责人）"),
    "tse_id": ("tse", "TSE（测试设计）"),
}

# 任务级必填字段 ↔ 受其直接驱动的环节（2026-11-26）
# 用途：① update_task 拒绝把这些字段清空（否则 TaskCreate 的必填被 PUT 绕过）
#      ② 字段变更时只重铺它自己负责的环节，不做"全量重铺"（避免覆盖手动改派的结果）
FIELD_STEPS = {
    "owner": (1, 5),
    "tse_id": (2,),
    "version_owner": (3, 4, 6, 8),
    "executor_id": (7,),
}
REQUIRED_TASK_FIELDS = (
    "name", "product_name", "project_id", "begin_date", "end_date",
    "owner", "tse_id", "version_owner", "executor_id",
)


class TaskService:
    """测试任务服务"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def generate_code(self) -> str:
        """生成任务编号"""
        year = datetime.now().strftime("%Y")
        month = datetime.now().strftime("%m")
        prefix = f"TST-{year}{month}-"
        
        # 查询当月最大编号
        stmt = select(func.max(TestTask.code)).where(
            TestTask.code.like(f"{prefix}%")
        )
        result = await self.db.execute(stmt)
        max_code = result.scalar()
        
        if max_code:
            seq = int(max_code[-4:]) + 1
        else:
            seq = 1
        
        return f"{prefix}{seq:04d}"
    
    async def create_task(self, data: TaskCreate, user_id: int) -> TestTask:
        """创建任务"""
        task = TestTask(
            name=data.name,
            zentao_product_id=data.zentao_product_id,
            product_name=data.product_name,
            code=await self.generate_code(),
            project_id=data.project_id,
            version=data.version,
            test_type=data.test_type,
            begin_date=data.begin_date,
            end_date=data.end_date,
            owner=data.owner,
            tse_id=data.tse_id,
            version_owner=data.version_owner,
            executor_id=data.executor_id,
            executors=data.executors,
            description=data.description,
            created_by=user_id,
        )
        self.db.add(task)
        await self.db.flush()
        
        # 初始化步骤
        await self._init_steps(task.id, data)

        # 自动完成步骤1（任务发起），因为创建任务时已填写了全部准出条件
        await self._auto_complete_step1(task.id, user_id)

        # 记录日志
        await self._log_action(task.id, "created", user_id, f"创建测试任务: {task.name}")
        
        await self.db.commit()
        await self.db.refresh(task)
        return task
    
    async def _auto_complete_step1(self, task_id: int, user_id: int):
        """创建任务后自动完成步骤1，推进到步骤2"""
        stmt = select(TaskStep).where(
            TaskStep.task_id == task_id, TaskStep.step == 1
        )
        result = await self.db.execute(stmt)
        step1 = result.scalar_one_or_none()
        if step1:
            _t = _now()
            step1.status = StepStatus.completed.value
            step1.begin_date = _t
            step1.end_date = _t
            # 自动完成也留一轮记录，保持时间线完整（否则第1步永远没有轮次）
            step1.rounds = [{"n": 1, "start": _iso(_t), "end": _iso(_t), "outcome": "completed"}]
            step1.remark = "任务创建时自动完成"

        # 更新任务 current_step 到2
        task_stmt = select(TestTask).where(TestTask.id == task_id)
        task_result = await self.db.execute(task_stmt)
        task = task_result.scalar_one_or_none()
        if task:
            task.current_step = 2
            task.status = TaskStatus.design.value  # 进入测试设计阶段

        await self._log_action(task_id, "step_updated", user_id, "步骤1任务发起自动完成，进入测试设计")

    async def _init_steps(self, task_id: int, task_data: TaskCreate):
        """初始化任务步骤（9步流程，见 FLOW_STEPS）"""
        owner_map = {
            1: task_data.owner,
            2: task_data.tse_id,
            3: task_data.version_owner,
            4: task_data.version_owner,   # 环境确认
            5: task_data.owner,            # 环境审核
            6: task_data.version_owner,   # 测试用例分配
            7: task_data.executor_id,     # 执行测试（合并原"问题记录"功能）
            8: task_data.version_owner,   # 测试完成（报告整理/log链接/自动化结果）—— 归版本负责人
            9: None,                       # 数据审核（PL + TSE 双审核，assigned_to=None 让两人都能操作；末环，双通过即任务完结）
        }
        
        for step_info in FLOW_STEPS:
            step = TaskStep(
                task_id=task_id,
                step=step_info["step"],
                step_name=step_info["name"],
                step_type=step_info["type"],
                assigned_to=owner_map.get(step_info["step"]),
                status=StepStatus.pending.value,
            )
            self.db.add(step)
        await self.db.flush()  # 刷新到数据库，确保后续查询能查到步骤

    async def get_tasks(
        self, 
        status: Optional[str] = None, 
        owner: Optional[int] = None,
        limit: int = 100,
        offset: int = 0
    ) -> tuple[List[TestTask], int]:
        """获取任务列表"""
        stmt = select(TestTask).options(
            selectinload(TestTask.steps)
        ).where(TestTask.deleted == False)
        
        if status:
            stmt = stmt.where(TestTask.status == status)
        if owner:
            stmt = stmt.where(
                (TestTask.owner == owner) |
                (TestTask.tse_id == owner) |
                (TestTask.version_owner == owner) |
                (TestTask.executor_id == owner)
            )
        
        # 总数
        count_stmt = select(func.count(TestTask.id)).where(TestTask.deleted == False)
        if status:
            count_stmt = count_stmt.where(TestTask.status == status)
        if owner:
            count_stmt = count_stmt.where(
                (TestTask.owner == owner) | 
                (TestTask.tse_id == owner) | 
                (TestTask.version_owner == owner)
            )
        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar()
        
        # 分页
        stmt = stmt.order_by(TestTask.id.desc()).limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        tasks = result.scalars().all()
        
        # 批量补充 project_name（避免前端二次查询）
        project_ids = {t.project_id for t in tasks if t.project_id}
        if project_ids:
            proj_res = await self.db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids)))
            project_name_map = {row[0]: row[1] for row in proj_res.all()}
            for t in tasks:
                t.project_name = project_name_map.get(t.project_id)
        
        return list(tasks), total
    
    async def get_task(self, task_id: int) -> Optional[TestTask]:
        """获取单个任务"""
        stmt = select(TestTask).options(
            selectinload(TestTask.steps),
            selectinload(TestTask.issues),
            selectinload(TestTask.actions)
        ).where(TestTask.id == task_id)
        result = await self.db.execute(stmt)
        task = result.scalar_one_or_none()
        if task and task.project_id:
            proj = (await self.db.execute(select(Project.name).where(Project.id == task.project_id))).scalar_one_or_none()
            task.project_name = proj
        return task
    
    async def update_task(
        self, task_id: int, data: TaskUpdate, actor_id: Optional[int] = None
    ) -> Optional[TestTask]:
        """更新任务

        2026-11-26 两条硬约束（原实现直接 setattr，导致 TaskCreate 的必填形同虚设）：
          ① 必填字段不允许被清空——传 null 直接拒绝。前端目前没有"编辑任务"入口，
             但「执行人管理」等场景走的就是这个 PUT，接口直调更可以绕过建任务校验。
          ② 角色字段（owner/tse_id/version_owner/executor_id）变更后，把它驱动的
             环节负责人一并重铺；只重铺该字段负责的环节，且跳过已完成环节，
             避免覆盖"手动改派"的结果与历史（FIELD_STEPS 见模块顶部）。
        """
        task = await self.get_task(task_id)
        if not task:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # ① 必填字段禁止清空
        cleared = [f for f in REQUIRED_TASK_FIELDS if f in update_data and update_data[f] is None]
        if cleared:
            zh = {
                "name": "任务名称", "product_name": "待测产品", "project_id": "所属项目",
                "begin_date": "开始日期", "end_date": "结束日期", "owner": "PL 负责人",
                "tse_id": "TSE", "version_owner": "版本负责人", "executor_id": "测试执行人",
            }
            raise ValueError("以下字段为必填，不允许清空：" + "、".join(zh.get(f, f) for f in cleared))

        # 角色字段：目标用户必须存在且未停用（停用用户登录不了，填了等于没人能操作）
        for f, zname in (("owner", "PL 负责人"), ("tse_id", "TSE"),
                         ("version_owner", "版本负责人"), ("executor_id", "测试执行人")):
            uid = update_data.get(f)
            if uid is None:
                continue
            u = (await self.db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
            if not u:
                raise ValueError(f"{zname}指定的用户（ID:{uid}）不存在")
            if u.is_active is False:
                raise ValueError(f"{zname}指定的用户 {u.realname or u.account} 已被停用，不能设为负责人")

        step_notes = []
        for key, value in update_data.items():
            old = getattr(task, key, None)
            setattr(task, key, value)
            if key not in FIELD_STEPS or value == old:
                continue
            # ② 只重铺该字段负责的环节，已完成环节不动（历史只读）
            for s in (task.steps or []):
                if s.step not in FIELD_STEPS[key]:
                    continue
                if s.status == StepStatus.completed.value:
                    continue
                if s.assigned_to != value:
                    s.assigned_to = value
                    step_notes.append(f"环节{s.step}「{s.step_name}」")

        if step_notes and actor_id is not None:
            await self._log_action(
                task_id, "updated", actor_id,
                f"任务信息更新，负责人同步：{'、'.join(step_notes)}"
            )

        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def delete_task(self, task_id: int, user_id: int) -> bool:
        """软删除任务（标记deleted=True），同时记录操作日志"""
        task = await self.get_task(task_id)
        if not task:
            return False
        task.deleted = True
        await self._log_action(task_id, "deleted", user_id, f"删除测试任务: {task.name}")
        await self.db.commit()
        return True

    async def update_step(
        self, 
        step_id: int, 
        data: TaskStepUpdate, 
        user_id: int,
        user_roles: Optional[List[str]] = None
    ) -> Optional[TaskStep]:
        """更新步骤状态

        user_roles 用于「该环节没指定负责人」时按角色兜底（见 app.core.step_perms）。
        """
        stmt = select(TaskStep).where(TaskStep.id == step_id)
        result = await self.db.execute(stmt)
        step = result.scalar_one_or_none()
        
        if not step:
            return None

        # 权限校验：统一走 app.core.step_perms.can_operate_step（与附件、用例等接口同一口径）
        #   ① step.assigned_to == 自己
        #   ② 环节9 数据审核：仅任务的 PL(owner) 与 TSE —— assigned_to 为 NULL 是"双人共审"，
        #      不是"全员可操作"（2026-11-25 用户报"版本负责人能替 PL/TSE 审核"的根因）
        #   ③ 环节7 执行测试：executor_id + executors 列表全员
        #   ④ 其他环节 assigned_to 为 NULL（=建任务时没指定负责人）：仅 PL 兜底
        # admin 不再自动获得业务环节操作权；如需介入业务环节，需先把自己指派为该环节负责人
        _perm_task = (await self.db.execute(
            select(TestTask).where(TestTask.id == step.task_id)
        )).scalar_one_or_none()
        if not can_operate_step(step, _perm_task, user_id, user_roles):
            raise PermissionError(deny_detail(step))

        # 保存准出条件数据（单独更新或随状态一起）
        if data.ext_data is not None:
            merged = dict(step.ext_data or {})
            merged.update(data.ext_data)
            step.ext_data = merged

        if data.status:
            # 必须先点「开始本环节」：实际开始时间是耗时统计的唯一来源，不允许跳过直接完成，
            # 否则 begin_date 为空，后续所有步骤耗时/滞留分析都缺这一条。
            if data.status == "completed" and step.begin_date is None:
                raise ValueError(f"请先点「▶ 开始本环节」记录环节{step.step}「{step.step_name}」的实际开始时间，再提交完成")

            # 第三阶段硬校验：所有适用配套项必须已勾选，防止绕过前端直接完成
            if data.status == "completed" and step.step == 3:
                vc = (await self.db.execute(
                    select(TaskVersionConfig).where(TaskVersionConfig.task_id == step.task_id)
                )).scalar_one_or_none()
                if not vc:
                    raise ValueError("请先选择并核对版本配套表，再完成本环节")
                vc_items = (await self.db.execute(
                    select(TaskVersionItem).where(TaskVersionItem.task_config_id == vc.id)
                )).scalars().all()
                applicable = [
                    i for i in vc_items
                    if i.is_applicable and (i.required_value or "").strip() not in ("", "/", "N/A", "n/a", "-")
                ]
                if not applicable:
                    raise ValueError("版本配套表没有可核对条目，请先维护配套表")
                pending = [i for i in applicable if not i.is_checked]
                if pending:
                    raise ValueError(f"版本配套表还有 {len(pending)} 项未核对，请全部勾选后再完成本环节")

            step.status = data.status

            if data.status == "in_progress" and step.begin_date is None:
                # 「开始本环节」：只记一次实际开始时间，重复点不覆盖；
                # 同时开启新一轮记录（第一次是第1轮，被打回后重做就是第2轮、第3轮…）
                step.begin_date = _now()
                step.rounds = _append_round(step.rounds, step.begin_date)
                # 环节9 防御性清理：审核结论是"本轮裁决"，新一轮开始时绝不能带着上一轮的结论。
                # _reset_step 已在打回时清过，这里兜住历史残留与其他重开路径；
                # 只在本次请求没带 ext_data 时清，避免把调用方刚提交的结论误删。
                if step.step == 9 and data.ext_data is None and isinstance(step.ext_data, dict) and (
                        step.ext_data.get("pl_review") or step.ext_data.get("tse_review")):
                    _cleaned = dict(step.ext_data)
                    _cleaned.pop("pl_review", None)
                    _cleaned.pop("tse_review", None)
                    step.ext_data = _cleaned
            if data.status == "completed":
                # 走到这里 begin_date 一定非空（上面已硬校验），直接记完成时间
                step.end_date = _now()
                step.rounds = _close_round(step.rounds, step.end_date, "completed",
                                           fallback_start=step.begin_date)
                if data.remark is not None:
                    step.remark = data.remark
            if data.status == "rejected":
                if data.remark is not None:
                    step.remark = data.remark
        elif data.remark is not None:
            step.remark = data.remark

        # 更新任务进度
        # 业务规则：环节5(环境审核)打回 → 环节4(环境确认)退回重做 + 环节5自己也要重新审核
        if step.step == 5 and data.status == "rejected":
            await self._reset_step(step.task_id, 4)
            await self._reset_step(step.task_id, 5)

        # 业务规则：环节9(数据审核)不通过需补测 → 只退回环节8(测试完成)，
        # 由版本负责人组织补测后重新提交，再回到环节9审核
        if step.step == 9 and data.status == "rejected":
            task_stmt = select(TestTask).where(TestTask.id == step.task_id)
            task_result = await self.db.execute(task_stmt)
            task = task_result.scalar_one_or_none()
            # 打回前先把本轮审核结论归档进操作历史——_reset_step 会清空 ext_data 里的
            # pl_review/tse_review，不归档就再也查不到"这次是谁判了需补测、什么理由"
            _ext = step.ext_data or {}
            _pl = _ext.get("pl_review") or {}
            _tse = _ext.get("tse_review") or {}
            if _pl or _tse:
                def _rv(r: dict) -> str:
                    if not r.get("result"):
                        return "未选"
                    txt = "通过" if r.get("result") == "pass" else "需补测"
                    rm = (r.get("remark") or "").strip()
                    return f"{txt}（{rm}）" if rm else txt
                await self._log_action(
                    step.task_id, "step_9_review_archived", user_id,
                    f"环节9打回，本轮审核结论归档：PL={_rv(_pl)}；TSE={_rv(_tse)}"
                )
            # 只重置环节8（测试完成），环节9自己也重置让审核人重新审核
            await self._reset_step(step.task_id, 8)
            await self._reset_step(step.task_id, 9)

        await self._update_task_progress(step.task_id)

        # 记录日志
        if data.status == "in_progress":
            action_desc = f"开始环节{step.step}「{step.step_name}」，记录实际开始时间"
        elif data.status == "completed":
            action_desc = f"完成环节{step.step}「{step.step_name}」，记录实际完成时间"
        elif data.status == "rejected":
            action_desc = f"打回环节{step.step}「{step.step_name}」"
        elif data.status:
            action_desc = f"步骤{step.step}状态更新为: {data.status}"
        else:
            action_desc = f"步骤{step.step}准出条件数据更新"
        await self._log_action(
            step.task_id,
            "step_updated",
            user_id,
            action_desc
        )

        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def rollback_step(
        self,
        task_id: int,
        step_id: int,
        reason: str,
        user_id: int,
        user_roles: Optional[List[str]] = None,
    ) -> TaskStep:
        """当前环节回退至上一环节（修订模式：业务数据保留，只动流程状态）

        回退后两个环节**都回到「尚未开始」**（2026-11-26）：当前环节本轮计时收口、上一环节上一轮
        计时收口，状态均为 pending；上一环节要等负责人重新点「开始本环节」才开始新一轮计时。

        权限：统一走 step_perms.can_operate_step——环节负责人本人（环节7 含 executors 全体执行人）；
        负责人为 NULL 时仅任务 PL 或该环节对应角色可兜底；PL/管理员不自动获得他人环节的回退权
        约束：只能回退当前环节；步骤9 有专属的"需补测打回"规则（双审核，亦即末环），不走本通道
        （步骤5 已并入手动回退——通过与否以"提交到下一环节/返回上一环节"表达，审核意见记入处理说明；原"步骤10终态"已随该环节裁撤失效）
        原因必填，落两处（全流程可追溯）：
          1) 目标环节 remark 就地标注，2) 操作历史(action=step_rollback)
        """
        if not (reason or "").strip():
            raise ValueError("回退原因必填")

        task = await self.get_task(task_id)
        if not task:
            raise ValueError("任务不存在")

        stmt = select(TaskStep).where(TaskStep.id == step_id)
        step = (await self.db.execute(stmt)).scalar_one_or_none()
        if not step or step.task_id != task_id:
            raise ValueError("环节不存在或不属于该任务")

        # 权限：统一走 app.core.step_perms.can_operate_step（与 update_step、附件、用例同一口径）。
        # 2026-11-26 前这里各写一份 `assigned_to == 本人 or assigned_to is None`，
        # 等于"环节没指定负责人时任何人都能把流程退回去"。
        #   · 环节7：主执行人 + executors 全体；其余环节：assigned_to 本人
        #   · assigned_to 为 NULL：任务 PL 或具备该环节角色的人兜底（不是全员）
        if not can_operate_step(step, task, user_id, user_roles):
            raise PermissionError(deny_detail(step))
        if step.step != task.current_step:
            raise ValueError("只能回退当前环节")
        if step.step == 9:
            raise ValueError("步骤9请使用既有的『需补测』打回流程（数据审核双人复核）")
        if step.step <= 2:
            raise ValueError("当前环节没有可回退的上一步")

        prev_step_no = step.step - 1
        prev = (await self.db.execute(
            select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step == prev_step_no)
        )).scalar_one_or_none()
        if not prev:
            raise ValueError("上一环节不存在")

        # 回退核心（2026-11-26 用户定夺）：当前环节与上一环节**都回到「尚未开始」(pending)。
        #   · 当前环节(step)：本轮到此结束——收口本轮计时，状态重置为尚未开始；
        #   · 上一环节(prev)：上一轮同样收口，状态重置为尚未开始，由负责人重新点「开始」才计时。
        # 两边都**不**在回退瞬间自动开始新一轮：自动开始会让耗时把"等人来重做"的空档也算进去，
        # 且上一环节直接变"进行中"、当前环节却还挂着一条没结束的计时，两边状态自相矛盾。
        # 本轮结局统一记 rolled_back（轮次起止保留在 rounds 里，审计不丢）。
        await self._reset_step(task_id, prev.step, reason="rolled_back")
        await self._reset_step(task_id, step.step, reason="rolled_back")
        marker = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')} 回退自步骤{step.step}] {reason.strip()}"
        prev.remark = f"{prev.remark} ｜ {marker}" if prev.remark else marker

        # 进度/任务状态统一重算：两环都是 pending，_update_task_progress 会把 current_step
        # 修正为上一环节（最早的未完成环节），并把任务 status 映射回对应阶段
        await self._update_task_progress(task_id)

        await self._log_action(
            task_id, "step_rollback", user_id,
            f"步骤{step.step}「{step.step_name}」回退至步骤{prev.step}「{prev.step_name}」。原因：{reason.strip()}"
        )

        await self.db.commit()
        await self.db.refresh(prev)
        return prev

    async def change_step_assignee(self, task_id: int, step_id: int, new_uid: int, actor: User) -> TaskStep:
        """PL改派环节责任人（环节2-8；2026-09-22 需求，2026-11-26 纳入环节2）

        全链一致的保证方式 = 尊重"单一事实来源"：环节操作权/我的任务/展示文案
        全部由 task_steps.assigned_to 派生，改这一行 + 操作历史留痕即为全量同步；
        同时**级联回写**对应的任务级角色字段（2→tse_id、3/4/6/8→version_owner、
        7→executor_id、5→owner，与建任务初始化 owner_map 一一对应）——详情页头部的
        「TSE/版本负责人/测试执行人/PL」横幅读的是任务级字段，不回写会两层撕裂
        （2026-09-22 用户反馈"改派后标题栏没变化"）。注意：任务字段→步骤方向的
        级联重铺在 update_task 里做（只重铺该字段负责的环节，跳过已完成环节）。
        开放范围：2-8；1（任务发起，创建即自动完成）与 9（双人共审 owner∪tse，
        见 list_tasks pending 特判与前端对应特判）不开放。
        已完成(completed)环节不做改派；rejected(被打回待处理)保留可改派
        (打回即换人重来的场景)。审计动作 step_reassign 记新旧双名+级联说明。
        候选资格（2026-09-22 需求1/2）：目标责任人须持有该环节所属家族的项目角色
        （3/4/6/8→version_owner、7→executor、5→pl，环境审核因此仅PL）；
        任务未挂项目或项目该角色无在编成员时放行兜底。
        """
        task = await self.get_task(task_id)
        if not task:
            raise ValueError("任务不存在")

        # 权限：PL(任务owner) 或 admin(与 update_task 同判法: 'admin' in _user_roles)
        actor_roles = _user_roles(actor)
        if not ('admin' in actor_roles or task.owner == actor.id):
            raise PermissionError("仅PL(任务负责人)或管理员可改派环节责任人")

        stmt = select(TaskStep).where(TaskStep.id == step_id)
        step = (await self.db.execute(stmt)).scalar_one_or_none()
        if not step or step.task_id != task_id:
            raise ValueError("环节不存在或不属于该任务")
        # 2026-11-26：环节2 已开放（TSE 填错/离职时原先无任何入口可救，只能改库）；
        # 1 为任务发起（创建即自动完成）、9 为 PL+TSE 双人共审，仍不开放。
        if step.step in (1, 9):
            raise ValueError("环节1（任务发起，创建即自动完成）与环节9（PL+TSE 双人共审）不开放改派")
        # 已完成环节不做改派（2026-09-22 用户约定：都完成了还改什么）；
        # rejected（被打回待处理）保留可改派——打回本就是"换人重来"的场景
        if step.status == StepStatus.completed.value:
            raise ValueError(f"环节{step.step}「{step.step_name}」已完成，无需改派")

        new_user = (await self.db.execute(select(User).where(User.id == new_uid))).scalar_one_or_none()
        if not new_user:
            raise ValueError("目标责任人不存在")
        if new_user.is_active is False:
            raise ValueError(f"目标责任人 {new_user.realname or new_user.account} 已被禁用")
        if step.assigned_to == new_uid:
            raise ValueError("所选用户已经是该环节的责任人")

        # 改派资格（2026-09-22 需求："改派只展示/允许有当前环节权限的人"+“环境审核只有PL”）：
        # 目标责任人必须持有该环节所属 CASCADE 家族对应的项目角色（3/4/6/8→版本负责人、
        # 7→测试执行、5→PL）。兜底——任务未挂项目、或项目里该角色尚无在编成员时不设卡，
        # 可用性优先（与前端"过滤后为空则维持全量列表"同一口径）。
        fam_pair = CASCADE_FIELD.get(step.step)
        if fam_pair and task.project_id:
            spr, spr_zh = FAMILY_PROJECT_ROLE[fam_pair[0]]
            holder_rows = (await self.db.execute(
                select(ProjectMember.user_id).where(
                    ProjectMember.project_id == task.project_id,
                    ProjectMember.project_role == spr,
                )
            )).scalars().all()
            holder_set = set(holder_rows)
            if holder_set and new_uid not in holder_set:
                raise ValueError(
                    f"环节{step.step}「{step.step_name}」仅限项目角色「{spr_zh}」担任，"
                    f"{new_user.realname or new_user.account} 未持该角色"
                )

        # 一次取齐新旧姓名用于留痕（旧负责人可为空 → 待指派）
        ids = [i for i in {step.assigned_to, new_uid} if i]
        names: dict = {}
        if ids:
            rows = (await self.db.execute(
                select(User.id, User.realname, User.account).where(User.id.in_(ids))
            )).all()
            names = {r[0]: (r[1] or r[2]) for r in rows}
        old_label = (
            f"{names.get(step.assigned_to, '未知')}(ID:{step.assigned_to})" if step.assigned_to else "待指派"
        )
        new_label = f"{new_user.realname or new_user.account}(ID:{new_uid})"

        step.assigned_to = new_uid

        # 级联回写任务级角色字段（2026-09-22 用户反馈：改派后标题栏没变化——
        # 详情页头部的「版本负责人/测试执行人/PL」横幅读的是任务级字段，
        # 只改步骤行会造成两层撕裂；需求原话"同步到任务状态"即指此）。
        # 映射见模块级 CASCADE_FIELD（task_service 顶部）：3/4/6/8 ← version_owner；
        # 7 ← executor_id；5 ← owner。家族↔项目角色见 FAMILY_PROJECT_ROLE。
        sync_note = ""
        pair = CASCADE_FIELD.get(step.step)
        if pair:
            fname, fzhan = pair
            setattr(task, fname, new_uid)
            # 同族环节对齐（用户原话"整个流程对应责任人都会同步到任务状态"）：
            # 共享同一任务字段的兄弟环节(如 3/4/6/8 同属版本负责人)中，
            # 所有未完成者一并对齐为新负责人——任务字段与步骤行不打架。
            # 已完成环节只读(与 completed 锁一致)，缺席不动。
            siblings = (await self.db.execute(
                select(TaskStep).where(
                    TaskStep.task_id == task_id,
                    TaskStep.step.in_([k for k, v in CASCADE_FIELD.items() if v[0] == fname]),
                    TaskStep.status != StepStatus.completed.value,
                )
            )).scalars().all()
            aligned = []
            for sb in siblings:
                if sb.id == step.id or sb.assigned_to == new_uid:
                    continue
                sb.assigned_to = new_uid
                aligned.append(sb.step)
            if aligned:
                sync_note = f"；任务级{fzhan}已同步为{new_label}，同组环节{'/'.join(map(str,aligned))}未完成者已一并对齐"
            else:
                sync_note = f"；任务级{fzhan}已同步为{new_label}"

        await self._log_action(
            task_id, "step_reassign", actor.id,
            f"步骤{step.step}「{step.step_name}」责任人由「{old_label}」改为「{new_label}」{sync_note}"
        )
        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def _reset_step(self, task_id: int, step_no: int, reason: str = "rejected"):
        """将指定步骤退回待处理

        reason 只用于给「本轮记录」定结局（默认 rejected=被打回）。
        本轮的时间区间必须在这里收口：_reset_step 之后 begin_date/end_date 会被清空，
        不收口的话这一轮的起止就永久丢了，只剩一个空壳。
        """
        stmt = select(TaskStep).where(
            TaskStep.task_id == task_id, TaskStep.step == step_no
        )
        result = await self.db.execute(stmt)
        target = result.scalar_one_or_none()
        if target:
            if target.rounds or target.begin_date:
                target.rounds = _close_round(
                    target.rounds, _now(), reason, fallback_start=target.begin_date
                )
            target.status = StepStatus.pending.value
            target.begin_date = None
            target.end_date = None
            # 环节9（数据审核）的 PL/TSE 审核结论是"本轮裁决"，打回后必须清空。
            # 否则下一轮重新提上来时，前端会用旧 ext_data 回填两个下拉框：
            # 一方改成"通过"、另一方还留着上一轮的"需补测"，一点完成又被打回。
            # 结论已由 update_step 在打回前归档进操作历史，不会丢审计。
            if step_no == 9 and isinstance(target.ext_data, dict):
                cleaned = dict(target.ext_data)
                cleaned.pop("pl_review", None)
                cleaned.pop("tse_review", None)
                target.ext_data = cleaned

    async def _update_task_progress(self, task_id: int):
        """更新任务进度"""
        # 获取所有步骤
        stmt = select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step)
        result = await self.db.execute(stmt)
        steps = result.scalars().all()
        
        if not steps:
            return
        
        completed = sum(1 for s in steps if s.status == StepStatus.completed.value)
        progress = int(completed / len(steps) * 100)
        
        # 找当前步骤
        current_step = 1
        for i, s in enumerate(steps):
            if s.status == StepStatus.in_progress.value:
                current_step = s.step
                break
            if s.status == StepStatus.pending.value and current_step == 1:
                current_step = s.step
        
        # 如果全部完成：终态锁定为 done——不再经过"末环步号查表"这一跳
        # （历史背景：曾有第10步"任务结束"扛终态，裁撤后末环变为9，而9在做时是
        # review——若继续 mapping[len(steps)] 会把全完成误判成 review 永不封卷）
        if completed == len(steps):
            current_step = len(steps)
            progress = 100
            task_status = TaskStatus.done.value
        else:
            task_status = self._get_status_by_step(current_step)

        values = dict(progress=progress, current_step=current_step, status=task_status)
        if completed == len(steps):
            # 封卷：首次进入 done 时记任务实际完成时间。
            # 已记过就不覆盖——避免后续任何一次进度重算把原始交付时间冲掉。
            task_row = (await self.db.execute(
                select(TestTask).where(TestTask.id == task_id)
            )).scalar_one_or_none()
            if task_row is not None and task_row.actual_end_date is None:
                values["actual_end_date"] = _now()
        else:
            # 未全部完成（含末环被打回重开）：清空实际完成时间，
            # 否则会出现"任务已回到测试中、实际完成时间却还留着"的自相矛盾。
            # 上一轮的封卷时刻仍可从环节9的 rounds 与操作历史里查到。
            values["actual_end_date"] = None

        # 更新任务
        stmt = update(TestTask).where(TestTask.id == task_id).values(**values)
        await self.db.execute(stmt)
    
    def _get_status_by_step(self, step: int) -> str:
        """根据步骤获取状态（步骤1-9"在做"的阶段名；全完成走 _update_task_progress 直置 done，
        原第10步"任务结束"扛终态的职责已由其接管）"""
        mapping = {
            1: TaskStatus.init.value,
            2: TaskStatus.design.value,
            3: TaskStatus.version_confirm.value,
            4: TaskStatus.env_confirm.value,
            5: TaskStatus.env_approved.value,
            6: TaskStatus.testing.value,  # 测试用例分配
            7: TaskStatus.testing.value,  # 执行测试
            8: TaskStatus.testing.value,  # 测试完成
            9: TaskStatus.review.value,    # 数据审核（末环）
        }
        return mapping.get(step, TaskStatus.init.value)
    
    async def _log_action(
        self, 
        task_id: int, 
        action: str, 
        actor: int, 
        comment: str
    ):
        """记录操作日志"""
        log = TaskAction(
            task_id=task_id,
            action=action,
            actor=actor,
            comment=comment,
        )
        self.db.add(log)
    
    async def get_stats(self) -> dict:
        """获取统计数据"""
        # 总数
        total_stmt = select(func.count(TestTask.id)).where(TestTask.deleted == False)
        total_result = await self.db.execute(total_stmt)
        total = total_result.scalar()
        
        # 进行中
        active_stmt = select(func.count(TestTask.id)).where(
            TestTask.deleted == False,
            ~TestTask.status.in_([TaskStatus.done.value, TaskStatus.closed.value])
        )
        active_result = await self.db.execute(active_stmt)
        active = active_result.scalar()
        
        # 已完成
        done = total - active
        
        return {"total": total, "active": active, "done": done}


class IssueService:
    """问题记录服务"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def generate_bug_no(self, task_id: int) -> str:
        """生成问题编号"""
        stmt = select(TestTask).where(TestTask.id == task_id)
        result = await self.db.execute(stmt)
        task = result.scalar_one_or_none()
        
        if not task:
            return f"BUG-{datetime.now().strftime('%Y%m%d')}-001"
        
        prefix = task.code.replace("-", "")
        stmt = select(func.max(TaskIssue.bug_no)).where(
            TaskIssue.bug_no.like(f"{prefix}-%")
        )
        result = await self.db.execute(stmt)
        max_no = result.scalar()
        
        if max_no:
            seq = int(max_no[-3:]) + 1
        else:
            seq = 1
        
        return f"{prefix}-{seq:03d}"
    
    async def create_issue(self, data: IssueCreate, user_id: int, task_id: int = None) -> TaskIssue:
        """创建问题（轻量化：外部问题单号由调用方录入，标题可省略）"""
        title = data.title
        if not title:
            title = f"问题单 {data.issue_no}" if data.issue_no else "关联问题单"
        issue = TaskIssue(
            task_id=task_id or data.task_id,
            case_link_id=data.case_link_id,
            issue_no=data.issue_no,
            bug_no=await self.generate_bug_no(task_id or data.task_id),
            title=title,
            severity=data.severity,
            priority=data.priority,
            assigned_to=data.assigned_to,
            steps=data.steps,
            description=data.description,
            reporter=user_id,
        )
        self.db.add(issue)
        await self.db.commit()
        await self.db.refresh(issue)
        return issue
    
    async def get_issues(self, task_id: int) -> List[TaskIssue]:
        """获取问题的列表"""
        stmt = select(TaskIssue).where(
            TaskIssue.task_id == task_id
        ).order_by(TaskIssue.id.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
    
    async def update_issue(
        self, 
        issue_id: int, 
        data: IssueUpdate
    ) -> Optional[TaskIssue]:
        """更新问题"""
        stmt = select(TaskIssue).where(TaskIssue.id == issue_id)
        result = await self.db.execute(stmt)
        issue = result.scalar_one_or_none()
        
        if not issue:
            return None
        
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(issue, key, value)

        await self.db.commit()
        await self.db.refresh(issue)
        return issue

    async def get_issue_tree(self) -> IssueTreeResponse:
        """按 产品→项目→测试任务 维度聚合所有问题单（仅含未删除任务）"""
        stmt = (
            select(TaskIssue, TestTask)
            .join(TestTask, TaskIssue.task_id == TestTask.id)
            .where(TestTask.deleted == False)
            .order_by(TaskIssue.id.desc())
        )
        result = await self.db.execute(stmt)
        rows = result.all()
        if not rows:
            return IssueTreeResponse(total=0, open_count=0, products=[])

        # 收集关联ID
        user_ids = set()
        project_ids = set()
        case_link_ids = set()
        for issue, task in rows:
            if issue.reporter:
                user_ids.add(issue.reporter)
            if issue.assigned_to:
                user_ids.add(issue.assigned_to)
            if issue.case_link_id:
                case_link_ids.add(issue.case_link_id)
            if task.project_id:
                project_ids.add(task.project_id)

        # 用户映射 id -> 显示名
        user_map = {}
        if user_ids:
            u_res = await self.db.execute(select(User).where(User.id.in_(user_ids)))
            for u in u_res.scalars().all():
                user_map[u.id] = u.realname or u.account

        # 项目 / 产品映射
        project_map = {}
        if project_ids:
            p_res = await self.db.execute(select(Project).where(Project.id.in_(project_ids)))
            for p in p_res.scalars().all():
                project_map[p.id] = p
        product_ids = {p.product_id for p in project_map.values() if p.product_id}
        product_map = {}
        if product_ids:
            pr_res = await self.db.execute(select(Product).where(Product.id.in_(product_ids)))
            for pr in pr_res.scalars().all():
                product_map[pr.id] = pr

        # 用例标题映射
        case_map = {}
        if case_link_ids:
            cl_res = await self.db.execute(
                select(TaskCaseLink).where(TaskCaseLink.id.in_(case_link_ids))
            )
            for cl in cl_res.scalars().all():
                case_map[cl.id] = cl.case_title

        # 构建树
        tree = {}
        total = 0
        open_count = 0
        for issue, task in rows:
            total += 1
            # 未关闭口径(2026-09-23用户反馈定夺): 只有待处理/已指派算未关,fixed(已解决)=绿灯
            # 终态不再计——问题单模块是两态工作流(open↔fixed),"已解决"就该从"未关闭"退役。
            # 与前端工作台statIssues口径一致(ISSUE_OPEN_STATUSES),改这里的务必同步改那边!
            if issue.status in ("open", "assigned"):
                open_count += 1

            node = IssueTreeNode(
                id=issue.id,
                bug_no=issue.bug_no,
                issue_no=issue.issue_no,
                title=issue.title,
                severity=issue.severity,
                status=issue.status,
                reporter=issue.reporter,
                reporter_name=user_map.get(issue.reporter),
                assigned_to=issue.assigned_to,
                assigned_name=user_map.get(issue.assigned_to),
                case_link_id=issue.case_link_id,
                case_title=case_map.get(issue.case_link_id) if issue.case_link_id else None,
                created_at=issue.created_at.isoformat() if issue.created_at else None,
            )

            proj = project_map.get(task.project_id) if task.project_id else None
            if proj and proj.product_id and proj.product_id in product_map:
                prod = product_map[proj.product_id]
                prod_id = prod.id
                prod_name = prod.name
            else:
                prod_id = None
                prod_name = task.product_name or "未关联产品"

            p = tree.setdefault(
                ("p", prod_id, prod_name),
                {"product_id": prod_id, "product_name": prod_name, "projects": {}},
            )
            proj_key = ("j", proj.id if proj else None, task.project_id)
            proj_name = proj.name if proj else (f"项目#{task.project_id}" if task.project_id else "未关联项目")
            j = p["projects"].setdefault(
                proj_key,
                {"project_id": proj.id if proj else None, "project_name": proj_name, "tasks": {}},
            )
            # 2026-09-23口径修复: 在收录期就把本任务的快照存进dict——
            # 此前组装段直接引用外层rows循环的遗留变量task(=最后一行), 造成同项目下
            # 所有任务的version/status/progress/current_step都是"碰巧最后一个任务"的值(用户实证显示错误)
            t = j["tasks"].setdefault(
                task.id,
                {"task_id": task.id, "task_name": task.name, "task_code": task.code,
                 "version": task.version, "status": task.status,
                 "progress": task.progress or 0, "current_step": task.current_step or 0,
                 "begin_date": task.begin_date, "end_date": task.end_date, "issues": []},
            )
            t["issues"].append(node)

        products_out = []
        for _, pv in tree.items():
            projects_out = []
            for _, jv in pv["projects"].items():
                tasks_out = []
                for _, tv in jv["tasks"].items():
                    # 改读本任务自己在收录期的快照(不再用外层遗留变量), 日期补充入户
                    tasks_out.append(IssueTreeTask(
                        task_id=tv["task_id"],
                        task_name=tv["task_name"],
                        task_code=tv["task_code"],
                        version=tv.get("version"),
                        status=tv.get("status"),
                        progress=tv.get("progress", 0),
                        current_step=tv.get("current_step", 0),
                        begin_date=tv.get("begin_date"),
                        end_date=tv.get("end_date"),
                        issues=tv["issues"],
                    ))
                projects_out.append(IssueTreeProject(
                    project_id=jv["project_id"],
                    project_name=jv["project_name"],
                    tasks=tasks_out,
                ))
            products_out.append(IssueTreeProduct(
                product_id=pv["product_id"],
                product_name=pv["product_name"],
                projects=projects_out,
            ))

        return IssueTreeResponse(total=total, open_count=open_count, products=products_out)
