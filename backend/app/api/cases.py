"""
API路由 - 测试用例关联与执行结果（本地用例库，不依赖禅道）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from app.db.database import get_db
from app.core.security import get_current_user
from app.models.models import User, TestTask, TaskCaseLink, CaseResult, TestCase, CaseModule

router = APIRouter(prefix="/api/tasks", tags=["测试用例"])


class CaseResultReq(BaseModel):
    status: str = Field(..., description="pass/fail/block/na")
    comment: str = ""
    step_results: Optional[List[dict]] = None  # [{step_idx, actual, status}] 步骤级结果


@router.get("/{task_id}/cases")
async def list_linked_cases(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取任务已关联的用例列表（含最新执行结果）"""
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
        names = []
        cur = module_map.get(mid)
        while cur:
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
    """取消关联用例"""
    stmt = select(TaskCaseLink).where(
        TaskCaseLink.id == link_id,
        TaskCaseLink.task_id == task_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="关联记录不存在")
    await db.delete(link)
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
    """记录用例执行结果"""
    if data.status not in ("pass", "fail", "block", "na"):
        raise HTTPException(status_code=400, detail="状态必须为 pass/fail/block/na")

    stmt = select(TaskCaseLink).where(
        TaskCaseLink.id == link_id,
        TaskCaseLink.task_id == task_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="关联记录不存在")

    cr = CaseResult(
        link_id=link_id,
        status=data.status,
        comment=data.comment,
        executor_id=user.id,
        step_results=data.step_results,
    )
    db.add(cr)
    await db.commit()
    await db.refresh(cr)
    return {"ok": True, "id": cr.id, "status": cr.status}


@router.get("/{task_id}/cases/stats")
async def case_stats(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用例执行统计"""
    stmt = select(TaskCaseLink).options(
        selectinload(TaskCaseLink.results)
    ).where(TaskCaseLink.task_id == task_id)
    result = await db.execute(stmt)
    links = result.scalars().all()

    total = len(links)
    passed = sum(1 for l in links if l.results and l.results[-1].status == "pass")
    failed = sum(1 for l in links if l.results and l.results[-1].status == "fail")
    blocked = sum(1 for l in links if l.results and l.results[-1].status == "block")
    na = sum(1 for l in links if l.results and l.results[-1].status == "na")
    pending = total - passed - failed - blocked - na

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "na": na,
        "pending": pending,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
    }
