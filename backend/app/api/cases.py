"""
API路由 - 测试用例关联与执行结果（本地用例库，不依赖禅道）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from app.db.database import get_db
from app.core.security import get_current_user
from app.core.step_perms import assert_can_operate, assert_can_operate_any
from app.models.models import User, TestTask, TaskStep, TaskCaseLink, CaseResult, TestCase, CaseModule
from app.services.task_service import TaskService

router = APIRouter(prefix="/api/tasks", tags=["测试用例"])

_CN_TZ = ZoneInfo("Asia/Shanghai")
_CASE_STATUS_ZH = {"pass": "通过", "fail": "失败", "block": "阻塞", "na": "不支持", "partial": "部分执行"}
CASE_STATUSES = tuple(_CASE_STATUS_ZH)


def _marked_steps(step_results) -> dict:
    """步骤明细 → {step_idx: status}；不是 dict 或 step_idx 不是整数的条目忽略"""
    marked = {}
    for r in step_results or []:
        if not isinstance(r, dict):
            continue
        try:
            idx = int(r.get("step_idx"))
        except (TypeError, ValueError):
            continue
        marked[idx] = r.get("status")
    return marked


def effective_case_status(status: Optional[str], step_results, steps: list) -> Optional[str]:
    """读取口径：把历史上「通过但步骤没测完」的旧结果换算成 partial（部分执行），不改库。

    2026-11-26 之前后端不校验步骤覆盖，存过一批 status=pass 但 step_results 没覆盖全部步骤的结果，
    统计/导出都应按 partial 计。完全没有步骤明细的早期「通过」无法判断测没测完，保持原样。
    """
    if status != "pass" or not steps or not step_results:
        return status
    marked = {i for i in _marked_steps(step_results) if 0 <= i < len(steps)}
    return "partial" if len(marked) < len(steps) else "pass"


def _derive_case_status(steps: list, step_results: Optional[list], client_status: str):
    """按步骤结果推导用例整体状态，返回 (status, error)。后端是唯一口径，不信任前端传的 status。

    规则（与前端 deriveOverallStatus 同口径，2026-11-26 用户两次反馈后定稿）：
      任一步骤失败 → fail；否则任一阻塞 → block（失败/阻塞允许其余步骤未测）；
      否则所有步骤都已标记 → 全部不支持为 na，其余 pass；
      否则（有步骤已标、有步骤未测、无失败/阻塞）→ partial「部分执行」：进度先存下来，下次打开接着测，不算通过。
      一个步骤都没标却要判通过 → error。
    用例没有步骤时不校验，沿用前端传的状态。
    整条用例直接判失败/阻塞且完全没填步骤明细的，放行（不是本次问题）。
    """
    if not steps:
        return None, None
    marked = {i: s for i, s in _marked_steps(step_results).items() if 0 <= i < len(steps)}
    if any(s == "fail" for s in marked.values()):
        return "fail", None
    if any(s == "block" for s in marked.values()):
        return "block", None
    missing = [i + 1 for i in range(len(steps)) if i not in marked]
    if missing:
        if not marked:
            if client_status in ("fail", "block"):
                return client_status, None
            return None, f"步骤 {'、'.join(map(str, missing))} 未执行，用例不能判为通过（不适用的步骤请标「不支持」）"
        return "partial", None
    return ("na" if all(s == "na" for s in marked.values()) else "pass"), None


async def _case_assignee(db: AsyncSession, task_id: int, link_id: int) -> Optional[int]:
    """步骤6「用例指派」里这条用例的被指派人（task_steps[step=6].ext_data.case_assignments[link_id]）

    键是 link_id 的字符串形式，值是 user_id；未指派返回 None。
    """
    step6 = (await db.execute(
        select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step == 6)
    )).scalar_one_or_none()
    if step6 is None or not isinstance(step6.ext_data, dict):
        return None
    asg = step6.ext_data.get("case_assignments")
    if not isinstance(asg, dict):
        return None
    v = asg.get(str(link_id), asg.get(link_id))
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _user_name(db: AsyncSession, uid: Optional[int]) -> str:
    if not uid:
        return "—"
    u = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    return (u.realname or u.account) if u else f"用户{uid}"


class CaseResultReq(BaseModel):
    status: str = Field(..., description="pass/fail/block/na/partial")
    comment: str = ""
    step_results: Optional[List[dict]] = None  # [{step_idx, actual, status}] 步骤级结果


@router.get("/{task_id}/cases")
async def list_linked_cases(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取任务已关联的用例列表（含最新执行结果）"""
    return await get_enriched_linked_cases(db, task_id)


