"""
API路由 - 步骤附件（环境自检报告等）
"""
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from typing import Optional

from app.db.database import get_db
from app.core.security import get_current_user, _user_roles
from app.core.step_perms import can_operate_step, deny_detail
from app.models.models import User, StepFile, TaskStep, TestTask
from app.services.task_service import TaskService

router = APIRouter(prefix="/api/tasks", tags=["步骤附件"])

# 上传根目录
UPLOAD_DIR = "/app/uploads"
# 允许的文件扩展名（白名单）
ALLOWED_EXT = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv", "md", "json", "xml", "log",
    "png", "jpg", "jpeg", "gif", "bmp", "webp",
    "zip", "tar", "gz", "7z", "rar",
}
# 单文件最大 50MB
MAX_FILE_SIZE = 50 * 1024 * 1024


def _ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _check_step_permission(step: TaskStep, user: User, task: TestTask = None) -> None:
    """上传/删除附件的权限，统一走 step_perms 口径。

    历史坑：这里原来自己写了一份 `is_all_hands = step.assigned_to is None`，
    环节9（assigned_to 恒为 NULL 的双人共审环节）因此变成"谁都能传附件"。
    现在与 update_step 共用 can_operate_step。
    """
    if not can_operate_step(step, task, user.id, _user_roles(user)):
        raise HTTPException(status_code=403, detail=deny_detail(step))


@router.get("/{task_id}/steps/{step_id}/files")
async def list_step_files(
    task_id: int,
    step_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出某步骤的附件"""
    stmt = (
        select(StepFile)
        .where(StepFile.step_id == step_id, StepFile.task_id == task_id)
        .order_by(StepFile.uploaded_at.desc())
    )
    result = await db.execute(stmt)
    files = result.scalars().all()
    return [
        {
            "id": f.id,
            "step_id": f.step_id,
            "task_id": f.task_id,
            "original_name": f.original_name,
            "file_size": f.file_size,
            "mime_type": f.mime_type,
            "category": f.category,
            "uploaded_by": f.uploaded_by,
            "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None,
        }
        for f in files
    ]


@router.post("/{task_id}/steps/{step_id}/files")
async def upload_step_file(
    task_id: int,
    step_id: int,
    file: UploadFile = File(...),
    category: Optional[str] = Form("env_check"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传附件到某步骤"""
    # 校验步骤存在 + 属于该任务
    step_stmt = select(TaskStep).where(TaskStep.id == step_id, TaskStep.task_id == task_id)
    step = (await db.execute(step_stmt)).scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="步骤不存在或不属于该任务")
    # 查任务（用于判断 step 7 是否允许 task.executors 上传）
    task_stmt = select(TestTask).where(TestTask.id == task_id)
    task = (await db.execute(task_stmt)).scalar_one_or_none()
    _check_step_permission(step, current_user, task)

    # 文件名 + 扩展名校验
    original_name = file.filename or "untitled"
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 .{ext}，允许: {', '.join(sorted(ALLOWED_EXT))}",
        )

    # 读取内容 + 大小校验
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大（{len(content)} 字节），单文件上限 50MB",
        )

    # 落盘
    _ensure_upload_dir()
    stored_name = f"{uuid.uuid4().hex}_{step_id}_{ext}"
    file_path = os.path.join(UPLOAD_DIR, stored_name)
    with open(file_path, "wb") as f:
        f.write(content)

    # 入库
    rec = StepFile(
        step_id=step_id,
        task_id=task_id,
        original_name=original_name,
        stored_name=stored_name,
        file_size=len(content),
        mime_type=file.content_type,
        category=category or "env_check",
        uploaded_by=current_user.id,
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)

    # 记录操作日志
    service = TaskService(db)
    await service._log_action(
        task_id, "file_uploaded", current_user.id,
        f"步骤{step.step}上传附件: {original_name} ({len(content)}字节)"
    )
    await db.commit()

    return {
        "id": rec.id,
        "step_id": rec.step_id,
        "task_id": rec.task_id,
        "original_name": rec.original_name,
        "file_size": rec.file_size,
        "mime_type": rec.mime_type,
        "category": rec.category,
        "uploaded_by": rec.uploaded_by,
        "uploaded_at": rec.uploaded_at.isoformat() if rec.uploaded_at else None,
    }


@router.get("/{task_id}/steps/{step_id}/files/{file_id}/download")
async def download_step_file(
    task_id: int,
    step_id: int,
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载附件"""
    stmt = select(StepFile).where(
        StepFile.id == file_id,
        StepFile.step_id == step_id,
        StepFile.task_id == task_id,
    )
    f = (await db.execute(stmt)).scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="附件不存在")

    file_path = os.path.join(UPLOAD_DIR, f.stored_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件已被删除")

    return FileResponse(
        path=file_path,
        filename=f.original_name,
        media_type=f.mime_type or "application/octet-stream",
    )


@router.delete("/{task_id}/steps/{step_id}/files/{file_id}")
async def delete_step_file(
    task_id: int,
    step_id: int,
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除附件"""
    stmt = select(StepFile).where(
        StepFile.id == file_id,
        StepFile.step_id == step_id,
        StepFile.task_id == task_id,
    )
    f = (await db.execute(stmt)).scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="附件不存在")

    # 权限校验
    step_stmt = select(TaskStep).where(TaskStep.id == step_id)
    step = (await db.execute(step_stmt)).scalar_one_or_none()
    if step:
        task_stmt = select(TestTask).where(TestTask.id == task_id)
        task = (await db.execute(task_stmt)).scalar_one_or_none()
        _check_step_permission(step, current_user, task)

    # 删盘文件
    file_path = os.path.join(UPLOAD_DIR, f.stored_name)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass

    # 删库记录
    await db.execute(delete(StepFile).where(StepFile.id == file_id))

    # 记录操作日志
    service = TaskService(db)
    await service._log_action(
        task_id, "file_deleted", current_user.id,
        f"步骤{step.step if step else '?'}删除附件: {f.original_name}"
    )
    await db.commit()

    return {"ok": True, "id": file_id}
