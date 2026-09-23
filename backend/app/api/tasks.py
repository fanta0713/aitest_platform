"""
API路由 - 测试任务
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import List, Optional
from app.db.database import get_db
from app.core.security import get_current_user, _user_roles
from app.models.models import User, Project, TaskStep, TaskCaseLink, TaskIssue
from app.services.task_service import TaskService, IssueService
from app.schemas.schemas import (
    TaskCreate, TaskUpdate, TaskStepUpdate,
    TaskResponse, TaskListResponse,
    StepResponse, IssueCreate, IssueUpdate, IssueResponse,
    IssueTreeResponse,
    ActionResponse, StatsResponse
)

router = APIRouter(prefix="/api/tasks", tags=["测试任务"])


async def _enrich_issues_with_case_title(db: AsyncSession, issues: list) -> list:
    """为问题单列表补全关联用例标题（case_title 非持久化字段，运行期联表获取）"""
    if not issues:
        return []
    link_ids = {i.case_link_id for i in issues if i.case_link_id}
    case_map = {}
    if link_ids:
        res = await db.execute(select(TaskCaseLink).where(TaskCaseLink.id.in_(link_ids)))
        for cl in res.scalars().all():
            case_map[cl.id] = cl.case_title
    out = []
    for i in issues:
        d = IssueResponse.model_validate(i)
        d.case_title = case_map.get(i.case_link_id) if i.case_link_id else None
        out.append(d)
    return out


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: Optional[str] = Query(None, description="状态筛选"),
    owner: Optional[int] = Query(None, description="负责人ID"),
    mine: bool = Query(False, description="只看我的任务"),
    pending: bool = Query(False, description="只看待我处理的任务"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取任务列表"""
    service = TaskService(db)
    if mine:
        owner = current_user.id
    tasks, total = await service.get_tasks(status=status, owner=owner, limit=limit, offset=offset)
    # pending过滤：只返回当前环节分配给自己的任务，已完成的任务不再挂人名下
    if pending:
        def _is_my_turn(t):
            if t.status == 'done':
                return False
            if t.current_step == 9:
                # 数据审核为 PL+TSE 双人共审（task_steps.assigned_to=None，单人字段
                # 表达不了两人），PL/TSE 任一人的"待我处理"都必须出现(2026-09-22 用户反馈)
                return current_user.id in (t.owner, t.tse_id)
            return t.current_step_assignee == current_user.id
        tasks = [t for t in tasks if _is_my_turn(t)]
        total = len(tasks)
    # 加载每个任务的 current_step_name（从 task_steps 查），避免流程改造后步骤名错位
    task_ids = [t.id for t in tasks]
    step_name_map = {}
    if task_ids:
        sn_stmt = select(TaskStep.task_id, TaskStep.step_name).where(
            TaskStep.task_id.in_(task_ids)
        )
        # 需要按 step==current_step 配对，先拉所有相关 step
        all_steps_stmt = select(TaskStep.task_id, TaskStep.step, TaskStep.step_name, TaskStep.assigned_to).where(
            TaskStep.task_id.in_(task_ids)
        )
        all_steps = (await db.execute(all_steps_stmt)).all()
        # 收集所有 assignee id 用于批量查人名
        assignee_ids = set()
        for tid, step_no, step_name, assigned_to in all_steps:
            task_obj = next((t for t in tasks if t.id == tid), None)
            if task_obj and step_no == task_obj.current_step:
                step_name_map[tid] = step_name
                if assigned_to:
                    assignee_ids.add(assigned_to)
        # 批量查人名
        assignee_name_map = {}
        if assignee_ids:
            from app.models.models import User
            users_result = await db.execute(select(User.id, User.realname).where(User.id.in_(assignee_ids)))
            for uid, realname in users_result.all():
                assignee_name_map[uid] = realname
        # 填充 current_step_assignee 和 assignee_name
        step_assignee_map = {}
        for tid, step_no, step_name, assigned_to in all_steps:
            task_obj = next((t for t in tasks if t.id == tid), None)
            if task_obj and step_no == task_obj.current_step:
                step_assignee_map[tid] = assigned_to
    items = []
    for t in tasks:
        item = TaskResponse.model_validate(t)
        item.current_step_name = step_name_map.get(t.id)
        # 已完成的任务不再挂在某个人名下
        if t.status == 'done':
            item.current_step_assignee = None
            item.current_step_assignee_name = None
        else:
            item.current_step_assignee = step_assignee_map.get(t.id)
            if item.current_step_assignee:
                item.current_step_assignee_name = assignee_name_map.get(item.current_step_assignee)
        items.append(item)
    return TaskListResponse(total=total, items=items)