async def get_enriched_linked_cases(db: AsyncSession, task_id: int) -> list:
    """获取任务已关联的用例列表（含模块路径与最新执行结果），供列表/导出复用"""
    stmt = select(TaskCaseLink).options(
        selectinload(TaskCaseLink.results)
    ).where(TaskCaseLink.task_id == task_id).order_by(TaskCaseLink.id)
    result = await db.execute(stmt)
    links = result.scalars().all()

    # 拉取关联的 TestCase（含 steps 字段）
    local_case_ids = [l.local_case_id for l in links if l.local_case_id]
    case_map = {}
    if local_case_ids:
        case_stmt = select(TestCase).where(TestCase.id.in_(local_case_ids))
        case_result = await db.execute(case_stmt)
        for c in case_result.scalars().all():
            case_map[c.id] = c

    # 拉取关联用例所属模块（含完整父链），供前端按模块层级聚合展示
    module_map = {}
    module_id_set = {c.module_id for c in case_map.values() if c.module_id}
    if module_id_set:
        to_fetch = set(module_id_set)
        loaded_ids = set()
        while to_fetch:
            mod_stmt = select(CaseModule).where(CaseModule.id.in_(to_fetch))
            mod_result = await db.execute(mod_stmt)
            fetched = mod_result.scalars().all()
            for m in fetched:
                module_map[m.id] = m
                loaded_ids.add(m.id)
            next_parents = {
                m.parent_id for m in fetched
                if m.parent_id and m.parent_id not in loaded_ids
            }
            to_fetch = next_parents

    def build_module_path(mid):
        # 必须带 visited 环保护：模块 parent_id 一旦成环（历史导入 / 直接改库），
        # 原来的 `while cur:` 会无限循环，请求永不返回，前端就永久停在「加载中」
        # （2026-11-26 生产任务90 环节7 卡加载中的可能成因之一）。
        names = []
        seen = set()
        cur = module_map.get(mid)
        while cur and cur.id not in seen:
            seen.add(cur.id)
            names.insert(0, cur.name)
            cur = module_map.get(cur.parent_id) if cur.parent_id else None
        return names

    out = []
    for link in links:
        latest = link.results[-1] if link.results else None
        case_obj = case_map.get(link.local_case_id) if link.local_case_id else None
        steps_json = []
        if case_obj and case_obj.steps:
            steps_json = case_obj.steps if isinstance(case_obj.steps, list) else []
        module_id = case_obj.module_id if case_obj else None
        out.append({
            "id": link.id,
            "local_case_id": link.local_case_id,
            "zentao_case_id": link.zentao_case_id,
            "title": link.case_title,
            "type": link.case_type,
            "priority": link.case_priority,
            "precondition": case_obj.precondition if case_obj else None,
            "steps": steps_json,
            "created_by": case_obj.created_by if case_obj else None,
            "module_id": module_id,
            "module_path": build_module_path(module_id) if module_id else [],
            "latest_result": ({
                "status": latest.status,
                "comment": latest.comment,
                "step_results": latest.step_results,
                "executor_id": latest.executor_id,
                "executed_at": (latest.executed_at.isoformat() if latest.executed_at else None),
            } if latest else None),
            "result_count": len(link.results),
        })
    return out


