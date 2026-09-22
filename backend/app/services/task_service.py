"""
服务层 - 测试任务管理
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, delete
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime
from app.models.models import (
    TestTask, TaskStep, TaskIssue, TaskAction,
    TaskStatus, StepStatus, IssueStatus,
    User, Project, Product, TaskCaseLink
)
from app.schemas.schemas import (
    TaskCreate, TaskUpdate, TaskStepUpdate, IssueCreate, IssueUpdate,
    IssueResponse, IssueTreeResponse, IssueTreeNode, IssueTreeTask,
    IssueTreeProject, IssueTreeProduct
)
from app.core.security import _user_roles


# 流程步骤定义（10步，去掉原"人力计算"）
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
    {"step": 10, "name": "任务结束", "type": "end", "roles": ["pl"]},
]


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
        from datetime import datetime
        stmt = select(TaskStep).where(
            TaskStep.task_id == task_id, TaskStep.step == 1
        )
        result = await self.db.execute(stmt)
        step1 = result.scalar_one_or_none()
        if step1:
            step1.status = StepStatus.completed.value
            step1.begin_date = datetime.now()
            step1.end_date = datetime.now()
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
        """初始化任务步骤"""
        owner_map = {
            1: task_data.owner,
            2: task_data.tse_id,
            3: task_data.version_owner,
            4: task_data.version_owner,   # 环境确认
            5: task_data.owner,            # 环境审核
            6: task_data.version_owner,   # 测试用例分配
            7: task_data.executor_id,     # 执行测试（合并原"问题记录"功能）
            8: task_data.version_owner,   # 测试完成（报告整理/log链接/自动化结果）—— 归版本负责人
            9: None,                       # 数据审核（PL + TSE 双审核，assigned_to=None 让两人都能操作）
            10: task_data.owner,           # 任务结束
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
    
    async def update_task(self, task_id: int, data: TaskUpdate) -> Optional[TestTask]:
        """更新任务"""
        task = await self.get_task(task_id)
        if not task:
            return None
        
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(task, key, value)
        
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
        user_is_admin: bool = False
    ) -> Optional[TaskStep]:
        """更新步骤状态"""
        stmt = select(TaskStep).where(TaskStep.id == step_id)
        result = await self.db.execute(stmt)
        step = result.scalar_one_or_none()
        
        if not step:
            return None

        # 权限校验：只有步骤 assignee 或 全员环节(assigned_to is None) 才能操作
        # admin 不再自动获得业务环节操作权；如需介入业务环节，需先把自己指派为该环节负责人
        # assigned_to为None的环节(如步骤9数据审核的PL+TSE双审核)全员可操作
        # 步骤7（执行测试）特殊：executor_id 和 executors 列表中的用户都可操作
        is_all_hands = (step.assigned_to is None)
        is_assignee = (step.assigned_to == user_id)
        is_step7_executor = False
        if step.step == 7:
            task_stmt = select(TestTask).where(TestTask.id == step.task_id)
            task_result = await self.db.execute(task_stmt)
            task = task_result.scalar_one_or_none()
            if task:
                executor_ids = set()
                if task.executor_id:
                    executor_ids.add(task.executor_id)
                if task.executors:
                    for e in task.executors:
                        if isinstance(e, dict) and e.get('user_id'):
                            executor_ids.add(int(e['user_id']))
                is_step7_executor = user_id in executor_ids
        if not is_all_hands and not is_assignee and not is_step7_executor:
            raise PermissionError(f"无权操作环节{step.step}「{step.step_name}」，该环节负责人为他人")

        # 保存准出条件数据（单独更新或随状态一起）
        if data.ext_data is not None:
            merged = dict(step.ext_data or {})
            merged.update(data.ext_data)
            step.ext_data = merged

        if data.status:
            step.status = data.status

            if data.status == "in_progress" and step.begin_date is None:
                step.begin_date = datetime.now()
            if data.status == "completed":
                # 从 pending 直接完成时，自动补 begin_date（用户没点"开始"）
                if step.status == StepStatus.pending.value and step.begin_date is None:
                    step.begin_date = datetime.now()
                step.end_date = datetime.now()
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
            # 只重置环节8（测试完成），环节9自己也重置让审核人重新审核
            await self._reset_step(step.task_id, 8)
            await self._reset_step(step.task_id, 9)

        await self._update_task_progress(step.task_id)

        # 记录日志
        action_desc = f"步骤{step.step}状态更新为: {data.status}" if data.status else f"步骤{step.step}准出条件数据更新"
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
    ) -> TaskStep:
        """当前环节回退至上一环节（修订模式：业务数据保留，只动流程状态）

        权限：仅当前环节负责人（步骤7含 executors 全体执行人）；PL 特权已取消
        约束：只能回退当前环节；步骤9 有专属的"需补测打回"规则（双审核）、步骤10 为终态，不走本通道
        （步骤5 已并入手动回退——通过与否以"提交到下一环节/返回上一环节"表达，审核意见记入处理说明）
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
        if step.step != task.current_step:
            raise ValueError("只能回退当前环节")
        if step.step == 9:
            raise ValueError("步骤9请使用既有的『需补测』打回流程（数据审核双人复核）")
        if step.step <= 2:
            raise ValueError("当前环节没有可回退的上一步")

        # 权限：仅当前环节负责人（PL 特权已取消——PL 名下环节与其他角色同权，按 assigned_to 认定）
        is_cur_assignee = (step.assigned_to == user_id) or (step.assigned_to is None)
        if step.step == 7:
            executor_ids = set()
            if task.executor_id:
                executor_ids.add(task.executor_id)
            if task.executors:
                for e in task.executors:
                    if isinstance(e, dict) and e.get('user_id'):
                        executor_ids.add(int(e['user_id']))
            if user_id in executor_ids:
                is_cur_assignee = True
        if not is_cur_assignee:
            raise PermissionError("仅当前环节负责人可回退")

        prev_step_no = step.step - 1
        prev = (await self.db.execute(
            select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step == prev_step_no)
        )).scalar_one_or_none()
        if not prev:
            raise ValueError("上一环节不存在")

        # 回退核心：上一环节重开为"进行中"（保留begin_date等历史痕迹，仅清完成时间）
        # 当前环节保持原状态（通常pending），等上一环节重新完成后自然流转回来
        prev.status = StepStatus.in_progress.value
        prev.end_date = None
        marker = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')} 回退自步骤{step.step}] {reason.strip()}"
        prev.remark = f"{prev.remark} ｜ {marker}" if prev.remark else marker

        # 进度/任务状态统一重算：_update_task_progress 会依据最早的"进行中"环节
        # 自动把 current_step 修正为上一环节，并把任务status映射回对应阶段
        await self._update_task_progress(task_id)

        await self._log_action(
            task_id, "step_rollback", user_id,
            f"步骤{step.step}「{step.step_name}」回退至步骤{prev.step}「{prev.step_name}」。原因：{reason.strip()}"
        )

        await self.db.commit()
        await self.db.refresh(prev)
        return prev

    async def change_step_assignee(self, task_id: int, step_id: int, new_uid: int, actor: User) -> TaskStep:
        """PL改派非PL环节(3-8)的责任人（2026-09-22 需求）

        全链一致的保证方式 = 尊重"单一事实来源"：环节操作权/我的任务/展示文案
        全部由 task_steps.assigned_to 派生，改这一行 + 操作历史留痕即为全量同步；
        同时**级联回写**对应的任务级角色字段（3/4/6/8→version_owner、7→executor_id、
        5→owner，与建任务初始化 owner_map 一一对应）——详情页头部的
        「版本负责人/测试执行人/PL」横幅读的是任务级字段，不回写会两层撕裂
        （2026-09-22 用户反馈"改派后标题栏没变化"）。注意：任务字段→步骤方向的
        既有的级联重铺约定仍在（任务编辑时重铺全部步骤，未被本操作的局部改派覆写前有效）。
        开放范围即"非PL环节"：1/2为PL环节、9为双人共审(owner∪tse，见
        list_tasks pending 特判与前端对应特判)、10为终态，均不开放。
        已完成(completed)环节不做改派；rejected(被打回待处理)保留可改派
        (打回即换人重来的场景)。审计动作 step_reassign 记新旧双名+级联说明。
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
        if step.step in (1, 2, 9, 10):
            raise ValueError(f"环节{step.step}为PL/双人共审/终态环节，不开放改派(仅3-8)")
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
        # 映射与建任务初始化 owner_map（task_service 约126行）一一对应：
        #   3/4/6/8 ← version_owner；7 ← executor_id；5 ← owner
        CASCADE_FIELD = {
            3: ("version_owner", "版本负责人"),
            4: ("version_owner", "版本负责人"),
            6: ("version_owner", "版本负责人"),
            8: ("version_owner", "版本负责人"),
            7: ("executor_id", "测试执行人"),
            5: ("owner", "PL负责人"),
        }
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

    async def _reset_step(self, task_id: int, step_no: int):
        """将指定步骤退回待处理"""
        stmt = select(TaskStep).where(
            TaskStep.task_id == task_id, TaskStep.step == step_no
        )
        result = await self.db.execute(stmt)
        target = result.scalar_one_or_none()
        if target:
            target.status = StepStatus.pending.value
            target.begin_date = None
            target.end_date = None

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
        
        # 如果全部完成
        if completed == len(steps):
            current_step = len(steps)
            progress = 100
        
        # 更新任务
        stmt = update(TestTask).where(TestTask.id == task_id).values(
            progress=progress,
            current_step=current_step,
            status=self._get_status_by_step(current_step)
        )
        await self.db.execute(stmt)
    
    def _get_status_by_step(self, step: int) -> str:
        """根据步骤获取状态"""
        mapping = {
            1: TaskStatus.init.value,
            2: TaskStatus.design.value,
            3: TaskStatus.version_confirm.value,
            4: TaskStatus.env_confirm.value,
            5: TaskStatus.env_approved.value,
            6: TaskStatus.testing.value,  # 测试用例分配
            7: TaskStatus.testing.value,  # 执行测试
            8: TaskStatus.testing.value,  # 测试完成
            9: TaskStatus.review.value,    # 数据审核
            10: TaskStatus.done.value,     # 任务结束
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
            if issue.status not in ("closed", "rejected"):
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
            t = j["tasks"].setdefault(
                task.id,
                {"task_id": task.id, "task_name": task.name, "task_code": task.code, "issues": []},
            )
            t["issues"].append(node)

        products_out = []
        for _, pv in tree.items():
            projects_out = []
            for _, jv in pv["projects"].items():
                tasks_out = []
                for _, tv in jv["tasks"].items():
                    tasks_out.append(IssueTreeTask(
                        task_id=tv["task_id"],
                        task_name=tv["task_name"],
                        task_code=tv["task_code"],
                        version=task.version,
                        status=task.status,
                        progress=task.progress or 0,
                        current_step=task.current_step or 0,
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
