"""
API路由 - 版本配套表

设计要点：
- 版本配套表是可复用基线，任务进入第三阶段时复制为独立快照；
- 配套表/条目可维护、可删除、可导出；
- 第三阶段完成前由后端硬校验：所有适用条目必须已勾选。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.export import build_xlsx_single
from app.core.security import get_current_user, require_version_config_manager
from app.db.database import get_db
from app.models.models import (
    Product, Project, TaskStep, TestTask, User,
    VersionConfig, VersionConfigItem, TaskVersionConfig, TaskVersionItem,
)

router = APIRouter(prefix="/api/version-configs", tags=["版本配套表"])
task_router = APIRouter(prefix="/api/tasks", tags=["版本配套表"])


class ConfigItemReq(BaseModel):
    node_type: str = "计算节点"
    group_type: str = "未分组"
    component_model: Optional[str] = None
    item_name: str = Field(..., min_length=1, max_length=200)
    required_value: Optional[str] = ""
    remark: Optional[str] = None
    sort_order: Optional[int] = None


class ConfigCreateReq(BaseModel):
    # model_key 是业务字段，显式关闭 pydantic 的 model_ 保护前缀告警
    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(..., min_length=1, max_length=200)
    product_id: Optional[int] = None
    model_key: Optional[str] = None
    description: Optional[str] = None
    copy_from_id: Optional[int] = None
    items: List[ConfigItemReq] = []


class ConfigUpdateReq(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    product_id: Optional[int] = None
    model_key: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class TaskItemPatchReq(BaseModel):
    checked: bool
    remark: Optional[str] = None


class TaskConfigInitReq(BaseModel):
    config_id: int


class TaskBulkCheckReq(BaseModel):
    checked: bool
    node_type: Optional[str] = None   # 传配置大类=只处理该组；不传=全部


def _s(v):
    return (v or "").strip()


def _is_applicable(value: Optional[str]) -> bool:
    return _s(value) not in ("", "/", "N/A", "n/a", "-")


def _item_dict(it: VersionConfigItem):
    return {
        "id": it.id,
        "node_type": it.node_type or "计算节点",
        "group_type": it.group_type or "未分组",
        "component_model": it.component_model or "",
        "item_name": it.item_name,
        "required_value": it.required_value or "",
        "is_applicable": bool(it.is_applicable),
        "remark": it.remark or "",
        "sort_order": it.sort_order or 0,
    }


def _task_item_dict(it: TaskVersionItem):
    return {
        "id": it.id,
        "node_type": it.node_type or "计算节点",
        "group_type": it.group_type or "未分组",
        "component_model": it.component_model or "",
        "item_name": it.item_name,
        "required_value": it.required_value or "",
        "is_applicable": bool(it.is_applicable),
        "is_checked": bool(it.is_checked),
        "remark": it.remark or "",
        "checked_by": it.checked_by,
        "checked_at": it.checked_at.isoformat() if it.checked_at else None,
        "sort_order": it.sort_order or 0,
    }


async def _product_map(db: AsyncSession, ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await db.execute(select(Product.id, Product.name).where(Product.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}


async def _get_config(db: AsyncSession, config_id: int, with_items: bool = False):
    stmt = select(VersionConfig).where(VersionConfig.id == config_id)
    if with_items:
        stmt = stmt.options(selectinload(VersionConfig.items))
    return (await db.execute(stmt)).scalar_one_or_none()


def _config_dict(cfg: VersionConfig, product_name: str = "", item_count: int = 0):
    return {
        "id": cfg.id,
        "name": cfg.name,
        "product_id": cfg.product_id,
        "product_name": product_name or "未指定产品",
        "model_key": cfg.model_key or "",
        "description": cfg.description or "",
        "is_active": bool(cfg.is_active),
        "item_count": item_count,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


# ---------------- 版本配套表管理 ----------------
@router.get("")
async def list_configs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    configs = (await db.execute(
        select(VersionConfig).order_by(VersionConfig.id.desc())
    )).scalars().all()
    counts = dict((await db.execute(
        select(VersionConfigItem.config_id, func.count(VersionConfigItem.id))
        .group_by(VersionConfigItem.config_id)
    )).all())
    pmap = await _product_map(db, [c.product_id for c in configs])
    return [_config_dict(c, pmap.get(c.product_id, ""), counts.get(c.id, 0)) for c in configs]


@router.get("/{config_id}")
async def get_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_config(db, config_id, with_items=True)
    if not cfg:
        raise HTTPException(status_code=404, detail="版本配套表不存在")
    pmap = await _product_map(db, [cfg.product_id])
    data = _config_dict(cfg, pmap.get(cfg.product_id, ""), len(cfg.items))
    data["items"] = [_item_dict(i) for i in cfg.items]
    return data


@router.post("")
async def create_config(
    data: ConfigCreateReq,
    current_user: User = Depends(require_version_config_manager),
    db: AsyncSession = Depends(get_db),
):
    # 支持「复制已有配套表」：新产品/新机型从最接近的一份改起，避免从零敲
    if not data.product_id:
        raise HTTPException(status_code=400, detail="请选择适用产品：版本配套表必须归属到具体产品")
    source_items = []
    if data.copy_from_id:
        src = await _get_config(db, data.copy_from_id, with_items=True)
        if not src:
            raise HTTPException(status_code=400, detail="要复制的版本配套表不存在")
        source_items = list(src.items)

    cfg = VersionConfig(
        name=data.name.strip(),
        product_id=data.product_id,
        model_key=_s(data.model_key) or None,
        description=_s(data.description) or None,
        is_active=True,
        created_by=current_user.id,
    )
    db.add(cfg)
    await db.flush()
    if data.items:
        for idx, item in enumerate(data.items):
            db.add(VersionConfigItem(
                config_id=cfg.id,
                node_type=_s(item.node_type) or "计算节点",
                group_type=_s(item.group_type) or "未分组",
                component_model=_s(item.component_model) or None,
                item_name=item.item_name.strip(),
                required_value=_s(item.required_value),
                is_applicable=_is_applicable(item.required_value),
                remark=_s(item.remark) or None,
                sort_order=item.sort_order if item.sort_order is not None else (idx + 1) * 10,
            ))
    else:
        for idx, it in enumerate(source_items):
            db.add(VersionConfigItem(
                config_id=cfg.id,
                node_type=it.node_type or "计算节点",
                group_type=it.group_type or "未分组",
                component_model=it.component_model,
                item_name=it.item_name,
                required_value=it.required_value or "",
                is_applicable=bool(it.is_applicable),
                remark=it.remark,
                sort_order=it.sort_order if it.sort_order is not None else (idx + 1) * 10,
            ))
    await db.commit()
    await db.refresh(cfg)
    return {"id": cfg.id, "name": cfg.name, "copied_from": data.copy_from_id or None,
            "item_count": len(data.items) if data.items else len(source_items)}


@router.put("/{config_id}")
async def update_config(
    config_id: int,
    data: ConfigUpdateReq,
    current_user: User = Depends(require_version_config_manager),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_config(db, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="版本配套表不存在")
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="版本配套表名称不能为空")
        cfg.name = name
    if "product_id" in data.model_fields_set:
        if not data.product_id:
            raise HTTPException(status_code=400, detail="版本配套表必须归属到具体产品")
        cfg.product_id = data.product_id
    # model_key / description 用「是否显式传入」判断，否则传 null 会被当成不修改，前端清空说明保存不掉
    if "model_key" in data.model_fields_set:
        cfg.model_key = _s(data.model_key) or None
    if "description" in data.model_fields_set:
        cfg.description = _s(data.description) or None
    if data.is_active is not None:
        cfg.is_active = data.is_active
    await db.commit()
    return {"ok": True, "id": cfg.id}


@router.delete("/{config_id}")
async def delete_config(
    config_id: int,
    current_user: User = Depends(require_version_config_manager),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_config(db, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="版本配套表不存在")
    await db.execute(sa_delete(VersionConfigItem).where(VersionConfigItem.config_id == config_id))
    await db.execute(sa_delete(VersionConfig).where(VersionConfig.id == config_id))
    await db.commit()
    return {"ok": True, "id": config_id}


@router.post("/{config_id}/items")
async def add_item(
    config_id: int,
    data: ConfigItemReq,
    current_user: User = Depends(require_version_config_manager),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_config(db, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="版本配套表不存在")
    max_sort = (await db.execute(
        select(func.max(VersionConfigItem.sort_order)).where(VersionConfigItem.config_id == config_id)
    )).scalar() or 0
    item = VersionConfigItem(
        config_id=config_id,
        node_type=_s(data.node_type) or "计算节点",
        group_type=_s(data.group_type) or "未分组",
        component_model=_s(data.component_model) or None,
        item_name=data.item_name.strip(),
        required_value=_s(data.required_value),
        is_applicable=_is_applicable(data.required_value),
        remark=_s(data.remark) or None,
        sort_order=data.sort_order if data.sort_order is not None else max_sort + 10,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.put("/{config_id}/items/{item_id}")
async def update_item(
    config_id: int,
    item_id: int,
    data: ConfigItemReq,
    current_user: User = Depends(require_version_config_manager),
    db: AsyncSession = Depends(get_db),
):
    item = (await db.execute(
        select(VersionConfigItem).where(
            VersionConfigItem.id == item_id, VersionConfigItem.config_id == config_id
        )
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="配套项不存在")
    item.node_type = _s(data.node_type) or "计算节点"
    item.group_type = _s(data.group_type) or "未分组"
    item.component_model = _s(data.component_model) or None
    item.item_name = data.item_name.strip()
    item.required_value = _s(data.required_value)
    item.is_applicable = _is_applicable(data.required_value)
    item.remark = _s(data.remark) or None
    if data.sort_order is not None:
        item.sort_order = data.sort_order
    await db.commit()
    return _item_dict(item)


@router.delete("/{config_id}/items/{item_id}")
async def delete_item(
    config_id: int,
    item_id: int,
    current_user: User = Depends(require_version_config_manager),
    db: AsyncSession = Depends(get_db),
):
    item = (await db.execute(
        select(VersionConfigItem).where(
            VersionConfigItem.id == item_id, VersionConfigItem.config_id == config_id
        )
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="配套项不存在")
    await db.execute(sa_delete(VersionConfigItem).where(VersionConfigItem.id == item_id))
    await db.commit()
    return {"ok": True, "id": item_id}


@router.get("/{config_id}/export")
async def export_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _get_config(db, config_id, with_items=True)
    if not cfg:
        raise HTTPException(status_code=404, detail="版本配套表不存在")
    rows = [[("s", "配置大类"), ("s", "一级分类"), ("s", "要求版本")]]
    for it in cfg.items:
        rows.append([
            ("s", it.node_type or ""),
            ("s", it.item_name or ""),
            ("s", it.required_value or ""),
        ])
    if not cfg.items:
        rows.append([("s", "（暂无配套项）"), ("s", ""), ("s", "")])
    xlsx = build_xlsx_single("版本配套表", rows, [18, 34, 46])
    filename = f"version_config_{cfg.id}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- 任务第三阶段 ----------------
async def _task_step3(db: AsyncSession, task_id: int):
    task = (await db.execute(select(TestTask).where(TestTask.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    step = (await db.execute(
        select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step == 3)
    )).scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="第三阶段不存在")
    return task, step


async def _task_product_id(db: AsyncSession, task: TestTask):
    if not task.project_id:
        return None
    project = (await db.execute(select(Project).where(Project.id == task.project_id))).scalar_one_or_none()
    return project.product_id if project else None


async def _candidate_configs(db: AsyncSession, product_id: Optional[int]):
    """第三阶段可选的配套表：严格按产品归属，不跨产品、不再有「通用」表。

    同一产品可以有多份配套表（不同机型/不同过点版本），由第三阶段负责人自行选择。
    """
    if not product_id:
        return []
    return (await db.execute(
        select(VersionConfig)
        .where(VersionConfig.is_active.is_(True), VersionConfig.product_id == product_id)
        .order_by(VersionConfig.id)
    )).scalars().all()


async def _get_task_config(db: AsyncSession, task_id: int):
    return (await db.execute(
        select(TaskVersionConfig).where(TaskVersionConfig.task_id == task_id)
    )).scalar_one_or_none()


async def _create_task_snapshot(db: AsyncSession, task: TestTask, step: TaskStep, cfg: VersionConfig):
    header = TaskVersionConfig(
        task_id=task.id,
        step_id=step.id,
        config_id=cfg.id,
        config_name=cfg.name,
    )
    db.add(header)
    await db.flush()
    for it in cfg.items:
        db.add(TaskVersionItem(
            task_config_id=header.id,
            source_item_id=it.id,
            node_type=it.node_type or "计算节点",
            group_type=it.group_type or "未分组",
            component_model=it.component_model,
            item_name=it.item_name,
            required_value=it.required_value or "",
            is_applicable=bool(it.is_applicable),
            is_checked=False,
            check_result="na" if not it.is_applicable else "pending",
            remark=it.remark,
            sort_order=it.sort_order or 0,
        ))
    await db.commit()
    await db.refresh(header)
    return header


def _task_config_payload(header: TaskVersionConfig, items, product_name: str = ""):
    applicable = [
        i for i in items
        if i.is_applicable and _s(i.required_value) not in ("", "/", "N/A", "n/a", "-")
    ]
    checked = [i for i in applicable if i.is_checked]
    na_count = len([i for i in items if i not in applicable])
    return {
        "initialized": True,
        "config": {
            "id": header.config_id,
            "name": header.config_name,
            "product_name": product_name or "未指定产品",
        },
        "summary": {
            "total": len(items),
            "applicable": len(applicable),
            "checked": len(checked),
            "pending": len(applicable) - len(checked),
            "na": na_count,
        },
        "items": [_task_item_dict(i) for i in items],
    }


@task_router.get("/{task_id}/version-config")
async def get_task_version_config(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, step = await _task_step3(db, task_id)
    header = await _get_task_config(db, task_id)
    product_id = await _task_product_id(db, task)
    candidates = await _candidate_configs(db, product_id)

    if not header:
        # 唯一候选时自动生成快照；多候选交给前端选择，避免猜错
        if len(candidates) == 1:
            cfg = await _get_config(db, candidates[0].id, with_items=True)
            header = await _create_task_snapshot(db, task, step, cfg)
        else:
            pmap = await _product_map(db, [c.product_id for c in candidates])
            return {
                "initialized": False,
                "needs_selection": True,
                "config": None,
                "summary": {"total": 0, "applicable": 0, "checked": 0, "pending": 0, "na": 0},
                "items": [],
                "available_configs": [
                    _config_dict(c, pmap.get(c.product_id, ""), 0) for c in candidates
                ],
                "can_edit": bool(
                    step.assigned_to == current_user.id
                    and task.current_step == 3
                    and step.status != "completed"
                ),
            }

    items = (await db.execute(
        select(TaskVersionItem)
        .where(TaskVersionItem.task_config_id == header.id)
        .order_by(TaskVersionItem.sort_order, TaskVersionItem.id)
    )).scalars().all()
    # 快照里只存了 config_name，产品名要回到基线表取（配套表严格按产品归属）
    product_name = ""
    if header.config_id:
        cfg_row = (await db.execute(
            select(VersionConfig).where(VersionConfig.id == header.config_id)
        )).scalar_one_or_none()
        if cfg_row and cfg_row.product_id:
            pmap0 = await _product_map(db, [cfg_row.product_id])
            product_name = pmap0.get(cfg_row.product_id, "")
    payload = _task_config_payload(header, items, product_name)
    payload["needs_selection"] = False
    pmap = await _product_map(db, [c.product_id for c in candidates])
    cand_counts = {}
    if candidates:
        cand_counts = dict((await db.execute(
            select(VersionConfigItem.config_id, func.count(VersionConfigItem.id))
            .where(VersionConfigItem.config_id.in_([c.id for c in candidates]))
            .group_by(VersionConfigItem.config_id)
        )).all())
    payload["available_configs"] = [
        _config_dict(c, pmap.get(c.product_id, ""), cand_counts.get(c.id, 0)) for c in candidates
    ]
    payload["can_edit"] = bool(
        step.assigned_to == current_user.id
        and task.current_step == 3
        and step.status != "completed"
    )
    return payload


@task_router.post("/{task_id}/version-config/init")
async def init_task_version_config(
    task_id: int,
    data: TaskConfigInitReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, step = await _task_step3(db, task_id)
    if not (step.assigned_to == current_user.id and task.current_step == 3 and step.status != "completed"):
        raise HTTPException(status_code=403, detail="仅当前第三阶段负责人可选择版本配套表")
    header = await _get_task_config(db, task_id)
    if header:
        return {"ok": True, "already_initialized": True}
    cfg = await _get_config(db, data.config_id, with_items=True)
    if not cfg or not cfg.is_active:
        raise HTTPException(status_code=400, detail="版本配套表不存在或已停用")
    await _create_task_snapshot(db, task, step, cfg)
    return {"ok": True, "config_id": cfg.id, "config_name": cfg.name}


@task_router.post("/{task_id}/version-config/switch")
async def switch_task_version_config(
    task_id: int,
    data: TaskConfigInitReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """第三阶段负责人换用另一份配套表（同一产品可有多份）。

    换表会丢弃已核对进度，前端需二次确认后再调用。
    """
    task, step = await _task_step3(db, task_id)
    if not (step.assigned_to == current_user.id and task.current_step == 3 and step.status != "completed"):
        raise HTTPException(status_code=403, detail="仅当前第三阶段负责人可切换版本配套表")
    cfg = await _get_config(db, data.config_id, with_items=True)
    if not cfg or not cfg.is_active:
        raise HTTPException(status_code=400, detail="版本配套表不存在或已停用")
    header = await _get_task_config(db, task_id)
    if header and header.config_id == cfg.id:
        return {"ok": True, "unchanged": True, "config_id": cfg.id, "config_name": cfg.name}
    if header:
        await db.execute(sa_delete(TaskVersionItem).where(TaskVersionItem.task_config_id == header.id))
        await db.execute(sa_delete(TaskVersionConfig).where(TaskVersionConfig.id == header.id))
        await db.commit()
    await _create_task_snapshot(db, task, step, cfg)
    return {"ok": True, "config_id": cfg.id, "config_name": cfg.name, "item_count": len(cfg.items)}


@task_router.post("/{task_id}/version-config/bulk-check")
async def bulk_check_task_items(
    task_id: int,
    data: TaskBulkCheckReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量核对：整组（按配置大类）或全部一次性打勾/取消，替代逐条点 33 次。"""
    task, step = await _task_step3(db, task_id)
    if not (step.assigned_to == current_user.id and task.current_step == 3 and step.status != "completed"):
        raise HTTPException(status_code=403, detail="仅当前第三阶段负责人可核对版本配套表")
    header = await _get_task_config(db, task_id)
    if not header:
        raise HTTPException(status_code=400, detail="任务尚未生成版本配套表")
    stmt = select(TaskVersionItem).where(TaskVersionItem.task_config_id == header.id)
    if data.node_type:
        stmt = stmt.where(TaskVersionItem.node_type == data.node_type)
    items = (await db.execute(stmt)).scalars().all()
    now = datetime.now()
    affected = 0
    for it in items:
        if not it.is_applicable or _s(it.required_value) in ("", "/", "N/A", "n/a", "-"):
            continue
        it.is_checked = data.checked
        it.check_result = "ok" if data.checked else "pending"
        it.checked_by = current_user.id if data.checked else None
        it.checked_at = now if data.checked else None
        affected += 1
    await db.commit()
    return {"ok": True, "affected": affected, "checked": data.checked}


