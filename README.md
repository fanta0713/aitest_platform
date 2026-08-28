# 测试任务管理系统 (Test Task Manager)

一套面向测试团队的**测试任务全生命周期管理平台**，从任务发起到归档共 **10 个标准化环节**，覆盖测试设计、环境确认、用例分配、执行测试（步骤级结果录入）、测试报告整理、数据审核等全流程。

> 后端：FastAPI + SQLAlchemy async + PostgreSQL  
> 前端：单 HTML 文件（vanilla JS，约 3500 行）  
> 部署：Docker Compose 一键启动

---

## 目录结构

```
test_task/
├── backend/                        # 后端服务
│   ├── Dockerfile                 # 后端镜像构建
│   ├── requirements.txt           # Python 依赖清单
│   └── app/
│       ├── __init__.py
│       ├── main.py                 # FastAPI 入口（CORS / 路由注册 / 静态文件挂载）
│       ├── core/
│       │   ├── config.py          # 配置（环境变量 → Settings）
│       │   └── security.py         # JWT / PBKDF2 密码 / 鉴权依赖
│       ├── db/
│       │   └── database.py         # 异步 SQLAlchemy 会话
│       ├── models/
│       │   └── models.py           # 所有 ORM 模型（10+ 表）
│       ├── schemas/
│       │   └── schemas.py         # Pydantic 响应 schema
│       ├── services/
│       │   ├── task_service.py    # 任务流程编排核心（10 步流转）
│       │   └── user_service.py    # 用户/项目成员
│       └── api/                    # API 路由（按业务域拆分）
│           ├── tasks.py           # 任务 CRUD / 步骤推进 / 打回流转
│           ├── auth.py            # 登录 / 鉴权
│           ├── users.py           # 用户管理
│           ├── cases.py           # 用例关联 + 步骤级结果录入
│           ├── caselib.py         # 用例库 CRUD
│           ├── products.py        # 产品 / 项目 CRUD
│           └── files.py           # 步骤附件上传/下载
│
├── frontend/
│   └── index.html                 # 前端单文件（vanilla JS）
│
├── docker-compose.yml             # 一键编排（backend + db）
├── .env.example                   # 环境变量样例
├── .gitignore
└── README.md
```

---

## 技术栈

| 层 | 技术 | 版本 |
|---|---|---|
| 后端框架 | FastAPI | 0.109.0 |
| ASGI 服务器 | uvicorn[standard] | 0.27.0 |
| ORM | SQLAlchemy (async) | 2.0.25 |
| 数据库驱动 | asyncpg + psycopg2-binary | 0.29 / 2.9.9 |
| 数据库 | PostgreSQL | 15-alpine |
| 数据校验 | Pydantic + pydantic-settings | 2.5.3 / 2.1.0 |
| 鉴权 | python-jose (JWT) + passlib[bcrypt] | 3.3.0 / 1.7.4 |
| 文件上传 | python-multipart | 0.0.6 |
| 前端 | Vanilla JS（无构建步骤） | — |
| 部署 | Docker Compose | — |

---

## 10 步测试任务流程

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│ 1.任务发起│→│ 2.测试设计│→│ 3.版本配套│→│ 4.环境确认│→│ 5.环境审核│
│   (PL)   │  │  (TSE)   │  │(版本负责人)│  │(版本负责人)│  │   (PL)   │
└──────────┘  └──────────┘  └──────────┘  └──────────┘  └────┬─────┘
                                                              │ 通过
┌──────────────────────────────────────────────────────────────▼──────────┐
│ 6. 测试用例分配 → 7. 执行测试（步骤级结果+附件） → 8. 测试完成  │
│    (版本负责人)        (执行人 + 其他 executors)         (版本负责人) │
└──────────────────────────────────────────────────────────────┬──────────┘
                                                              │
┌──────────────────────────────────────────────────────────────▼──────────┐
│ 9. 数据审核 → 10. 任务结束  │
│   (版本负责人/TSE)     (PL)│
└───────────────────────────┘
```

### 打回流转规则

- **步骤5（环境审核）打回** → 步骤4（环境确认）退回重做，步骤5自身也重置为 pending 等待重新审核
- **步骤9（数据审核）打回** → 步骤5~8 全部退回重做，且步骤5处理人临时改为版本负责人（由版本负责人牵头补测），步骤9自身重置为 pending

---

## 核心数据模型（节选）

| 表 | 用途 |
|---|---|
| `users` | 用户（账号、PBKDF2 密码哈希、roles 多角色 JSON） |
| `products` / `projects` | 产品 → 项目（一对多） |
| `test_tasks` | 测试任务主表 |
| `task_steps` | 任务的 10 个环节实例（assigned_to / status / begin_date / end_date / ext_data JSON） |
| `task_actions` | 操作历史日志 |
| `case_libraries` / `case_modules` / `test_cases` | 用例库三级结构（用例 steps 是 JSON 数组 `[{desc, expect}]`） |
| `task_case_links` | 任务-用例关联 |
| `case_results` | 用例执行结果（status / comment / step_results JSON 步骤级结果） |
| `step_files` | 步骤附件（category 区分用途：env_check / case_step:{link_id}:{step_idx}） |

---

## 权限模型

操作权严格按步骤的 `assigned_to` 字段判断：

- **业务环节操作**：只有步骤 assignee 或全员环节（`assigned_to IS NULL`）能操作
- **admin 角色不再自动获得业务环节操作权**——admin 仍可管理用户/产品/项目和查看所有任务，但若要介入业务环节，需先在"人员与角色"页面把自己指派为该环节负责人
- **附件上传**：步骤 assignee 或全员环节才能上传/删除；执行测试环节允许任务的所有 executors 上传

> 这避免了"审核人打回后还能操作被退回环节"的越权问题。

---

## 一键部署（Docker Compose）

### 1. 准备环境

```bash
# 克隆
git clone https://github.com/fanta0713/aitest_platform.git
cd aitest_platform

