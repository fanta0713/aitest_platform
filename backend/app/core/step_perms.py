"""环节操作权限的唯一口径。

历史背景：同一个"谁能操作某环节"的判断原先散落在三处——`TaskService.update_step`、
`files.py` 的 `_check_step_permission`、前端 `renderStepPage`，各写一份。2026-11-25
环节9 出现"版本负责人能替 PL/TSE 审核"，根因就是几处都把 `assigned_to IS NULL`
一律当成"全员可操作"，而环节9 的 NULL 其实是"PL+TSE 双人共审"。

规则（按优先级）：
  1. `step.assigned_to == user`              → 可操作
  2. 环节9 数据审核                           → 仅任务 PL(owner) 与 TSE(tse_id)
  3. 环节7 执行测试                           → executor_id + executors 列表全员
  4. 其余环节 `assigned_to IS NULL`           → PL(owner) 或具备该环节角色的人兜底
     （NULL 只可能来自"建任务时没选 TSE/版本负责人/执行人"，不是"全员环节"。
       放行全员等于让任何人推动别人的任务；只给 PL 又会让真正该干活的 TSE/版本负责人
       干不了（2026-11-26 任务119 实测：没选 TSE，唯一有 tse 角色的人被挡在门外）。
       所以按环节对应角色兜底：2→tse、3/4/6/8→version_owner、7→executor、1/5→pl，PL 始终可兜。）
  5. 其余                                      → 拒绝

注意：admin **不**自动获得业务环节操作权（项目既有约定），要介入须先把自己指派为负责人。
"""
from typing import Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import select

from app.core.security import _user_roles
from app.models.models import TaskStep, TestTask

# 环节未指定负责人时，按"该环节对应的角色"兜底（不是"全员可操作"）
STEP_ROLE_FALLBACK = {
    1: ("pl",),
    2: ("tse",),
    3: ("version_owner",),
    4: ("version_owner",),
    5: ("pl",),
    6: ("version_owner",),
    7: ("executor",),
    8: ("version_owner",),
}
_ROLE_ZH = {"pl": "PL", "tse": "TSE", "version_owner": "版本负责人", "executor": "执行人"}


def executor_ids_of(task: Optional[TestTask]) -> set:
    """环节7 的操作人集合：主执行人 + 多人投入列表"""
    ids = set()
    if not task:
        return ids
    if task.executor_id:
        ids.add(task.executor_id)
    for e in (task.executors or []):
        if isinstance(e, dict) and e.get("user_id"):
            try:
                ids.add(int(e["user_id"]))
            except (TypeError, ValueError):
                continue
    return ids


def can_operate_step(step: TaskStep, task: Optional[TestTask], user_id: int,
                     user_roles: Optional[Sequence[str]] = None) -> bool:
    if step.assigned_to is not None and step.assigned_to == user_id:
        return True
    if step.step == 9:
        return bool(task) and user_id in {task.owner, task.tse_id}
    if step.step == 7 and user_id in executor_ids_of(task):
        return True
    if step.assigned_to is None:
        # 该环节没指定负责人（建任务时对应角色留空）：PL 兜底，或由具备该环节角色的人来做
        if task is not None and task.owner == user_id:
            return True
        # 防御：user_roles 传错类型（例如历史调用方传了 bool）时不要炸，按"无角色"处理
        roles = user_roles if isinstance(user_roles, (list, tuple, set, frozenset)) else ()
        return bool(set(roles) & set(STEP_ROLE_FALLBACK.get(step.step, ())))
    return False


def deny_detail(step: TaskStep) -> str:
    if step.step == 9:
        return "环节9「数据审核」为 PL 与 TSE 双人复核环节，仅任务的 PL、TSE 可操作"
    if step.assigned_to is None:
        need = "、".join(_ROLE_ZH.get(r, r) for r in STEP_ROLE_FALLBACK.get(step.step, ()))
        tail = f" 或 {need} 角色" if need else ""
        return f"环节{step.step}「{step.step_name}」尚未指定负责人，仅任务 PL{tail} 可操作"
    return f"无权操作环节{step.step}「{step.step_name}」，该环节负责人不是你"


async def load_step_and_task(db, task_id: int, step_no: int):
    step = (await db.execute(
        select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step == step_no)
    )).scalar_one_or_none()
    task = (await db.execute(
        select(TestTask).where(TestTask.id == task_id)
    )).scalar_one_or_none()
    return step, task


async def assert_can_operate(db, task_id: int, step_no: int, user, action: str = "操作"):
    """要求用户是该环节的操作人，否则 403。返回 (step, task) 供调用方复用。"""
    step, task = await load_step_and_task(db, task_id, step_no)
    if not step:
        raise HTTPException(status_code=404, detail=f"环节{step_no}不存在")
    if not can_operate_step(step, task, user.id, _user_roles(user)):
        raise HTTPException(status_code=403, detail=deny_detail(step))
    return step, task


async def assert_can_operate_any(db, task_id: int, step_nos: Sequence[int],
                                 user, action: str = "操作"):
    """任一环节可操作即放行。

    用于跨两个环节共用的操作——关联/解除关联用例在「测试设计(2)」和「执行测试(7)」
    两个环节的页面上都有入口。
    """
    first = None
    for n in step_nos:
        step, task = await load_step_and_task(db, task_id, n)
        if step is None:
            continue
        if first is None:
            first = step
        if can_operate_step(step, task, user.id, _user_roles(user)):
            return step, task
    if first is None:
        raise HTTPException(status_code=404, detail="任务环节不存在")
    raise HTTPException(
        status_code=403,
        detail=f"无权{action}：仅" + "、".join(f"环节{n}" for n in step_nos) + "的负责人可操作",
    )
