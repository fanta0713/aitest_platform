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
    TaskStatus, StepStatus, IssueStatus
)
from app.schemas.schemas import TaskCreate, TaskUpdate, TaskStepUpdate, IssueCreate, IssueUpdate


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
        
        return list(tasks), total
    
    async def get_task(self, task_id: int) -> Optional[TestTask]:
        """获取单个任务"""
        stmt = select(TestTask).options(
            selectinload(TestTask.steps),
            selectinload(TestTask.issues),
            selectinload(TestTask.actions)
        ).where(TestTask.id == task_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
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
        """创建问题"""
        issue = TaskIssue(
            task_id=task_id or data.task_id,
            bug_no=await self.generate_bug_no(task_id or data.task_id),
            title=data.title,
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
