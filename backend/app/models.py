"""
数据模型 - 测试任务
"""
from sqlalchemy import Column, Integer, String, Date, Text, DateTime, ForeignKey, Enum, Boolean, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.database import Base
import enum


class TaskStatus(str, enum.Enum):
    init = "init"
    design = "design"
    version_confirm = "version_confirm"
    env_confirm = "env_confirm"
    env_approved = "env_approved"
    testing = "testing"
    review = "review"
    done = "done"
    closed = "closed"


class StepStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    rejected = "rejected"


class TestType(str, enum.Enum):
    integration = "integration"
    functional = "functional"
    performance = "performance"
    regression = "regression"


class Severity(str, enum.Enum):
    fatal = "fatal"
    serious = "serious"
    normal = "normal"
    minor = "minor"


class IssueStatus(str, enum.Enum):
    open = "open"
    assigned = "assigned"
    fixed = "fixed"
    verified = "verified"
    closed = "closed"
    rejected = "rejected"


# 用户表（从禅道导入/登录时自动创建）
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    account = Column(String(100), unique=True, nullable=False, index=True, comment="登录账号(禅道账号)")
    realname = Column(String(100), nullable=True, comment="真实姓名")
    role = Column(String(30), default="member", comment="全局角色 admin/member")
    is_active = Column(Boolean, default=True, comment="是否启用")
    last_login_at = Column(DateTime(timezone=True), nullable=True, comment="最后登录时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project_members = relationship("ProjectMember", back_populates="user")


# 项目表（从禅道同步）
class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    zentao_id = Column(Integer, unique=True, nullable=False, index=True, comment="禅道项目ID")
    name = Column(String(255), nullable=False, comment="项目名称")
    status = Column(String(30), default="doing", comment="项目状态")
    begin_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    pm_account = Column(String(100), nullable=True, comment="项目经理账号")
    team_members = Column(Text, nullable=True, comment="团队成员账号列表(JSON)")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    members = relationship("ProjectMember", back_populates="project")


# 项目成员角色表
class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        # 同一用户在同一项目可以有多个角色，但同一角色唯一
        {"mysql_charset": "utf8mb4"},
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project_role = Column(String(30), nullable=False, comment="项目角色 pl/tse/version_owner/executor")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="project_members")
    project = relationship("Project", back_populates="members")


# 测试任务主表
class TestTask(Base):
    __tablename__ = "test_tasks"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    project_id = Column(Integer, nullable=True, comment="关联项目ID")
    name = Column(String(255), nullable=False, comment="任务名称")
    product_name = Column(String(255), nullable=True, comment="待测产品名称")
    code = Column(String(50), unique=True, nullable=False, index=True, comment="任务编号")
    version = Column(String(100), nullable=True, comment="关联版本")
    test_type = Column(String(30), default=TestType.integration.value, comment="测试类型")
    begin_date = Column(Date, nullable=True, comment="开始日期")
    end_date = Column(Date, nullable=True, comment="结束日期")
    owner = Column(Integer, nullable=False, comment="PL负责人")
    tse_id = Column(Integer, nullable=True, comment="TSE负责人")
    version_owner = Column(Integer, nullable=True, comment="版本负责人")
    status = Column(String(30), default=TaskStatus.init.value, comment="状态")
    progress = Column(Integer, default=0, comment="进度百分比")
    current_step = Column(Integer, default=1, comment="当前步骤")
    description = Column(Text, nullable=True, comment="任务描述")
    deleted = Column(Boolean, default=False, comment="删除标记")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # 关联
    steps = relationship("TaskStep", back_populates="task", order_by="TaskStep.step")
    issues = relationship("TaskIssue", back_populates="task")
    actions = relationship("TaskAction", back_populates="task")


# 任务步骤表
class TaskStep(Base):
    __tablename__ = "task_steps"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("test_tasks.id"), nullable=False, index=True)
    step = Column(Integer, nullable=False, comment="步骤序号 1-11")
    step_name = Column(String(100), nullable=False, comment="步骤名称")
    step_type = Column(String(30), default="confirm", comment="步骤类型")
    assigned_to = Column(Integer, nullable=True, comment="负责人")
    status = Column(String(30), default=StepStatus.pending.value, comment="状态")
    begin_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)
    remark = Column(Text, nullable=True, comment="备注")
    ext_data = Column(JSON, nullable=True, comment="准出条件数据(JSON)")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    task = relationship("TestTask", back_populates="steps")


# 问题记录表
class TaskIssue(Base):
    __tablename__ = "task_issues"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("test_tasks.id"), nullable=False, index=True)
    bug_no = Column(String(50), unique=True, nullable=False, comment="问题编号")
    title = Column(String(255), nullable=False, comment="问题标题")
    severity = Column(String(20), default=Severity.normal.value, comment="严重程度")
    priority = Column(String(10), default="p3", comment="优先级")
    status = Column(String(30), default=IssueStatus.open.value, comment="状态")
    reporter = Column(Integer, nullable=True, comment="报告人")
    assigned_to = Column(Integer, nullable=True, comment="指派人")
    resolution = Column(String(100), nullable=True, comment="解决方案")
    steps = Column(Text, nullable=True, comment="复现步骤")
    description = Column(Text, nullable=True, comment="详细描述")
    zentao_bug_id = Column(Integer, nullable=True, comment="关联禅道Bug ID")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    task = relationship("TestTask", back_populates="issues")


# 操作日志表
class TaskAction(Base):
    __tablename__ = "task_actions"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("test_tasks.id"), nullable=False, index=True)
    object_type = Column(String(30), default="task", comment="对象类型")
    object_id = Column(Integer, nullable=True)
    action = Column(String(30), nullable=False, comment="操作类型")
    actor = Column(Integer, nullable=True, comment="操作人")
    comment = Column(Text, nullable=True, comment="操作备注")
    extra = Column(Text, nullable=True, comment="扩展数据")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    task = relationship("TestTask", back_populates="actions")