# PL和TSE可创建/删除任务，admin全权限
TASK_MANAGER_ROLES = {"admin", "pl", "tse"}


@router.post("", response_model=TaskResponse)
async def create_task(
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建任务（仅PL/TSE/管理员）"""
    user_roles = set(_user_roles(current_user))
    if not (user_roles & TASK_MANAGER_ROLES):
        raise HTTPException(status_code=403, detail="仅PL/TSE/管理员可创建测试任务")
    # 任务周期必须落在项目周期内
    if data.project_id:
        proj = (await db.execute(select(Project).where(Project.id == data.project_id))).scalar_one_or_none()
        if proj:
            if proj.begin_date and data.begin_date and data.begin_date < proj.begin_date:
                raise HTTPException(status_code=400, detail=f"任务开始日期不能早于项目开始日期（{proj.begin_date.isoformat()}）")
            if proj.end_date and data.end_date and data.end_date > proj.end_date:
                raise HTTPException(status_code=400, detail=f"任务结束日期不能晚于项目结束日期（{proj.end_date.isoformat()}）")
            if data.begin_date and data.end_date and data.begin_date > data.end_date:
                raise HTTPException(status_code=400, detail="任务开始日期不能晚于结束日期")
    elif data.begin_date and data.end_date and data.begin_date > data.end_date:
        raise HTTPException(status_code=400, detail="任务开始日期不能晚于结束日期")
    service = TaskService(db)
    task = await service.create_task(data, current_user.id)
    # 重新用selectinload加载，避免current_step_assignee懒加载失败
    task = await service.get_task(task.id)
    return TaskResponse.model_validate(task)


@router.get("/stats", response_model=StatsResponse)
async def get_stats(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """获取统计数据"""
    service = TaskService(db)
    stats = await service.get_stats()
    return StatsResponse(**stats)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取任务详情"""
    service = TaskService(db)
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    resp = TaskResponse.model_validate(task)
    # 填充 current_step_name 和 current_step_assignee_name（从已加载的 task.steps 找）
    if task.steps:
        for s in task.steps:
            if s.step == task.current_step:
                resp.current_step_name = s.step_name
                # 已完成的任务不再挂在某个人名下
                if task.status != 'done':
                    resp.current_step_assignee = s.assigned_to
                    if s.assigned_to:
                        from app.models.models import User
                        u = (await db.execute(select(User).where(User.id == s.assigned_to))).scalar_one_or_none()
                        if u:
                            resp.current_step_assignee_name = u.realname
                break
    return resp


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    data: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新任务（仅管理员、PL或版本负责人可操作）"""
    service = TaskService(db)
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    user_roles = set(_user_roles(current_user))
    is_admin = 'admin' in user_roles
    is_pl_or_version_owner = current_user.id in {task.owner, task.version_owner}
    if not (is_admin or is_pl_or_version_owner):
        raise HTTPException(status_code=403, detail="仅管理员、PL或版本负责人可更新此任务")
    task = await service.update_task(task_id, data)
    return TaskResponse.model_validate(task)


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除测试任务（软删除，PL/TSE/管理员可操作）"""
    user_roles = set(_user_roles(current_user))
    if not (user_roles & TASK_MANAGER_ROLES):
        raise HTTPException(status_code=403, detail="仅PL/TSE/管理员可删除测试任务")
    service = TaskService(db)
    ok = await service.delete_task(task_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "message": "任务已删除"}


@router.get("/{task_id}/steps", response_model=List[StepResponse])
async def get_steps(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取任务步骤"""
    service = TaskService(db)
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return [StepResponse.model_validate(s) for s in task.steps]


@router.put("/{task_id}/steps/{step_id}", response_model=StepResponse)
async def update_step(
    task_id: int,
    step_id: int,
    data: TaskStepUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新步骤状态（仅分配给当前用户的环节可操作，全员环节除外）"""
    service = TaskService(db)
    try:
        step = await service.update_step(step_id, data, current_user.id, current_user.role == "admin")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not step:
        raise HTTPException(status_code=404, detail="步骤不存在")
    return StepResponse.model_validate(step)


class StepRollbackReq(BaseModel):
    """环节回退请求：原因必填，计入流程记录（目标环节remark + 操作历史）"""
    reason: str = Field(..., min_length=1, description="回退原因")


@router.post("/{task_id}/steps/{step_id}/rollback", response_model=StepResponse)
async def rollback_step(
    task_id: int,
    step_id: int,
    data: StepRollbackReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """当前环节回退至上一环节（仅PL或当前环节负责人；步骤5/9走既有打回、10为终态）"""
    service = TaskService(db)
    try:
        step = await service.rollback_step(task_id, step_id, data.reason, current_user.id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return step


class StepAssigneeReq(BaseModel):
    assigned_to: int = Field(..., description="新环节责任人用户ID")


@router.patch("/{task_id}/steps/{step_id}/assignee", response_model=StepResponse)
async def change_step_assignee(
    task_id: int,
    step_id: int,
    data: StepAssigneeReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """PL改派非PL环节(3-8)的责任人——全链以 task_steps.assigned_to 为单一事实源自动同步，操作历史留痕"""
    service = TaskService(db)
    try:
        step = await service.change_step_assignee(task_id, step_id, data.assigned_to, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return step


@router.get("/{task_id}/issues", response_model=List[IssueResponse])
async def get_issues(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取问题列表"""
    service = IssueService(db)
    issues = await service.get_issues(task_id)
    return await _enrich_issues_with_case_title(db, issues)


@router.post("/{task_id}/issues", response_model=IssueResponse)
async def create_issue(
    task_id: int,
    data: IssueCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建问题"""
    service = IssueService(db)
    issue = await service.create_issue(data, current_user.id, task_id)
    return (await _enrich_issues_with_case_title(db, [issue]))[0]


@router.put("/issues/{issue_id}", response_model=IssueResponse)
async def update_issue(
    issue_id: int,
    data: IssueUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新问题(2026-09-23权限定夺: 仅提单人本人可改,管理员豁免——用户:"陆家豪提的单梅昌富就不能更改状态")"""
    service = IssueService(db)
    # 预取做属主校验(404/403语义分明;update_issue内部不再重复取)
    res = await db.execute(select(TaskIssue).where(TaskIssue.id == issue_id))
    issue_row = res.scalar_one_or_none()
    if not issue_row:
        raise HTTPException(status_code=404, detail="问题不存在")
    if issue_row.reporter != current_user.id and "admin" not in _user_roles(current_user):
        raise HTTPException(status_code=403, detail="仅提单人可修改问题单状态")
    issue = await service.update_issue(issue_id, data)
    if not issue:
        raise HTTPException(status_code=404, detail="问题不存在")
    return (await _enrich_issues_with_case_title(db, [issue]))[0]


@router.get("/issues/tree", response_model=IssueTreeResponse)
async def get_issues_tree(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """跨任务问题单树：产品 → 项目 → 测试任务"""
    service = IssueService(db)
    return await service.get_issue_tree()


@router.get("/{task_id}/actions", response_model=List[ActionResponse])
async def get_actions(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取操作历史"""
    service = TaskService(db)
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return [ActionResponse.model_validate(a) for a in task.actions]
