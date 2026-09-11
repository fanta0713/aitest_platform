"""
API路由 - 用例库管理（完整CRUD + Excel导出 + 批量操作）
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, update
from sqlalchemy.orm import selectinload
from datetime import datetime
import io
import json
import csv

from app.db.database import get_db
from app.core.security import get_current_user, _user_roles
from app.models.models import (
    User, CaseLibrary, CaseModule, TestCase, TaskCaseLink, CaseResult, TaskIssue
)

router = APIRouter(prefix="/api/cases", tags=["用例库"])


async def _delete_cases_cascade(db: AsyncSession, case_ids: List[int]) -> int:
    """删除用例并清理其全部下游关联数据。

    test_cases 被 task_case_links.local_case_id 引用（外键 NO ACTION），
    直接 delete 会抛 ForeignKeyViolationError → 500。必须先清下游：
      case_results(link_id) → task_issues.case_link_id(置空) → task_case_links → test_cases
    （suite_cases 是 ON DELETE CASCADE，数据库会自己清）
    """
    ids = [int(i) for i in (case_ids or []) if i]
    if not ids:
        return 0
    link_ids = (await db.execute(
        select(TaskCaseLink.id).where(TaskCaseLink.local_case_id.in_(ids))
    )).scalars().all()
    if link_ids:
        await db.execute(delete(CaseResult).where(CaseResult.link_id.in_(link_ids)))
        await db.execute(
            update(TaskIssue).where(TaskIssue.case_link_id.in_(link_ids)).values(case_link_id=None)
        )
    await db.execute(delete(TaskCaseLink).where(TaskCaseLink.local_case_id.in_(ids)))
    await db.execute(delete(TestCase).where(TestCase.id.in_(ids)))
    return len(ids)


# ============ 用例库 ============
class LibraryCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""


@router.get("/libraries")
async def list_libraries(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(CaseLibrary).order_by(CaseLibrary.id)
    result = await db.execute(stmt)
    libs = result.scalars().all()
    out = []
    for lib in libs:
        cnt_stmt = select(func.count(TestCase.id)).where(TestCase.library_id == lib.id)
        cnt = (await db.execute(cnt_stmt)).scalar()
        out.append({"id": lib.id, "name": lib.name, "description": lib.description, "case_count": cnt})
    return out


@router.post("/libraries")
async def create_library(data: LibraryCreateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    lib = CaseLibrary(name=data.name, description=data.description)
    db.add(lib)
    await db.commit()
    await db.refresh(lib)
    return {"id": lib.id, "name": lib.name}


class LibraryUpdateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""


@router.put("/libraries/{lib_id}")
async def update_library(lib_id: int, data: LibraryUpdateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(CaseLibrary).where(CaseLibrary.id == lib_id)
    result = await db.execute(stmt)
    lib = result.scalar_one_or_none()
    if not lib:
        raise HTTPException(status_code=404, detail="用例库不存在")
    lib.name = data.name
    lib.description = data.description
    await db.commit()
    return {"id": lib.id, "name": lib.name}


@router.delete("/libraries/{lib_id}")
async def delete_library(lib_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    roles = set(_user_roles(user))
    if not (roles & {"admin", "pl", "tse"}):
        raise HTTPException(status_code=403, detail="仅PL/TSE/管理员可删除用例库")
    stmt = select(CaseLibrary).where(CaseLibrary.id == lib_id)
    result = await db.execute(stmt)
    lib = result.scalar_one_or_none()
    if not lib:
        raise HTTPException(status_code=404, detail="用例库不存在")
    # 级联删除用例（含任务关联/执行结果/问题单引用）和模块
    case_ids = (await db.execute(
        select(TestCase.id).where(TestCase.library_id == lib_id)
    )).scalars().all()
    if case_ids:
        await _delete_cases_cascade(db, list(case_ids))
    await db.execute(delete(CaseModule).where(CaseModule.library_id == lib_id))
    await db.delete(lib)
    await db.commit()
    return {"ok": True}


# ============ 模块 ============
class ModuleCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    parent_id: Optional[int] = None


class ModuleUpdateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


async def _get_descendant_ids(db: AsyncSession, lib_id: int, parent_id: int) -> list:
    """递归获取 parent_id 及其全部子孙模块ID（BFS）"""
    result_ids = [parent_id]
    queue = [parent_id]
    while queue:
        batch = await db.execute(
            select(CaseModule.id).where(
                CaseModule.library_id == lib_id,
                CaseModule.parent_id.in_(queue)
            )
        )
        child_ids = batch.scalars().all()
        result_ids.extend(child_ids)
        queue = child_ids
    return result_ids


@router.get("/libraries/{lib_id}/modules")
async def list_modules(lib_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(CaseModule).where(CaseModule.library_id == lib_id).order_by(CaseModule.sort, CaseModule.id)
    result = await db.execute(stmt)
    mods = result.scalars().all()
    # 预计算每个模块的直接用例数和递归用例数
    out = []
    for m in mods:
        direct_cnt = (await db.execute(select(func.count(TestCase.id)).where(TestCase.module_id == m.id))).scalar()
        desc_ids = await _get_descendant_ids(db, lib_id, m.id)
        total_cnt = (await db.execute(select(func.count(TestCase.id)).where(TestCase.module_id.in_(desc_ids)))).scalar()
        out.append({"id": m.id, "name": m.name, "parent_id": m.parent_id, "sort": m.sort,
                     "case_count": direct_cnt, "total_case_count": total_cnt})
    no_mod_cnt = (await db.execute(
        select(func.count(TestCase.id)).where(TestCase.library_id == lib_id, TestCase.module_id.is_(None))
    )).scalar()
    return {"modules": out, "uncategorized_count": no_mod_cnt}


@router.post("/libraries/{lib_id}/modules")
async def create_module(lib_id: int, data: ModuleCreateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    mod = CaseModule(library_id=lib_id, name=data.name, parent_id=data.parent_id)
    db.add(mod)
    await db.commit()
    await db.refresh(mod)
    return {"id": mod.id, "name": mod.name, "parent_id": mod.parent_id}


@router.put("/modules/{mod_id}")
async def update_module(mod_id: int, data: ModuleUpdateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(CaseModule).where(CaseModule.id == mod_id)
    result = await db.execute(stmt)
    mod = result.scalar_one_or_none()
    if not mod:
        raise HTTPException(status_code=404, detail="模块不存在")
    mod.name = data.name
    await db.commit()
    return {"id": mod.id, "name": mod.name}


@router.delete("/modules/{mod_id}")
async def delete_module(mod_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(CaseModule).where(CaseModule.id == mod_id)
    result = await db.execute(stmt)
    mod = result.scalar_one_or_none()
    if not mod:
        raise HTTPException(status_code=404, detail="模块不存在")
    # 级联: 找到全部子孙模块, 把用例设为无模块, 再删模块
    all_ids = await _get_descendant_ids(db, mod.library_id, mod_id)
    await db.execute(
        update(TestCase).where(TestCase.module_id.in_(all_ids)).values(module_id=None)
    )
    await db.execute(delete(CaseModule).where(CaseModule.id.in_(all_ids)))
    await db.commit()
    return {"ok": True, "deleted_modules": len(all_ids)}


# ============ 用例 ============
class CaseCreateReq(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    module_id: Optional[int] = None
    precondition: str = ""
    steps: List[dict] = []
    pri: int = 3
    case_type: str = "feature"
    stage: str = "feature"
    auto: str = "no"
    keywords: str = ""


async def _load_module_map_and_path_builder(db, case_objs):
    """加载用例涉及的所有模块（含父链），返回 (module_map, build_path_func)"""
    module_ids = {c.module_id for c in case_objs if c.module_id}
    module_map = {}
    if module_ids:
        to_fetch = set(module_ids)
        loaded = set()
        while to_fetch:
            rows = (await db.execute(
                select(CaseModule).where(CaseModule.id.in_(to_fetch))
            )).scalars().all()
            for m in rows:
                module_map[m.id] = m
                loaded.add(m.id)
            to_fetch = {
                m.parent_id for m in rows
                if m.parent_id and m.parent_id not in loaded
            }

    def build_path(mid):
        names = []
        cur = module_map.get(mid)
        while cur:
            names.insert(0, cur.name)
            cur = module_map.get(cur.parent_id) if cur.parent_id else None
        return names

    return module_map, build_path


@router.get("/libraries/{lib_id}/cases")
async def list_cases(
    lib_id: int,
    module_id: Optional[int] = Query(None),
    keyword: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestCase).where(TestCase.library_id == lib_id)
    if module_id is not None:
        if module_id == 0:
            stmt = stmt.where(TestCase.module_id.is_(None))
        else:
            # 递归: 包含该模块及其全部子模块的用例
            desc_ids = await _get_descendant_ids(db, lib_id, module_id)
            stmt = stmt.where(TestCase.module_id.in_(desc_ids))
    if keyword:
        stmt = stmt.where(TestCase.title.ilike(f"%{keyword}%"))
    stmt = stmt.order_by(TestCase.id.desc())
    result = await db.execute(stmt)
    cases = result.scalars().all()
    module_map, build_path = await _load_module_map_and_path_builder(db, cases)
    return [
        {"id": c.id, "title": c.title, "module_id": c.module_id,
         "module_name": module_map[c.module_id].name if c.module_id in module_map else "—",
         "module_path": build_path(c.module_id),
         "pri": c.pri, "case_type": c.case_type, "stage": c.stage, "auto": c.auto,
         "precondition": c.precondition, "steps": c.steps, "keywords": c.keywords,
         "status": c.status, "updated_at": str(c.updated_at) if c.updated_at else None}
        for c in cases
    ]


@router.post("/libraries/{lib_id}/cases")
async def create_case(lib_id: int, data: CaseCreateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = TestCase(
        library_id=lib_id, module_id=data.module_id, title=data.title,
        precondition=data.precondition, steps=data.steps, pri=data.pri,
        case_type=data.case_type, stage=data.stage, auto=data.auto,
        keywords=data.keywords, created_by=user.id,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id, "title": c.title}


@router.put("/cases/{case_id}")
async def update_case(case_id: int, data: CaseCreateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await db.execute(stmt)
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="用例不存在")
    c.title = data.title
    c.module_id = data.module_id
    c.precondition = data.precondition
    c.steps = data.steps
    c.pri = data.pri
    c.case_type = data.case_type
    c.stage = data.stage
    c.auto = data.auto
    c.keywords = data.keywords
    await db.commit()
    return {"ok": True}


@router.delete("/cases/{case_id}")
async def delete_case(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await db.execute(stmt)
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="用例不存在")
    # 统计被多少任务引用（删除后这些关联会一并移除）
    linked = (await db.execute(
        select(func.count(TaskCaseLink.id)).where(TaskCaseLink.local_case_id == case_id)
    )).scalar() or 0
    await _delete_cases_cascade(db, [case_id])
    await db.commit()
    return {"ok": True, "unlinked": linked}


# ============ 批量操作 ============
class BatchDeleteReq(BaseModel):
    case_ids: List[int] = Field(..., description="用例ID列表")


@router.post("/cases/batch-delete")
async def batch_delete_cases(data: BatchDeleteReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not data.case_ids:
        return {"ok": True, "deleted": 0}
    deleted = await _delete_cases_cascade(db, data.case_ids)
    await db.commit()
    return {"ok": True, "deleted": deleted}


class BatchUpdateReq(BaseModel):
    case_ids: List[int] = Field(..., description="用例ID列表")
    module_id: Optional[int] = None
    pri: Optional[int] = None
    case_type: Optional[str] = None
    stage: Optional[str] = None
    auto: Optional[str] = None


@router.post("/cases/batch-update")
async def batch_update_cases(data: BatchUpdateReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not data.case_ids:
        return {"ok": True, "updated": 0}
    vals = {}
    if data.module_id is not None:
        vals["module_id"] = data.module_id
    if data.pri is not None:
        vals["pri"] = data.pri
    if data.case_type is not None:
        vals["case_type"] = data.case_type
    if data.stage is not None:
        vals["stage"] = data.stage
    if data.auto is not None:
        vals["auto"] = data.auto
    if vals:
        await db.execute(update(TestCase).where(TestCase.id.in_(data.case_ids)).values(**vals))
        await db.commit()
    return {"ok": True, "updated": len(data.case_ids)}


class CopyCasesReq(BaseModel):
    case_ids: List[int] = Field(..., description="要复制的用例ID列表")
    target_library_id: int = Field(..., description="目标用例库ID")
    target_module_id: Optional[int] = Field(None, description="目标模块ID")


@router.post("/cases/copy")
async def copy_cases(data: CopyCasesReq, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """复制用例到指定用例库+模块"""
    if not data.case_ids:
        return {"ok": True, "copied": 0}
    stmt = select(TestCase).where(TestCase.id.in_(data.case_ids))
    result = await db.execute(stmt)
    cases = result.scalars().all()
    for c in cases:
        new_c = TestCase(
            library_id=data.target_library_id,
            module_id=data.target_module_id,
            title=c.title,
            precondition=c.precondition,
            steps=c.steps,
            pri=c.pri,
            case_type=c.case_type,
            stage=c.stage,
            auto=c.auto,
            keywords=c.keywords,
            status="normal",
            created_by=user.id,
        )
        db.add(new_c)
    await db.commit()
    return {"ok": True, "copied": len(cases)}


# ============ Excel导出 ============
@router.get("/libraries/{lib_id}/export")
async def export_cases(lib_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """导出用例为CSV(Excel兼容)"""
    # 查用例库名
    lib = await db.get(CaseLibrary, lib_id)
    lib_name = lib.name if lib else "cases"

    # 查所有模块
    mod_stmt = select(CaseModule).where(CaseModule.library_id == lib_id)
    mods = {(await db.execute(mod_stmt)).scalars().all()}
    mod_map = {m.id: m.name for m in mods}

    # 查所有用例
    stmt = select(TestCase).where(TestCase.library_id == lib_id).order_by(TestCase.module_id, TestCase.id)
    cases = (await db.execute(stmt)).scalars().all()

    # 生成CSV
    output = io.StringIO()
    # BOM for Excel UTF-8
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["模块", "用例标题", "前置条件", "步骤", "预期结果", "优先级", "类型", "阶段", "自动化", "关键词"])

    for c in cases:
        mod_name = mod_map.get(c.module_id, "")
        if c.steps:
            for i, step in enumerate(c.steps):
                writer.writerow([
                    mod_name, c.title if i == 0 else "",
                    c.precondition if i == 0 else "",
                    step.get("desc", ""), step.get("expect", ""),
                    c.pri if i == 0 else "", c.case_type if i == 0 else "",
                    c.stage if i == 0 else "", c.auto if i == 0 else "",
                    c.keywords if i == 0 else "",
                ])
        else:
            writer.writerow([mod_name, c.title, c.precondition, "", "", c.pri, c.case_type, c.stage, c.auto, c.keywords])

    output.seek(0)
    filename = f"{lib_name}_用例导出_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ============ 按模块批量关联到任务 ============
class LinkByModuleReq(BaseModel):
    module_ids: List[int] = Field(default=[], description="模块ID列表(如需含子模块,前端需传全部子孙ID)")
    case_ids: List[int] = Field(default=[], description="用例ID列表")


@router.post("/link-to-task/{task_id}")
async def link_cases_by_module(
    task_id: int,
    data: LinkByModuleReq,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量关联用例到任务（按模块 或 按用例ID，两者可混用）"""
    from sqlalchemy import or_
    conds = []
    if data.module_ids:
        conds.append(TestCase.module_id.in_(data.module_ids))
    if data.case_ids:
        conds.append(TestCase.id.in_(data.case_ids))
    if not conds:
        return {"ok": True, "created": 0}

    stmt = select(TestCase).where(or_(*conds))
    result = await db.execute(stmt)
    cases = result.scalars().all()

    created = 0
    for c in cases:
        # 检查是否已关联
        existing = await db.execute(
            select(TaskCaseLink).where(TaskCaseLink.task_id == task_id, TaskCaseLink.local_case_id == c.id)
        )
        if existing.scalar_one_or_none():
            continue
        link = TaskCaseLink(
            task_id=task_id,
            local_case_id=c.id,
            zentao_case_id=None,
            case_title=c.title,
            case_type=c.case_type,
            case_priority=c.pri,
            linked_by=user.id,
        )
        db.add(link)
        created += 1

    await db.commit()
    return {"ok": True, "created": created}