@router.delete("/{task_id}/cases/{link_id}")
async def unlink_case(
    task_id: int,
    link_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取消关联用例

    权限（2026-11-25 补）：测试设计(2) 或 执行测试(7) 的负责人/执行人。
    此前该接口**完全没有权限校验**，任何登录用户都能解除任意任务的用例关联。
    """
    await assert_can_operate_any(db, task_id, (2, 7), user, "解除用例关联")
    stmt = select(TaskCaseLink).where(
        TaskCaseLink.id == link_id,
        TaskCaseLink.task_id == task_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="关联记录不存在")
    # 先清执行结果再删关联：case_results.link_id 是 NOT NULL 且无级联，
    # 直接 ORM 删关联会去把子表外键置空 → NotNullViolation → 500。
    # （2026-11-25 实测：只要该用例已有执行结果，解除关联必崩）
    title = link.case_title
    await db.execute(delete(CaseResult).where(CaseResult.link_id == link_id))
    await db.execute(delete(TaskCaseLink).where(TaskCaseLink.id == link_id))
    await db.commit()
    await TaskService(db)._log_action(task_id, "case_unlinked", user.id,
                                      f"解除用例关联：{title}")
    await db.commit()
    return {"ok": True}


@router.post("/{task_id}/cases/{link_id}/result")
async def record_result(
    task_id: int,
    link_id: int,
    data: CaseResultReq,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """记录用例执行结果

    权限：仅环节7「执行测试」的操作人（主执行人 + 多人投入列表），
    **且**这条用例如果在步骤6 被指派给了某个人，只有被指派人可以执行（2026-11-26 用户定夺）。
    此前把逐条指派当成"负责人标识"，任何执行人都能替别人执行
    （预发任务133 实测：分配给李东坡的用例被主执行人点了通过）。未指派的用例仍由全体执行人共担。
    """
    if data.status not in CASE_STATUSES:
        raise HTTPException(status_code=400, detail=f"状态必须为 {'/'.join(CASE_STATUSES)}")

    await assert_can_operate(db, task_id, 7, user, "记录用例执行结果")
    assignee = await _case_assignee(db, task_id, link_id)
    if assignee is not None and assignee != user.id:
        raise HTTPException(
            status_code=403,
            detail=f"该用例已分配给 {await _user_name(db, assignee)}，仅被分配人可执行",
        )

    stmt = select(TaskCaseLink).where(
        TaskCaseLink.id == link_id,
        TaskCaseLink.task_id == task_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="关联记录不存在")

    # 整体状态以步骤结果推导为准：有步骤未执行不能判通过（2026-11-26 用户反馈）
    steps: list = []
    if link.local_case_id:
        case_obj = (await db.execute(
            select(TestCase).where(TestCase.id == link.local_case_id)
        )).scalar_one_or_none()
        if case_obj and isinstance(case_obj.steps, list):
            steps = case_obj.steps
    overall, err = _derive_case_status(steps, data.step_results, data.status)
    if err:
        raise HTTPException(status_code=400, detail=err)

    cr = CaseResult(
        link_id=link_id,
        status=overall or data.status,
        comment=data.comment,
        executor_id=user.id,
        step_results=data.step_results,
    )
    db.add(cr)
    await db.commit()
    await db.refresh(cr)
    return {"ok": True, "id": cr.id, "status": cr.status}


@router.delete("/{task_id}/cases/{link_id}/result")
async def reset_case_result(
    task_id: int,
    link_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把一条用例恢复为「未执行」（清除其全部执行结果，恢复为待执行）

    背景（2026-11-26 用户反馈）：执行结果只追加、不能清除，手滑点了「通过」就没法恢复原状。
    前端没有单独的「重置」按钮（用户定夺：每步的「未执行」就够用）——本接口只在
    「弹窗内所有步骤都被标回未执行后点确认」时被调用。
    做法是删除该用例的 case_results（列表/统计/导出都按"最新一条"取状态，删掉即回到待执行），
    同时把被清除的结果摘要写进任务操作历史（action=case_result_reset），审计不丢。
    不动步骤附件与已登记的问题单。

    权限：环节7 的操作人，且满足其一——用例未指派 / 我是这条用例的被指派人 /
    最近一次结果是我记的（自己点错的自己能撤）。
    """
    await assert_can_operate(db, task_id, 7, user, "重置用例执行结果")
    link = (await db.execute(
        select(TaskCaseLink).options(selectinload(TaskCaseLink.results)).where(
            TaskCaseLink.id == link_id,
            TaskCaseLink.task_id == task_id,
        )
    )).scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="关联记录不存在")
    if not link.results:
        return {"ok": True, "deleted": 0}

    latest = link.results[-1]
    assignee = await _case_assignee(db, task_id, link_id)
    if assignee is not None and assignee != user.id and latest.executor_id != user.id:
        raise HTTPException(
            status_code=403,
            detail=f"该用例已分配给 {await _user_name(db, assignee)}，仅被分配人或本次结果的记录人可重置",
        )

    n = len(link.results)
    when = ""
    if latest.executed_at:
        dt = latest.executed_at.astimezone(_CN_TZ) if latest.executed_at.tzinfo else latest.executed_at
        when = "，" + dt.strftime("%Y-%m-%d %H:%M")
    summary = (f"{_CASE_STATUS_ZH.get(latest.status, latest.status)}，"
               f"执行人：{await _user_name(db, latest.executor_id)}{when}")
    await db.execute(delete(CaseResult).where(CaseResult.link_id == link_id))
    await db.commit()
    await TaskService(db)._log_action(
        task_id, "case_result_reset", user.id,
        f"用例执行结果重置为未执行：{link.case_title}（清除 {n} 条记录；最近一次：{summary}）",
    )
    await db.commit()
    return {"ok": True, "deleted": n}


@router.get("/{task_id}/cases/stats")
async def case_stats(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用例执行统计。

    按读取口径 effective_case_status 计数：历史「通过但步骤没测完」的旧结果计入 partial，不算通过。
    """
    links = (await db.execute(
        select(TaskCaseLink).options(selectinload(TaskCaseLink.results)).where(TaskCaseLink.task_id == task_id)
    )).scalars().all()
    case_ids = [l.local_case_id for l in links if l.local_case_id]
    steps_map = {}
    if case_ids:
        for c in (await db.execute(select(TestCase).where(TestCase.id.in_(case_ids)))).scalars().all():
            steps_map[c.id] = c.steps if isinstance(c.steps, list) else []

    total = len(links)
    counts = {"pass": 0, "fail": 0, "block": 0, "na": 0, "partial": 0}
    for l in links:
        if not l.results:
            continue
        last = l.results[-1]
        st = effective_case_status(last.status, last.step_results, steps_map.get(l.local_case_id) or [])
        if st in counts:
            counts[st] += 1
    pending = total - sum(counts.values())

    return {
        "total": total,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "blocked": counts["block"],
        "na": counts["na"],
        "partial": counts["partial"],
        "pending": pending,
        "pass_rate": round(counts["pass"] / total * 100, 1) if total else 0,
    }
