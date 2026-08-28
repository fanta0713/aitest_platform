"""
服务层 - 用户与项目角色
"""
import json
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from app.models.models import User, Project, ProjectMember

# 项目角色定义
PROJECT_ROLES = {
    "pl": "PL（项目负责人）",
    "tse": "TSE（测试设计）",
    "version_owner": "版本负责人",
    "executor": "测试执行",
}


class UserService:
    """用户服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_account(self, account: str) -> Optional[User]:
        stmt = select(User).where(User.account == account)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get(self, user_id: int) -> Optional[User]:
        stmt = select(User).where(User.id == user_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def login_user(self, account: str, realname: Optional[str] = None) -> User:
        """登录成功后更新/创建本地用户"""
        user = await self.get_by_account(account)
        now = datetime.now(timezone.utc)
        if not user:
            # 首次登录自动建户；禅道admin映射为系统管理员
            user = User(
                account=account,
                realname=realname or account,
                role="admin" if account == "admin" else "member",
                last_login_at=now,
            )
            self.db.add(user)
        else:
            user.last_login_at = now
            if realname and user.realname != realname:
                user.realname = realname
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def list_users(self) -> List[User]:
        stmt = select(User).where(User.is_active == True).order_by(User.id)  # noqa: E712
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_user_roles(self, user_id: int) -> List[dict]:
        """获取用户的所有项目角色"""
        stmt = (
            select(ProjectMember, Project)
            .join(Project, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user_id)
        )
        result = await self.db.execute(stmt)
        rows = result.all()
        return [
            {
                "project_id": pm.project_id,
                "project_name": proj.name,
                "project_role": pm.project_role,
                "project_role_name": PROJECT_ROLES.get(pm.project_role, pm.project_role),
            }
            for pm, proj in rows
        ]

    # === 项目成员管理 ===
    async def list_projects(self) -> List[dict]:
        stmt = select(Project).order_by(Project.id)
        result = await self.db.execute(stmt)
        projects = list(result.scalars().all())
        # 批量查产品名
        prod_ids = {p.product_id for p in projects if p.product_id}
        prod_map = {}
        if prod_ids:
            from app.models.models import Product
            prod_result = await self.db.execute(select(Product).where(Product.id.in_(prod_ids)))
            for prod in prod_result.scalars().all():
                prod_map[prod.id] = prod.name
        out = []
        for p in projects:
            members = await self.get_project_members(p.id)
            out.append(
                {
                    "id": p.id,
                    "zentao_id": p.zentao_id,
                    "product_id": p.product_id,
                    "product_name": prod_map.get(p.product_id, "") if p.product_id else "",
                    "name": p.name,
                    "status": p.status,
                    "begin_date": str(p.begin_date) if p.begin_date else None,
                    "end_date": str(p.end_date) if p.end_date else None,
                    "pm_account": p.pm_account,
                    "members": members,
                }
            )
        return out

    async def get_project_members(self, project_id: int) -> List[dict]:
        stmt = (
            select(ProjectMember, User)
            .join(User, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project_id)
        )
        result = await self.db.execute(stmt)
        return [
            {
                "id": pm.id,
                "user_id": u.id,
                "account": u.account,
                "realname": u.realname,
                "project_role": pm.project_role,
                "project_role_name": PROJECT_ROLES.get(pm.project_role, pm.project_role),
            }
            for pm, u in result.all()
        ]

    async def add_member(self, project_id: int, user_id: int, project_role: str) -> dict:
        if project_role not in PROJECT_ROLES:
            raise ValueError(f"无效角色: {project_role}")
        # 同一用户同一项目同一角色幂等
        stmt = select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            ProjectMember.project_role == project_role,
        )
        result = await self.db.execute(stmt)
        existing = result.scalar_one_or_none()
        if not existing:
            self.db.add(
                ProjectMember(
                    project_id=project_id, user_id=user_id, project_role=project_role
                )
            )
            await self.db.commit()
        return {"ok": True}

    async def remove_member(self, project_id: int, user_id: int, project_role: str) -> dict:
        stmt = delete(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            ProjectMember.project_role == project_role,
        )
        await self.db.execute(stmt)
        await self.db.commit()
        return {"ok": True}


def _parse_date(val):
    if not val:
        return None
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
