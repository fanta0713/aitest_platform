# 测试任务管理平台 - 备份与异地恢复

> 适用对象：192.2.100.30 上的 `/opt/testtask`（testtask-backend + testtask-db）
> 最近核查：2026-09-07

## 一、备份什么（5 类资产）

| # | 资产 | 位置 | 说明 |
|---|------|------|------|
| 1 | 数据库 | 容器 `testtask-db` 内 `testtask` 库 | 在 **Docker 命名卷** `testtask_testtask-db-data` 里，不是宿主机目录，必须 `pg_dump` 导出 |
| 2 | 附件 | 容器 `testtask-backend` 内 `/app/uploads` | ⚠️ **未做宿主机挂载**，只存在于容器可写层，必须 `docker cp` 导出 |
| 3 | 后端源码 | `/opt/testtask/backend` | ⚠️ **权威版本**，含 git 仓库里缺失的 `auth.py`/`users.py`/`caselib.py` |
| 4 | 前端 | `/opt/testtask/frontend` | 宿主机 bind mount，直接拷目录 |
| 5 | 配置 | `/opt/testtask/.env`、`docker-compose.yml` | `.env` 含 `SECRET_KEY`，恢复时保持一致 |

当前体量（2026-09-07）：数据库 9.7MB（dump 后 ~412KB）、附件 23MB / 25 个文件、任务 71 条、用例 276 条 —— 全量备份极小，可高频执行。

## 二、备份（在 192.2.100.30 上）

```bash
bash /opt/testtask/ops/backup.sh
# 产物：/opt/testtask/backups/<时间戳>.tar.gz
# 默认保留最近 10 份
```

把包传到异地（示例）：

```bash
scp /opt/testtask/backups/<时间戳>.tar.gz user@<异地机>:/data/backup/
```

建议加个定时任务（每天 02:00）：

```cron
0 2 * * * bash /opt/testtask/ops/backup.sh >> /var/log/testtask_backup.log 2>&1
```

## 三、恢复（在异地新机器上）

```bash
# 1. 前置：安装 Docker + Docker Compose
# 2. 解压备份
tar -xzf testtask_backup_<时间戳>.tar.gz -C /tmp

# 3. 执行恢复
bash /tmp/<时间戳>/../restore.sh /tmp/<时间戳>
# 若备份目录里没有 restore.sh，用本目录的：
bash restore.sh /tmp/<时间戳>
```

脚本会依次：还原源码/前端/配置 → `docker compose up -d --build` → 等 DB 就绪 →
`DROP SCHEMA public CASCADE` 清掉自动建的空表 → 导入 `db.sql` →
`docker cp` 还原附件 → 重启后端。

**换 IP 无需改代码**：前端 `const API = ''`（相对路径），只要 8888 端口映射不变即可；
若端口要改，同步改 `docker-compose.yml` 的 `8888:8888`。

## 四、两个必须尽快修的隐患

### 隐患 1：附件目录没有挂载（高危）

`docker inspect testtask-backend` 只有 1 个 bind mount：

```
bind | src=/opt/testtask/frontend | dst=/app/frontend
```

`/app/uploads` 在容器可写层里，**不在任何 volume/bind 中**。后果：
- `docker-compose down` 再 `up --build` 重建容器 → 25 个附件全部丢失
- 但 `step_files` 表记录还在 → 变成"记录有、文件 404"的孤儿数据

修复：在 `/opt/testtask/docker-compose.yml` 的 backend volumes 里补一行，然后重建：

```yaml
    volumes:
      - ./frontend:/app/frontend
      - ./backend/uploads:/app/uploads   # ← 新增
```

> 注意：本地仓库的 `docker-compose.yml` 里**已经有**这一行，服务器上的版本没有，两边已不一致。
> 修复前先 `docker cp` 把现有 25 个附件导出到 `/opt/testtask/backend/uploads`，避免重建时丢失。

### 隐患 2：Git 仓库不是完整副本（恢复时致命）

`git ls-files backend/app/api` 只有 5 个文件：

```
cases.py  export.py  files.py  products.py  tasks.py
```

但 `main.py` 里写的是：

```python
from app.api import tasks, auth, users, cases, caselib, products, files, export
```

服务器上实际有 `auth.py`、`users.py`、`caselib.py`、`zentao.py`、`__init__.py` —— **这些从未提交到 GitHub**。
只从 `fanta0713/aitest_platform.git` clone 下来构建，启动会直接 `ImportError` 崩溃。

修复：把缺失文件补提交（建议在服务器或本地补上后 push）：

```bash
git add backend/app/api/auth.py backend/app/api/users.py backend/app/api/caselib.py backend/app/api/__init__.py
git commit -m "补齐未提交的 auth/users/caselib 路由模块"
git -c credential.helper=manager push origin main
```

## 五、与禅道的关系

**运行时已完全脱钩**，详见上级目录的核查结论。残留仅为：
- 服务器上 `backend/app/api/zentao.py`、`backend/app/core/zentao.py` 两个**死文件**（`main.py` 未注册 zentao router）
- `.env` / `docker-compose.yml` 里的 `ZENTAO_URL`、`ZENTAO_TOKEN`、`ZENTAO_ADMIN_PASSWORD`（代码里无引用）
- 数据表里的历史迁移列：`test_tasks.zentao_product_id`、`projects.zentao_id`、`task_case_links.zentao_case_id`、`task_issues.zentao_bug_id`、`case_libraries.zentao_product_id`、`case_modules.zentao_module_id`

这些都不影响运行，可择机清理。服务器上仍在运行的 `zentao`/`zentao-mysql`/`zentao-redis` 容器是另一套独立的禅道 ALM，与本平台无调用关系。
