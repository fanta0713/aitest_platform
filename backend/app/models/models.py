"""
数据模型 - 测试任务
"""
from sqlalchemy import Column, Integer, String, Date, Text, DateTime, ForeignKey, Enum, Boolean, JSON, UniqueConstraint
from sqlalchemy.orm import relationship, backref
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
    account = Column(String(100), unique=True, nullable=False, index=True, comment="登录账号")
    realname = Column(String(100), nullable=True, comment="真实姓名")
    password_hash = Column(String(255), nullable=True, comment="密码哈希(bcrypt)")
    role = Column(String(30), default="member", comment="主角色(兼容)")
    roles = Column(JSON, nullable=True, comment="角色列表[admin/pl/tse/version_owner/executor]")
    is_active = Column(Boolean, default=True, comment="是否启用")
    last_login_at = Column(DateTime(timezone=True), nullable=True, comment="最后登录时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project_members = relationship("ProjectMember", back_populates="user")


# 产品表（本地管理）
class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="产品名称")
    code = Column(String(100), nullable=True, comment="产品代号")
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# 项目表
class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    zentao_id = Column(Integer, nullable=True, index=True, comment="禅道项目ID(历史数据,可空)")
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True, comment="所属产品ID")
    name = Column(String(255), nullable=False, comment="项目名称")
    code = Column(String(100), nullable=True, comment="项目代号")
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
    project_id = Column(Integer, nullable=True, comment="关联禅道项目ID")
    name = Column(String(255), nullable=False, comment="任务名称")
    zentao_product_id = Column(Integer, nullable=True, comment="关联禅道产品ID")
    product_name = Column(String(255), nullable=True, comment="待测产品名称(缓存)")
    code = Column(String(50), unique=True, nullable=False, index=True, comment="任务编号")
    version = Column(String(100), nullable=True, comment="关联版本")
    test_type = Column(String(30), default=TestType.integration.value, comment="测试类型")
    begin_date = Column(Date, nullable=True, comment="开始日期")
    end_date = Column(Date, nullable=True, comment="结束日期")
    owner = Column(Integer, nullable=False, comment="PL负责人")
    tse_id = Column(Integer, nullable=True, comment="TSE负责人")
    version_owner = Column(Integer, nullable=True, comment="版本负责人")
    executor_id = Column(Integer, nullable=True, comment="测试执行人")
    executors = Column(JSON, nullable=True, comment="多人人力投入[{user_id,workload,remark}]")
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
    case_links = relationship("TaskCaseLink", back_populates="task", order_by="TaskCaseLink.id")

    @property
    def current_step_assignee(self):
        """当前环节的负责人"""
        for s in self.steps:
            if s.step == self.current_step:
                return s.assigned_to
        return None


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
    case_link_id = Column(Integer, ForeignKey("task_case_links.id"), nullable=True, index=True, comment="关联用例（从用例失败/阻塞提单时记录）")
    issue_no = Column(String(100), nullable=True, index=True, comment="问题单号（外部缺陷系统的单号，本系统只做关联记录）")
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


# 测试用例关联表（系统本地管理，用例数据在禅道）
class TaskCaseLink(Base):
    __tablename__ = "task_case_links"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("test_tasks.id"), nullable=False, index=True)
    zentao_case_id = Column(Integer, nullable=True, comment="禅道用例ID(迁移用,后续可为空)")
    local_case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=True, comment="本地用例ID")
    case_title = Column(String(500), nullable=True, comment="用例标题(缓存)")
    case_type = Column(String(30), nullable=True, comment="用例类型")
    case_priority = Column(Integer, nullable=True, comment="优先级")
    linked_by = Column(Integer, nullable=True, comment="关联人")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    task = relationship("TestTask", back_populates="case_links")
    results = relationship("CaseResult", back_populates="link", order_by="CaseResult.id")


