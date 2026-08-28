"""
认证与安全 - JWT会话管理
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_db
from app.models.models import User

settings = get_settings()

TOKEN_COOKIE = "testtask_token"
TOKEN_EXPIRE_HOURS = 24 * 7  # 7天


def create_access_token(user_id: int, account: str) -> str:
    """签发JWT"""
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "account": account,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def verify_token(token: str) -> Optional[dict]:
    """校验JWT，返回payload"""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload
    except JWTError:
        return None


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """从Cookie或Authorization头获取当前登录用户"""
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="会话已过期，请重新登录")

    user_id = int(payload.get("sub", 0))
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return user


def _user_roles(user: User) -> list:
    """获取用户角色列表（roles字段优先，兼容旧role字段）"""
    if user.roles:
        return user.roles
    return [user.role] if user.role else []


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求管理员权限"""
    if "admin" not in _user_roles(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
