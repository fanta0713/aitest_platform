"""
API路由 - 用户与角色管理
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.core.security import get_current_user, require_admin
from app.services.user_service import UserService, PROJECT_ROLES
from app.models.models import User, ProjectMember, TestTask
from sqlalchemy import func

router = APIRouter(prefix="/api", tags=["用户与项目"])

# 全局角色
GLOBAL_ROLES = {"admin", "pl", "tse", "version_owner", "executor"}
ROLE_LABELS = {"admin": "管理员", "pl": "PL", "tse": "TSE", "version_owner": "版本负责人", "executor": "测试执行"}


class MemberRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    project_role: str = Field(...)


class RoleUpdateReq(BaseModel):
    roles: list = Field(..., description="角色列表: [admin/pl/tse/version_owner/executor]")


class UserCreateReq(BaseModel):
    account: str = Field(..., min_length=1, max_length=100, description="登录账号")
    realname: str = Field("", description="真实姓名")
    roles: list = Field(default=["member"], description="角色列表")


def _ensure_role(project_role: str):
    if project_role not in PROJECT_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色: {project_role}")


@router.get("/users")
async def list_users(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户列表（含角色列表）"""
    from app.core.security import _user_roles
    service = UserService(db)
    users = await service.list_users()
    return [
        {"id": u.id, "account": u.account, "realname": u.realname,
         "role": u.role, "roles": _user_roles(u)}
        for u in users
    ]


@router.put("/users/{user_id}/roles")
async def set_user_roles(
    user_id: int,
    data: RoleUpdateReq,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """设置用户角色列表（仅管理员，支持多角色）"""
    invalid = set(data.roles) - GLOBAL_ROLES
    if invalid:
        raise HTTPException(status_code=400, detail=f"无效角色: {','.join(invalid)}，可选: {', '.join(GLOBAL_ROLES)}")
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.roles = data.roles
    user.role = "admin" if "admin" in data.roles else (data.roles[0] if data.roles else "member")
    await db.commit()
    return {"ok": True, "id": user.id, "account": user.account, "roles": data.roles}


@router.post("/users")
async def create_user(
    data: UserCreateReq,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """创建用户（仅管理员）"""
    import hashlib, os, base64
    # 检查账号是否已存在
    existing = await db.execute(select(User).where(User.account == data.account))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"账号 {data.account} 已存在")
    # 生成初始密码: 首字母大写 + @123
    init_pwd = data.account[0].upper() + data.account[1:] + "@123" if data.account else "Init@123"
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', init_pwd.encode(), salt, 100000)
    password_hash = f"pbkdf2:sha256${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    # 确定 role
    roles = data.roles or ["member"]
    role = "admin" if "admin" in roles else roles[0]
    user = User(
        account=data.account,
        realname=data.realname or data.account,
        password_hash=password_hash,
        role=role,
        roles=roles,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"id": user.id, "account": user.account, "realname": user.realname, "initial_password": init_pwd}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除用户（仅管理员）"""
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.account == "admin":
        raise HTTPException(status_code=400, detail="管理员账号不可删除")
    # 检查是否有关联测试任务（只统计未删除的任务，软删除不算）
    task_cnt = (await db.execute(
        select(func.count(TestTask.id)).where(
            ((TestTask.owner == user_id) | (TestTask.tse_id == user_id) |
            (TestTask.version_owner == user_id) | (TestTask.executor_id == user_id))
            & (TestTask.deleted == False)
        )
    )).scalar()
    if task_cnt > 0:
        raise HTTPException(status_code=400, detail=f"该用户关联了 {task_cnt} 个测试任务，无法删除。请先迁移任务。")
    # 检查是否有项目成员记录
    member_cnt = (await db.execute(
        select(func.count(ProjectMember.id)).where(ProjectMember.user_id == user_id)
    )).scalar()
    if member_cnt > 0:
        raise HTTPException(status_code=400, detail=f"该用户在 {member_cnt} 个项目中担任角色，无法删除。请先移除项目角色。")
    await db.delete(user)
    await db.commit()
    return {"ok": True}


@router.get("/projects")
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """项目列表（含成员角色）"""
    service = UserService(db)
    return await service.list_projects()


@router.post("/projects/{project_id}/members")
async def add_member(
    project_id: int,
    data: MemberRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """为项目添加成员角色（仅管理员）"""
    _ensure_role(data.project_role)
    service = UserService(db)
    try:
        await service.add_member(project_id, data.user_id, data.project_role)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    members = await service.get_project_members(project_id)
    return {"ok": True, "members": members}


@router.delete("/projects/{project_id}/members/{user_id}/{project_role}")
async def remove_member(
    project_id: int,
    user_id: int,
    project_role: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """移除项目成员角色（仅管理员）"""
    _ensure_role(project_role)
    service = UserService(db)
    await service.remove_member(project_id, user_id, project_role)
    members = await service.get_project_members(project_id)
    return {"ok": True, "members": members}
