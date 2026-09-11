"""
API路由 - 认证与会话（本地密码验证，不依赖禅道）
"""
import hashlib
import hmac
import os
import base64
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from app.db.database import get_db
from app.core.security import (
    create_access_token,
    get_current_user,
    require_admin,
    _user_roles,
    TOKEN_COOKIE,
)
from app.services.user_service import UserService, PROJECT_ROLES
from app.models.models import User

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginRequest(BaseModel):
    account: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)


class ChangePasswordReq(BaseModel):
    old_password: str = Field(..., description="旧密码")
    new_password: str = Field(..., min_length=6, max_length=200, description="新密码")


class AdminResetPasswordReq(BaseModel):
    user_id: int = Field(..., description="目标用户ID")
    new_password: str = Field(..., min_length=6, max_length=200, description="新密码")


def _hash_password(password: str) -> str:
    """PBKDF2 哈希密码（不依赖外部库）"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return f"pbkdf2:sha256${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _verify_password(password: str, stored: str) -> bool:
    """验证密码"""
    if not stored:
        return False
    try:
        if stored.startswith("pbkdf2:sha256$"):
            parts = stored.split("$")
            salt = base64.b64decode(parts[1])
            expected = base64.b64decode(parts[2])
            dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
            return hmac.compare_digest(dk, expected)
    except Exception:
        return False
    return False


def _gen_initial_password(account: str) -> str:
    """根据用户名生成初始密码: 首字母大写 + @123"""
    if not account:
        return "Init@123"
    return account[0].upper() + account[1:] + "@123"


def _user_payload(user: User, roles: list) -> dict:
    user_roles = _user_roles(user)
    return {
        "id": user.id,
        "account": user.account,
        "realname": user.realname,
        "role": user.role,
        "roles": user_roles,
        "is_admin": "admin" in user_roles,
    }


@router.post("/login")
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    """登录（本地密码验证）"""
    stmt = select(User).where(User.account == data.account)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    if not _verify_password(data.password, user.password_hash or ""):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    # 更新最后登录时间
    user.last_login_at = datetime.utcnow()
    await db.commit()

    service = UserService(db)
    roles = await service.get_user_roles(user.id)
    jwt_token = create_access_token(user.id, user.account)

    response = JSONResponse(content={"user": _user_payload(user, roles)})
    response.set_cookie(
        key=TOKEN_COOKIE,
        value=jwt_token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return response


@router.post("/logout")
async def logout():
    """退出登录"""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=TOKEN_COOKIE)
    return response


@router.get("/me")
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户信息与项目角色"""
    service = UserService(db)
    roles = await service.get_user_roles(user.id)
    return _user_payload(user, roles)


@router.put("/change-password")
async def change_password(
    data: ChangePasswordReq,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户修改自己的密码"""
    if not _verify_password(data.old_password, user.password_hash or ""):
        raise HTTPException(status_code=400, detail="旧密码错误")
    user.password_hash = _hash_password(data.new_password)
    await db.commit()
    return {"ok": True}


@router.put("/admin-reset-password")
async def admin_reset_password(
    data: AdminResetPasswordReq,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员重置任意用户密码"""
    stmt = select(User).where(User.id == data.user_id)
    result = await db.execute(stmt)
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    target.password_hash = _hash_password(data.new_password)
    await db.commit()
    return {"ok": True, "account": target.account}


@router.get("/project-roles")
async def project_roles():
    """项目角色字典"""
    return {"roles": [{"key": k, "name": v} for k, v in PROJECT_ROLES.items()]}
