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
    """校验：步骤 assignee 或 全员环节(assigned_to is None) 或 task 的 executors 才能上传/删除。
    admin 不再自动获得业务环节操作权；如需介入业务环节，需先把自己指派为该环节负责人。"""
    is_assignee = step.assigned_to == user.id
    is_all_hands = step.assigned_to is None
    # 执行测试环节(step==7) 允许任务的所有 executors 上传
    is_task_executor = False
    if task and step.step == 7:
        executor_ids = set()
        if task.executor_id:
            executor_ids.add(task.executor_id)
        if task.executors:
            for e in task.executors:
                if isinstance(e, dict) and e.get('user_id'):
                    executor_ids.add(e['user_id'])
        is_task_executor = user.id in executor_ids
    if not (is_assignee or is_all_hands or is_task_executor):
        raise HTTPException(
            status_code=403,
            detail=f"无权操作环节{step.step}「{step.step_name}」的附件，该环节负责人为他人",
        )


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
