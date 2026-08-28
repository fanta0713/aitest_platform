"""
Pydantic schemas - API请求验证
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, date


# === 任务相关 ===
class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="任务名称")
    zentao_product_id: Optional[int] = Field(None, description="禅道产品ID")
    product_name: Optional[str] = Field(None, description="待测产品名称")
    project_id: Optional[int] = None
    version: Optional[str] = None
    test_type: str = "integration"
    begin_date: Optional[date] = None
    end_date: Optional[date] = None
    owner: int
    tse_id: Optional[int] = None
    version_owner: Optional[int] = None
    executor_id: Optional[int] = None
    executors: Optional[List[dict]] = None
    description: Optional[str] = None


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    zentao_product_id: Optional[int] = None
    product_name: Optional[str] = None
    project_id: Optional[int] = None
    version: Optional[str] = None
    test_type: Optional[str] = None
    begin_date: Optional[date] = None
    end_date: Optional[date] = None
    owner: Optional[int] = None
    tse_id: Optional[int] = None
    version_owner: Optional[int] = None
    executor_id: Optional[int] = None
    executors: Optional[List[dict]] = None
    description: Optional[str] = None


class TaskStepUpdate(BaseModel):
    status: Optional[str] = Field(None, description="状态: in_progress, completed, rejected")
    remark: Optional[str] = Field(None, description="备注")
    ext_data: Optional[dict] = Field(None, description="准出条件数据")


class TaskResponse(BaseModel):
    id: int
    name: str
    zentao_product_id: Optional[int] = None
    product_name: Optional[str] = None
    project_id: Optional[int] = None
    code: str
    version: Optional[str]
    test_type: str
    begin_date: Optional[date]
    end_date: Optional[date]
    owner: int
    tse_id: Optional[int]
    version_owner: Optional[int]
    executor_id: Optional[int] = None
    executors: Optional[List[dict]] = None
    status: str
    progress: int
    current_step: int
    current_step_assignee: Optional[int] = None
    current_step_name: Optional[str] = None  # 当前环节名称（从 task_steps 查，避免流程改造后错位）
    description: Optional[str]
    created_at: datetime
    
    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    total: int
    items: List[TaskResponse]


# === 步骤相关 ===
class StepResponse(BaseModel):
    id: int
    task_id: Optional[int] = None
    step: int
    step_name: str
    step_type: str
    assigned_to: Optional[int]
    status: str
    begin_date: Optional[datetime]
    end_date: Optional[datetime]
    remark: Optional[str]
    ext_data: Optional[dict] = None

    class Config:
        from_attributes = True


# === 问题相关 ===
class IssueCreate(BaseModel):
    task_id: Optional[int] = None
    title: str = Field(..., min_length=1, max_length=255)
    severity: str = "normal"
    priority: str = "p3"
    assigned_to: Optional[int] = None
    steps: Optional[str] = None
    description: Optional[str] = None


class IssueUpdate(BaseModel):
    title: Optional[str] = None
    severity: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[int] = None
    resolution: Optional[str] = None


class IssueResponse(BaseModel):
    id: int
    task_id: Optional[int] = None
    bug_no: str
    title: str
    severity: str
    priority: str
    status: str
    reporter: Optional[int]
    assigned_to: Optional[int]
    resolution: Optional[str]
    created_at: datetime
    
    class Config:
        from_attributes = True


# === 操作历史 ===
class ActionResponse(BaseModel):
    id: int
    task_id: Optional[int] = None
    action: str
    actor: Optional[int]
    comment: Optional[str]
    created_at: datetime
    
    class Config:
        from_attributes = True


# === 禅道数据 ===
class ZentaoUser(BaseModel):
    id: int
    account: str
    realname: str
    dept: Optional[int] = None


class ZentaoProject(BaseModel):
    id: int
    name: str
    code: str


# === 统计 ===
class StatsResponse(BaseModel):
    total: int
    active: int
    done: int
