"""
API路由 - 产品与项目管理（本地，不依赖禅道）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sa_delete, func
from datetime import date

from app.db.database import get_db
from app.core.security import get_current_user, require_admin, require_pp_manager
from app.models.models import User, Product, Project, TestTask

router = APIRouter(prefix="/api", tags=["产品与项目"])


class ProductCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    code: str = ""
    description: str = ""


class ProjectCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: str = ""
    product_id: Optional[int] = None
    begin_date: Optional[date] = None
    end_date: Optional[date] = None


# ============ 产品 CRUD ============
@router.get("/products")
async def list_products(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(Product).order_by(Product.id)
    result = await db.execute(stmt)
    products = result.scalars().all()
    out = []
    for p in products:
        # 统计该产品下的项目数
        cnt = (await db.execute(
            select(func.count(Project.id)).where(Project.product_id == p.id)
        )).scalar()
        out.append({"id": p.id, "name": p.name, "code": p.code,
                     "description": p.description, "project_count": cnt})
    return out


@router.post("/products")
async def create_product(data: ProductCreateReq, user: User = Depends(require_pp_manager), db: AsyncSession = Depends(get_db)):
    p = Product(name=data.name, code=data.code or None, description=data.description or None)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {"id": p.id, "name": p.name}


@router.put("/products/{pid}")
async def update_product(pid: int, data: ProductCreateReq, user: User = Depends(require_pp_manager), db: AsyncSession = Depends(get_db)):
    stmt = select(Product).where(Product.id == pid)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="产品不存在")
    p.name = data.name
    p.code = data.code or None
    p.description = data.description or None
    await db.commit()
    return {"id": p.id, "name": p.name}


@router.delete("/products/{pid}")
async def delete_product(pid: int, user: User = Depends(require_pp_manager), db: AsyncSession = Depends(get_db)):
    stmt = select(Product).where(Product.id == pid)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="产品不存在")
    # 检查是否有关联项目
    proj_cnt = (await db.execute(
        select(func.count(Project.id)).where(Project.product_id == pid)
    )).scalar()
    if proj_cnt > 0:
        raise HTTPException(status_code=400, detail=f"该产品下还有 {proj_cnt} 个项目，无法删除。请先删除或迁移项目。")
    await db.delete(p)
    await db.commit()
    return {"ok": True}


# ============ 项目 CRUD ============
@router.get("/projects")
async def list_projects(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(Project).order_by(Project.id)
    result = await db.execute(stmt)
    projects = result.scalars().all()
    # 批量查产品名
    prod_ids = {p.product_id for p in projects if p.product_id}
    prod_map = {}
    if prod_ids:
        prod_result = await db.execute(select(Product).where(Product.id.in_(prod_ids)))
        for prod in prod_result.scalars().all():
            prod_map[prod.id] = prod.name
    out = []
    for p in projects:
        # 统计关联的测试任务数
        task_cnt = (await db.execute(
            select(func.count(TestTask.id)).where(TestTask.project_id == p.id)
        )).scalar()
        out.append({
            "id": p.id, "name": p.name, "code": p.code, "status": p.status,
            "product_id": p.product_id, "product_name": prod_map.get(p.product_id, ""),
            "begin_date": str(p.begin_date) if p.begin_date else None,
            "end_date": str(p.end_date) if p.end_date else None,
            "task_count": task_cnt,
        })
    return out


@router.post("/projects")
async def create_project(data: ProjectCreateReq, user: User = Depends(require_pp_manager), db: AsyncSession = Depends(get_db)):
    p = Project(
        name=data.name,
        code=data.code or None,
        product_id=data.product_id,
        begin_date=data.begin_date,
        end_date=data.end_date,
        status="doing",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {"id": p.id, "name": p.name}


@router.put("/projects/{pid}")
async def update_project(pid: int, data: ProjectCreateReq, user: User = Depends(require_pp_manager), db: AsyncSession = Depends(get_db)):
    stmt = select(Project).where(Project.id == pid)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="项目不存在")
    p.name = data.name
    p.code = data.code or None
    p.product_id = data.product_id
    p.begin_date = data.begin_date
    p.end_date = data.end_date
    await db.commit()
    return {"id": p.id, "name": p.name}


@router.delete("/projects/{pid}")
async def delete_project(pid: int, user: User = Depends(require_pp_manager), db: AsyncSession = Depends(get_db)):
    stmt = select(Project).where(Project.id == pid)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="项目不存在")
    # 检查是否有关联测试任务（只统计未软删除的任务）
    task_cnt = (await db.execute(
        select(func.count(TestTask.id)).where(TestTask.project_id == pid, TestTask.deleted == False)
    )).scalar()
    if task_cnt > 0:
        raise HTTPException(status_code=400, detail=f"该项目下还有 {task_cnt} 个测试任务，无法删除。请先删除或迁移任务。")
    await db.delete(p)
    await db.commit()
    return {"ok": True}