@task_router.patch("/{task_id}/version-config/items/{item_id}")
async def patch_task_version_item(
    task_id: int,
    item_id: int,
    data: TaskItemPatchReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, step = await _task_step3(db, task_id)
    if not (step.assigned_to == current_user.id and task.current_step == 3 and step.status != "completed"):
        raise HTTPException(status_code=403, detail="仅当前第三阶段负责人可核对版本配套表")
    header = await _get_task_config(db, task_id)
    if not header:
        raise HTTPException(status_code=400, detail="任务尚未生成版本配套表")
    item = (await db.execute(
        select(TaskVersionItem).where(
            TaskVersionItem.id == item_id, TaskVersionItem.task_config_id == header.id
        )
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="配套项不存在")
    if not item.is_applicable:
        raise HTTPException(status_code=400, detail="该项标记为不适用，无需核对")
    item.is_checked = data.checked
    item.check_result = "ok" if data.checked else "pending"
    item.checked_by = current_user.id if data.checked else None
    item.checked_at = datetime.now() if data.checked else None
    if data.remark is not None:
        item.remark = data.remark.strip() or None
    await db.commit()
    return {"ok": True, "id": item.id, "checked": item.is_checked}


@task_router.get("/{task_id}/version-config/export")
async def export_task_version_config(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, step = await _task_step3(db, task_id)
    header = await _get_task_config(db, task_id)
    if not header:
        raise HTTPException(status_code=404, detail="任务尚未生成版本配套表")
    items = (await db.execute(
        select(TaskVersionItem)
        .where(TaskVersionItem.task_config_id == header.id)
        .order_by(TaskVersionItem.sort_order, TaskVersionItem.id)
    )).scalars().all()
    rows = [[("s", "配置大类"), ("s", "一级分类"), ("s", "要求版本"),
             ("s", "核对状态"), ("s", "备注")]]
    for it in items:
        if not it.is_applicable:
            status = "不适用"
        elif it.is_checked:
            status = "已核对"
        else:
            status = "待核对"
        rows.append([
            ("s", it.node_type or ""),
            ("s", it.item_name or ""),
            ("s", it.required_value or ""),
            ("s", status),
            ("s", it.remark or ""),
        ])
    xlsx = build_xlsx_single("版本配套表", rows, [18, 34, 46, 12, 36])
    filename = f"version_config_{task.code}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
