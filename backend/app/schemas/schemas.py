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
    product_name: str = Field(..., min_length=1, max_length=255, description="待测产品名称")
    project_id: int = Field(..., description="所属项目ID")
    version: Optional[str] = None
    test_type: str = "integration"
    # 2026-11-26：以下字段全部必填。原因——它们直接决定 9 步流程的环节负责人
    # （owner→环节1/5、tse_id→环节2、version_owner→环节3/4/6/8、executor_id→环节7），
    # 任一留空都会让该环节 assigned_to 为 NULL，落到"角色兜底"这条软路径上
    # （历史 bug：任务119 TSE 留空 → 环节2 没人有权操作）。
    begin_date: date = Field(..., description="任务开始日期（必填）")
    end_date: date = Field(..., description="任务结束日期（必填）")
    owner: int = Field(..., description="PL 负责人（决定环节1/5）")
    tse_id: int = Field(..., description="TSE（决定环节2）")
    version_owner: int = Field(..., description="版本负责人（决定环节3/4/6/8）")
    executor_id: int = Field(..., description="测试执行人（决定环节7）")
    executors: Optional[List[dict]] = Field(None, description="额外测试执行人（可选，非必填）")
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
    project_name: Optional[str] = None
    code: str
    version: Optional[str]
    test_type: str
    begin_date: Optional[date]
    end_date: Optional[date]
    actual_end_date: Optional[datetime] = None  # 实际完成时间（末环完成封卷时写入，被打回重开则清空）
    owner: int
    tse_id: Optional[int]
    version_owner: Optional[int]
    executor_id: Optional[int] = None
    executors: Optional[List[dict]] = None
    status: str
    progress: int
    current_step: int
    current_step_assignee: Optional[int] = None
    current_step_assignee_name: Optional[str] = None  # 当前环节负责人姓名（从 task_steps + users 查）
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
    rounds: Optional[List[dict]] = None  # 环节各轮实际起止[{n,start,end,outcome}]
    remark: Optional[str]
    ext_data: Optional[dict] = None

    class Config:
        from_attributes = True


# === 问题相关 ===
class IssueCreate(BaseModel):
    task_id: Optional[int] = None
    case_link_id: Optional[int] = None   # 从用例失败/阻塞提单时关联
    issue_no: Optional[str] = None       # 外部缺陷系统的问题单号（本系统只记录）
    title: Optional[str] = Field(None, max_length=255)  # 轻量录入时可省略，由服务端兜底
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
    case_link_id: Optional[int] = None
    issue_no: Optional[str] = None       # 外部问题单号
    case_title: Optional[str] = None    # 关联用例标题（冗余，便于列表直接展示）
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


# === 问题单树（产品 → 项目 → 测试任务） ===
class IssueTreeNode(BaseModel):
    """树上的一个问题单"""
    id: int
    bug_no: str
    issue_no: Optional[str] = None       # 外部问题单号（优先展示）
    title: str
    severity: str
    status: str
    reporter: Optional[int] = None
    reporter_name: Optional[str] = None
    assigned_to: Optional[int] = None
    assigned_name: Optional[str] = None
    case_link_id: Optional[int] = None
    case_title: Optional[str] = None
    created_at: Optional[datetime] = None


class IssueTreeTask(BaseModel):
    task_id: Optional[int] = None
    task_name: str = "未归属任务"
    task_code: Optional[str] = None
    version: Optional[str] = None
    status: Optional[str] = None
    progress: int = 0
    current_step: int = 0
    # 2026-09-23 问题单第三层改造: 起止时间入树(前端砍掉进度/当前步骤/问题单三列后
    # 表头仍有"起止时间",此前的错位根因之一就是表头8列vs数据7格永远差一格格子)
    begin_date: Optional[date] = None
    end_date: Optional[date] = None
    issues: List[IssueTreeNode] = []


class IssueTreeProject(BaseModel):
    project_id: Optional[int] = None
    project_name: str = "未归属项目"
    tasks: List[IssueTreeTask] = []


class IssueTreeProduct(BaseModel):
    product_id: Optional[int] = None
    product_name: str = "未归属产品"
    projects: List[IssueTreeProject] = []


class IssueTreeResponse(BaseModel):
    total: int = 0        # 问题单总数
    open_count: int = 0   # 未关闭数
    products: List[IssueTreeProduct] = []


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
