"""
API路由 - 禅道产品与项目对接
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from datetime import date

from app.core.security import get_current_user
from app.core.config import get_settings
from app.core.zentao import ZentaoClient
from app.services.user_service import UserService
from app.models.models import User, Project

router = APIRouter(prefix="/api/zentao", tags=["禅道产品项目"])


class ProductCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: str = ""
    product_type: str = "normal"
    desc: str = ""


class ProjectCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: str = ""
    model: str = "waterfall"
    products: list  # list of zentao product IDs
    begin: Optional[str] = None
    end: Optional[str] = None
    days: int = 0
    desc: str = ""


async def _get_admin_token() -> str:
    """用管理员账号换取禅道token"""
    settings = get_settings()
    zt = ZentaoClient()
    token = await zt.login(settings.zentao_admin_account, settings.zentao_admin_password)
    if not token:
        raise HTTPException(status_code=400, detail="禅道管理员登录失败")
    return token


@router.get("/products")
async def list_zentao_products(user: User = Depends(get_current_user)):
    """获取禅道产品列表"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    products = await zt.get_products()
    return [{"id": p.get("id"), "name": p.get("name"), "code": p.get("code", ""), "status": p.get("status")} for p in products]


@router.post("/products")
async def create_zentao_product(data: ProductCreateReq, user: User = Depends(get_current_user)):
    """在禅道创建产品（产品发布将在创建项目时自动生成）"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    try:
        result = await zt.create_product(name=data.name, code=data.code, product_type=data.product_type, desc=data.desc)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"禅道创建产品失败: {str(e)}")
    return {"id": result.get("id"), "name": result.get("name"), "code": result.get("code", "")}


@router.get("/projects")
async def list_zentao_projects(user: User = Depends(get_current_user)):
    """获取禅道项目列表（含关联产品）"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    projects = await zt.get_projects()
    return [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "status": p.get("status"),
            "begin": p.get("begin"),
            "end": p.get("end"),
        }
        for p in projects
    ]


@router.post("/projects")
async def create_zentao_project(
    data: ProjectCreateReq,
    user: User = Depends(get_current_user),
):
    """在禅道创建项目（需关联产品），并自动生成执行、构建和发布"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 创建项目
    try:
        result = await zt.create_project(
            name=data.name, products=data.products, code=data.code,
            model=data.model, begin=data.begin, end=data.end, days=data.days, desc=data.desc,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"禅道创建项目失败: {str(e)}")
    project_id = result.get("id")
    product_id = data.products[0] if data.products else 0

    # 2. 创建执行
    execution_id = None
    if project_id:
        try:
            exec_result = await zt.create_execution(
                project_id=project_id, name=f"{data.name}-执行", begin=data.begin or today,
                end=data.end or today, products=data.products
            )
            execution_id = exec_result.get("id")
            # 禅道可能返回空body，重新拉取获取ID
            if not execution_id:
                execs = await zt.get_executions(project_id)
                execution_id = execs[0].get("id") if execs else None
        except Exception:
            pass

    # 3. 创建构建
    build_id = None
    if project_id and execution_id and product_id:
        try:
            await zt.create_build(
                project_id=project_id, execution_id=execution_id, product_id=product_id,
                name=f"{data.name}-构建-v1.0", builder="admin",
                date_str=today, build_version="v1.0"
            )
            # 禅道创建构建返回空body，重新拉取获取ID
            builds = await zt.get_builds(project_id)
            build_id = builds[0].get("id") if builds else None
        except Exception:
            pass

    # 4. 创建项目发布
    release_ok = False
    if project_id and product_id:
        try:
            release_result = await zt.create_project_release(
                project_id=project_id, name=f"{data.name}-发布-v1.0",
                product_id=product_id, date_str=today,
                build_id=build_id or 0, desc="项目创建时自动生成"
            )
            release_ok = bool(release_result.get("id") or release_result)
        except Exception:
            pass

    return {"id": project_id, "name": result.get("name"), "status": result.get("status", "wait"),
            "execution_id": execution_id, "build_id": build_id, "release_created": release_ok}


# === 测试单 ===
class TesttaskCreateReq(BaseModel):
    project_id: int
    product_id: int
    name: str = Field(..., min_length=1, max_length=255)
    build_id: int = 0
    begin: str
    end: str
    owner: str = "admin"
    testtask_type: str = "system"


@router.get("/testtasks")
async def list_testtasks(
    product: int,
    user: User = Depends(get_current_user),
):
    """获取产品的测试单列表"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    tasks = await zt.get_testtasks(product)
    return [
        {"id": t.get("id"), "name": t.get("name"), "status": t.get("status"),
         "build": t.get("build"), "begin": t.get("begin"), "end": t.get("end")}
        for t in tasks
    ]