# 复制环境变量样例（生产请修改 SECRET_KEY 和 DB_PASSWORD）
cp .env.example .env
```

### 2. 启动

```bash
docker compose up -d --build
```

启动后：
- 后端 API：`http://<服务器IP>:8888`
- API 文档（Swagger）：`http://<服务器IP>:8888/docs`
- 前端页面：`http://<服务器IP>:8888/`（前端由后端静态文件服务挂载）

### 3. 首次登录

系统启动时会自动建表。默认管理员账号：

| 账号 | 密码 |
|---|---|
| `admin` | `Admin@123` |

> 其他用户初始密码规则：用户名首字母大写 + `@123`（如 `lidongpo` → `Lidongpo@123`）

### 4. 数据库直连（调试用）

PostgreSQL 暴露在宿主机 `5433` 端口：

```bash
psql -h <服务器IP> -p 5433 -U testtask -d testtask
# 密码：testtask123（或你在 .env 里设置的 DB_PASSWORD）
```

---

## 本地开发

### 后端

```bash
cd backend
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 用 uvicorn 启动（需要数据库已就绪）
uvicorn app.main:app --host 0.0.0.0 --port 8888 --reload
```

### 前端

前端是单 HTML 文件，不需要构建。直接编辑 `frontend/index.html`，后端通过 volume mount 自动同步到容器。

### 改动部署流程

```bash
# 后端改动 → 进容器（uvicorn --reload 会自动重载）
scp backend/app/xxx.py root@<server>:/tmp/xxx.py
ssh root@<server> "docker cp /tmp/xxx.py testtask-backend:/app/app/对应路径"

# 前端改动 → volume mount 自动生效
scp frontend/index.html root@<server>:/opt/testtask/frontend/index.html

# 看日志
ssh root@<server> "docker logs --tail 30 testtask-backend"
```

---

## 主要 API 端点

### 鉴权
- `POST /api/auth/login` — 登录（设 cookie）
- `GET /api/auth/me` — 当前用户

### 任务
- `GET /api/tasks` — 任务列表（支持 `?status=` / `?mine=true` 过滤）
- `POST /api/tasks` — 创建任务（自动初始化 10 个环节）
- `GET /api/tasks/{id}` — 任务详情（含 current_step_name）
- `GET /api/tasks/{id}/steps` — 任务的所有步骤
- `PUT /api/tasks/{id}/steps/{sid}` — 更新步骤（保存 ext_data / 推进状态 / 打回）

### 用例
- `GET /api/tasks/{id}/cases` — 任务关联用例（含 steps / precondition / latest_result.step_results）
- `POST /api/tasks/{id}/cases/{lid}/result` — 记录用例执行结果（含步骤级 step_results）
- `GET /api/tasks/{id}/cases/stats` — 通过/失败/阻塞统计

### 附件
- `POST /api/tasks/{tid}/steps/{sid}/files` — 上传附件（category 区分用途）
- `GET /api/tasks/{tid}/steps/{sid}/files` — 列出附件
- `GET /api/tasks/{tid}/steps/{sid}/files/{fid}/download` — 下载
- `DELETE /api/tasks/{tid}/steps/{sid}/files/{fid}` — 删除

### 用户 / 产品 / 项目 / 用例库
- `GET/POST /api/users` — 用户管理
- `GET/POST /api/products` — 产品
- `GET/POST /api/projects` — 项目（关联到产品）
- `GET/POST /api/caselib/...` — 用例库三级结构 CRUD

完整 API 文档请访问运行中的 `/docs`（Swagger UI）。

---

## 关键设计决策

1. **流程版本兼容**：后端返回 `current_step_name`（从 `task_steps` 表查），即使流程步骤顺序调整，已有任务的步骤名也不会错位。

2. **附件按 category 区分用途**：
   - `env_check` — 步骤4 环境自检报告
   - `case_step:{link_id}:{step_idx}` — 步骤7 执行测试中某用例某步骤的失败/阻塞附件

3. **步骤级测试结果录入**：用例的每个步骤都可单独填通过/失败/阻塞 + 实际情况 + 附件，整体状态自动汇总（任一失败→用例失败，任一阻塞→用例阻塞）。

4. **环境变量集中配置**：所有配置来自环境变量（`.env` 文件），密钥只出现在 `.env`，绝不写入代码库。`.env` 已加入 `.gitignore`。

5. **单 HTML 前端**：前端是 vanilla JS 单文件，不需要构建步骤，部署简单，volume mount 即可热更新。

---

## 已知坑（开发者必读）

1. **后端有 3 个 `/api/projects` GET 路由**（users.py / products.py / zentao.py），按 main.py 注册顺序 `users → products → zentao`，**前者覆盖后者**。如需修改 `/api/projects` 返回字段，改 users.py 的 `UserService.list_projects` 才生效。

2. **前端 `let` 变量不是 window 属性**：`let currentUser / currentTask / stepCache` 不能用 `window.currentUser` 访问，playwright 测试脚本必须用闭包 `page.evaluate("currentUser ? currentUser.account : null")`。

3. **asyncpg 不接受 str 直接插入 DATE 列**：Pydantic schema 里日期字段必须用 `Optional[date]` 而不是 `Optional[str]`。

4. **前端树渲染必须深度优先**：`map(m => render(m) + render(children)).join('')` 一次完成；拆成两个 map 拼接会变成广度优先。

---

## License

本项目仅用于内部测试团队，未公开发布。如需使用请联系作者。
