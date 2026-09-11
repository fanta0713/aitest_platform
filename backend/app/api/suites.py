"""
API路由 - 用例套件（收集固定用例，测试设计环节可一键批量关联到任务）
权限：PL / TSE / 管理员可增删改，其他成员只读
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, and_
from datetime import datetime

from app.db.database import get_db
from app.core.security import get_current_user, require_pp_manager
from app.models.models import User, CaseSuite, SuiteCase, TestCase, CaseModule, TaskCaseLink

router = APIRouter(prefix="/api/suites", tags=["用例套件"])


class SuiteCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="套件名称")
    description: Optional[str] = None


class SuiteUpdateReq(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None


class SuiteCaseIdsReq(BaseModel):
    case_ids: List[int] = Field(..., description="用例ID列表")


async def _get_suite_or_404(suite_id: int, db: AsyncSession) -> CaseSuite:
    suite = (await db.execute(select(CaseSuite).where(CaseSuite.id == suite_id))).scalar_one_or_none()
    if not suite:
        raise HTTPException(status_code=404, detail="套件不存在")
    return suite


@router.get("")
async def list_suites(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """套件列表（含用例数量），所有登录用户可查看"""
    rows = await db.execute(
        select(CaseSuite, func.count(SuiteCase.id).label("cnt"), User.realname)
        .outerjoin(SuiteCase, SuiteCase.suite_id == CaseSuite.id)
        .outerjoin(User, User.id == CaseSuite.created_by)
        .group_by(CaseSuite.id, User.realname)
        .order_by(CaseSuite.id)
    )
    out = []
    for suite, cnt, creator_name in rows.all():
        out.append({
            "id": suite.id,
            "name": suite.name,
            "description": suite.description,
            "case_count": cnt or 0,
            "created_by": suite.created_by,
            "created_by_name": creator_name,
            "created_at": suite.created_at.isoformat() if suite.created_at else None,
        })
    return out


@router.post("")
async def create_suite(
    data: SuiteCreateReq,
    user: User = Depends(require_pp_manager),
    db: AsyncSession = Depends(get_db),
):
    """新建套件（PL/TSE/管理员）"""
    dup = (await db.execute(select(CaseSuite).where(CaseSuite.name == data.name))).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=400, detail=f"套件名称「{data.name}」已存在")
    suite = CaseSuite(name=data.name, description=data.description, created_by=user.id)
    db.add(suite)
    await db.commit()
    await db.refresh(suite)
    return {"ok": True, "id": suite.id, "name": suite.name}


@router.put("/{suite_id}")
async def update_suite(
    suite_id: int,
    data: SuiteUpdateReq,
    user: User = Depends(require_pp_manager),
    db: AsyncSession = Depends(get_db),
):
    """编辑套件（PL/TSE/管理员）"""
    suite = await _get_suite_or_404(suite_id, db)
    if data.name is not None and data.name != suite.name:
        dup = (await db.execute(
            select(CaseSuite).where(CaseSuite.name == data.name, CaseSuite.id != suite_id)
        )).scalar_one_or_none()
        if dup:
            raise HTTPException(status_code=400, detail=f"套件名称「{data.name}」已存在")
        suite.name = data.name
    if data.description is not None:
        suite.description = data.description
    suite.updated_at = datetime.now()
    await db.commit()
    return {"ok": True, "id": suite.id, "name": suite.name}


@router.delete("/{suite_id}")
async def delete_suite(
    suite_id: int,
    user: User = Depends(require_pp_manager),
    db: AsyncSession = Depends(get_db),
):
    """删除套件（PL/TSE/管理员），同时清除套件内的用例关联"""
    suite = await _get_suite_or_404(suite_id, db)
    await db.execute(delete(SuiteCase).where(SuiteCase.suite_id == suite_id))
    await db.delete(suite)
    await db.commit()
    return {"ok": True, "id": suite_id}


@router.get("/{suite_id}/cases")
async def list_suite_cases(
    suite_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """套件内用例列表（含模块路径）"""
    await _get_suite_or_404(suite_id, db)
    rows = await db.execute(
        select(SuiteCase, TestCase, CaseModule.name)
        .join(TestCase, TestCase.id == SuiteCase.case_id)
        .outerjoin(CaseModule, CaseModule.id == TestCase.module_id)
        .where(SuiteCase.suite_id == suite_id)
        .order_by(SuiteCase.id)
    )
    rows = rows.all()
    # 加载模块父链
    module_ids = {c.module_id for _, c, _ in rows if c.module_id}
    module_map = {}
    if module_ids:
        to_fetch = set(module_ids)
        loaded = set()
        while to_fetch:
            mods = (await db.execute(
                select(CaseModule).where(CaseModule.id.in_(to_fetch))
            )).scalars().all()
            for m in mods:
                module_map[m.id] = m
                loaded.add(m.id)
            to_fetch = {
                m.parent_id for m in mods
                if m.parent_id and m.parent_id not in loaded
            }

    def build_path(mid):
        names = []
        cur = module_map.get(mid)
        while cur:
            names.insert(0, cur.name)
            cur = module_map.get(cur.parent_id) if cur.parent_id else None
        return names

    return [
        {
            "link_id": sc.id,
            "id": c.id,
            "title": c.title,
            "module_name": mod_name or "—",
            "module_path": build_path(c.module_id) if c.module_id else [],
            "pri": c.pri,
            "case_type": c.case_type,
            "auto": c.auto,
            "created_at": sc.created_at.isoformat() if sc.created_at else None,
        }
        for sc, c, mod_name in rows
    ]


@router.post("/{suite_id}/cases")
async def add_cases_to_suite(
    suite_id: int,
    data: SuiteCaseIdsReq,
    user: User = Depends(require_pp_manager),
    db: AsyncSession = Depends(get_db),
):
    """套件内关联用例（PL/TSE/管理员），已存在的自动跳过"""
    await _get_suite_or_404(suite_id, db)
    added = 0
    for cid in data.case_ids:
        case = (await db.execute(select(TestCase).where(TestCase.id == cid))).scalar_one_or_none()
        if not case:
            continue
        exists = (await db.execute(
            select(SuiteCase).where(SuiteCase.suite_id == suite_id, SuiteCase.case_id == cid)
        )).scalar_one_or_none()
        if exists:
            continue
        db.add(SuiteCase(suite_id=suite_id, case_id=cid))
        added += 1
    await db.commit()
    return {"ok": True, "added": added}


@router.delete("/{suite_id}/cases/{case_id}")
async def remove_case_from_suite(
    suite_id: int,
    case_id: int,
    user: User = Depends(require_pp_manager),
    db: AsyncSession = Depends(get_db),
):
    """取消套件内的某个用例关联（PL/TSE/管理员）"""
    await _get_suite_or_404(suite_id, db)
    await db.execute(delete(SuiteCase).where(
        and_(SuiteCase.suite_id == suite_id, SuiteCase.case_id == case_id)
    ))
    await db.commit()
    return {"ok": True, "suite_id": suite_id, "case_id": case_id}


@router.post("/{suite_id}/link-to-task/{task_id}")
async def link_suite_to_task(
    suite_id: int,
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按套件批量关联用例到任务（测试设计环节用），已关联的自动跳过"""
    await _get_suite_or_404(suite_id, db)
    rows = await db.execute(
        select(TestCase).join(SuiteCase, SuiteCase.case_id == TestCase.id)
        .where(SuiteCase.suite_id == suite_id)
        .order_by(TestCase.id)
    )
    cases = rows.scalars().all()
    if not cases:
        return {"ok": True, "created": 0, "skipped": 0, "message": "该套件内没有用例"}

    created = 0
    skipped = 0
    for c in cases:
        existing = (await db.execute(
            select(TaskCaseLink).where(TaskCaseLink.task_id == task_id, TaskCaseLink.local_case_id == c.id)
        )).scalar_one_or_none()
        if existing:
            skipped += 1
            continue
        db.add(TaskCaseLink(
            task_id=task_id,
            local_case_id=c.id,
            zentao_case_id=None,
            case_title=c.title,
            case_type=c.case_type,
            case_priority=c.pri,
            linked_by=user.id,
        ))
        created += 1
    await db.commit()
    return {"ok": True, "created": created, "skipped": skipped}