# 用例执行结果表
class CaseResult(Base):
    __tablename__ = "case_results"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    link_id = Column(Integer, ForeignKey("task_case_links.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, comment="pass/fail/block/na")
    comment = Column(Text, nullable=True, comment="备注")
    executor_id = Column(Integer, nullable=True, comment="执行人")
    executed_at = Column(DateTime(timezone=True), server_default=func.now())
    step_results = Column(JSON, nullable=True, comment="步骤级结果[{step_idx, actual, status}]")

    link = relationship("TaskCaseLink", back_populates="results")


# 用例库表
class CaseLibrary(Base):
    __tablename__ = "case_libraries"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="用例库名称")
    description = Column(Text, nullable=True, comment="用例库描述")
    zentao_product_id = Column(Integer, nullable=True, comment="迁移来源禅道产品ID")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    modules = relationship("CaseModule", back_populates="library", order_by="CaseModule.id")
    cases = relationship("TestCase", back_populates="library")


# 用例模块表（树形结构）
class CaseModule(Base):
    __tablename__ = "case_modules"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    library_id = Column(Integer, ForeignKey("case_libraries.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False, comment="模块名称")
    parent_id = Column(Integer, ForeignKey("case_modules.id"), nullable=True, comment="父模块ID")
    sort = Column(Integer, default=0, comment="排序")
    zentao_module_id = Column(Integer, nullable=True, comment="迁移来源禅道模块ID")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    library = relationship("CaseLibrary", back_populates="modules")
    children = relationship("CaseModule", backref=backref("parent", remote_side="CaseModule.id"))
    cases = relationship("TestCase", back_populates="module")


# 测试用例表
class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    library_id = Column(Integer, ForeignKey("case_libraries.id"), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("case_modules.id"), nullable=True, index=True)
    title = Column(String(500), nullable=False, comment="用例标题")
    precondition = Column(Text, nullable=True, comment="前置条件")
    steps = Column(JSON, nullable=True, comment="步骤[{desc,expect}]")
    pri = Column(Integer, default=3, comment="优先级1-4")
    case_type = Column(String(30), default="feature", comment="类型: feature/performance/integration/etc")
    stage = Column(String(30), default="feature", comment="阶段: unit/feature/system")
    auto = Column(String(10), default="no", comment="是否自动化: yes/no")
    keywords = Column(String(200), nullable=True, comment="关键词")
    status = Column(String(20), default="normal", comment="状态: normal/draft")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    library = relationship("CaseLibrary", back_populates="cases")
    module = relationship("CaseModule", back_populates="cases")


# 步骤附件表（环境自检报告等）
class StepFile(Base):
    __tablename__ = "step_files"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    step_id = Column(Integer, ForeignKey("task_steps.id"), nullable=False, index=True, comment="步骤ID")
    task_id = Column(Integer, ForeignKey("test_tasks.id"), nullable=False, index=True, comment="任务ID（冗余方便查询）")
    original_name = Column(String(255), nullable=False, comment="原始文件名")
    stored_name = Column(String(255), nullable=False, comment="存储文件名（UUID前缀）")
    file_size = Column(Integer, nullable=False, comment="文件大小(字节)")
    mime_type = Column(String(100), nullable=True, comment="MIME类型")
    category = Column(String(30), default="env_check", comment="附件分类: env_check/其他")
    uploaded_by = Column(Integer, nullable=True, comment="上传人ID")
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())


# 用例套件表（收集固定用例，测试设计环节可一键批量关联到任务）
class CaseSuite(Base):
    __tablename__ = "case_suites"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="套件名称")
    description = Column(Text, nullable=True, comment="套件描述")
    created_by = Column(Integer, nullable=True, comment="创建人")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# 套件与用例的关联表
class SuiteCase(Base):
    __tablename__ = "suite_cases"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    suite_id = Column(Integer, ForeignKey("case_suites.id", ondelete="CASCADE"), nullable=False, index=True, comment="套件ID")
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True, comment="用例ID")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("suite_id", "case_id", name="uq_suite_case"),
    )