@router.post("/testtasks")
async def create_testtask(data: TesttaskCreateReq, user: User = Depends(get_current_user)):
    """在禅道创建测试单（自动查找或创建执行和构建，用户无感）"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 找关联了指定产品的执行（禅道要求测试单的product必须和execution一致）
    execution_id = await zt.get_execution_for_product(data.project_id, data.product_id)
    if not execution_id:
        # 没有匹配的执行，创建一个关联该产品的执行
        exec_begin = data.begin
        exec_end = data.end
        try:
            proj = await zt.get_project(data.project_id)
            if proj:
                exec_begin = max(data.begin, proj.get("begin") or data.begin)
                exec_end = min(data.end, proj.get("end") or data.end)
        except Exception:
            pass
        try:
            exec_result = await zt.create_execution(
                project_id=data.project_id,
                name=f"{data.name}-执行",
                begin=exec_begin, end=exec_end,
                products=[data.product_id],
            )
            execution_id = exec_result.get("id")
            if not execution_id:
                execs = await zt.get_executions(data.project_id)
                execution_id = execs[0].get("id") if execs else None
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无法自动创建执行: {str(e)}")

    # 2. 自动获取项目的构建，没有则自动创建（禅道创建测试单必须有构建ID）
    build_id = data.build_id
    if not build_id:
        builds = await zt.get_builds(data.project_id)
        build_id = builds[0].get("id") if builds else None
    if not build_id and execution_id:
        try:
            await zt.create_build(
                project_id=data.project_id, execution_id=execution_id,
                product_id=data.product_id, name=f"{data.name}-构建",
                builder=data.owner, date_str=today,
            )
            # 禅道创建构建返回空body无法直接拿ID，重新拉取列表获取
            builds = await zt.get_builds(data.project_id)
            build_id = builds[0].get("id") if builds else None
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无法自动创建构建: {str(e)}")
    if not build_id:
        raise HTTPException(status_code=400, detail="无法获取构建ID，请先在禅道为该项目创建构建")

    try:
        result = await zt.create_testtask(
            project_id=data.project_id, execution_id=execution_id,
            product_id=data.product_id, name=data.name, build_id=build_id,
            begin=data.begin, end=data.end, owner=data.owner,
            testtask_type=data.testtask_type,
        )
    except Exception:
        # 用户选的构建可能不属于该产品，自动创建一个匹配的构建重试
        try:
            await zt.create_build(
                project_id=data.project_id, execution_id=execution_id,
                product_id=data.product_id, name=f"{data.name}-构建",
                builder=data.owner, date_str=today,
            )
            builds = await zt.get_builds(data.project_id)
            build_id = builds[0].get("id") if builds else build_id
            result = await zt.create_testtask(
                project_id=data.project_id, execution_id=execution_id,
                product_id=data.product_id, name=data.name, build_id=build_id,
                begin=data.begin, end=data.end, owner=data.owner,
                testtask_type=data.testtask_type,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"禅道创建测试单失败: {str(e)}")
    return {"id": result.get("id"), "name": result.get("name"), "status": result.get("status", "wait"),
            "build_id": build_id, "execution_id": execution_id}


@router.get("/executions")
async def list_executions(
    project: int,
    user: User = Depends(get_current_user),
):
    """获取项目的执行列表"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    execs = await zt.get_executions(project)
    return [{"id": e.get("id"), "name": e.get("name"), "status": e.get("status")} for e in execs]


@router.get("/builds")
async def list_builds(
    project: int,
    user: User = Depends(get_current_user),
):
    """获取项目的构建列表"""
    token = await _get_admin_token()
    zt = ZentaoClient(token=token)
    builds = await zt.get_builds(project)
    return [{"id": b.get("id"), "name": b.get("name")} for b in builds]
