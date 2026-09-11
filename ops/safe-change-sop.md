# 生产环境在线改动 SOP（保证别人正常使用）

适用：192.2.100.30 上的测试任务管理平台（`/opt/testtask`）
现状（2026-09-11 实测）：

| 项 | 现状 | 影响 |
|---|---|---|
| 前端 | bind mount `/opt/testtask/frontend/index.html` | 覆盖即生效，**改错整个页面立刻打不开** |
| 后端 | 单容器 `testtask-backend`，`docker cp` + `docker restart` | **实测重启中断 0.33 秒**（很小） |
| 数据库 | 单容器 postgres，数据在命名卷 | 改结构风险最高，且不可逆 |
| 附件 | `/app/uploads` **没挂载**，在容器可写层 | 重建容器会丢；也意味着**不能简单做双实例** |

**结论：真正的风险不是停机（0.33 秒），而是"坏代码上线且没有回滚"。**
所以核心策略是：**备份 + 自检 + 一键回滚 + 结构性改动先上预发环境**。

---

## 一、改动分级

| 级别 | 例子 | 风险 | 做法 |
|---|---|---|---|
| L1 纯展示 | 改列表列、改文案、加按钮 | 低 | 本地 `node --check` → 备份 → 覆盖前端 → 30 秒内验证；随时回滚 |
| L2 前端逻辑 | 新增交互、改 JS 函数 | 中 | 同上，但**必须先在预发环境点一遍** |
| L3 后端接口 | 改/加 API、改 SQL 查询 | 中高 | 先在预发验证 → import 自检 → 生产窗口（午休/下班）→ 健康检查 → 失败回滚 |
| L4 数据库结构 | 加表/加字段 = 低；**删字段/改类型/清数据 = 高** | 高 | 必须先 `pg_dump` 全量备份；`create_all` 只建不删（安全）；破坏性变更单独出 SQL 脚本，低峰执行 |

---

## 二、标准流程（每次改动都走）

1. **备份**
   ```bash
   bash /opt/testtask/ops/backup.sh            # 数据库全量
   # 前端会自动在覆盖前生成 index.html.bak_<时间戳>
   ```
2. **本地自检**
   - 前端：抽出 `<script>` 跑 `node --check`（必做，一次能避免整页白屏）
   - 后端：本地能 `python -c "import app.main"` 最好，至少确认改动文件语法 OK
3. **上预发**（L2 及以上必做）—— 预发是独立机器 **192.2.56.76**（root / 123），访问 http://192.2.56.76:8888
   ```bash
   # 3.1 先把生产当前代码刷到预发，保证预发起点的生产一致
   bash /opt/testtask/ops/refresh_staging.sh
   # 3.2 再把这次的改动同步到预发（在预发机上发布，或让助手推过去）
   # 3.3 在 http://192.2.56.76:8888 点一遍（含回归，不只是新功能）
   ```
4. **上生产**（验证通过后，挑个方便的时间）
   - **推荐一键同步**：`bash /opt/testtask/ops/sync_staging_to_prod.sh`
     （自动 diff 预发/生产代码 → 只发布差异文件 → 走自检 + 自动回滚；先看差异可以加 `--dry-run`）
   - 或者手动发：
     - 前端：`bash /opt/testtask/ops/deploy_frontend.sh /tmp/deploy/index.html`
     - 后端：`bash /opt/testtask/ops/deploy_backend.sh app/api/xxx.py`
   - **L3/L4 尽量放在 12:00-12:30 或 18:00 后**
5. **验证 + 留观** 主要页面点一遍，检查 `docker logs --tail=50 testtask-backend` 无异常
6. **出问题立刻回滚**（见下）

---

## 三、一键回滚

```bash
# 前端：列出最近备份
ls -t /opt/testtask/frontend/index.html.bak_* | head
# 回滚到指定备份（也可不带参数，默认回滚到最新一份）
bash /opt/testtask/ops/rollback_frontend.sh 20260911140000

# 后端：脚本自检失败会自动回滚；手工回滚
ls -t /opt/testtask/backups/app_*.tgz | head
tar xzf /opt/testtask/backups/app_20260911140000.tgz -C /tmp/restore
docker cp /tmp/restore/app/. testtask-backend:/app/app/
docker restart testtask-backend

# 数据库（最坏情况，会丢这段时间的新数据，谨慎）
bash /opt/testtask/ops/restore.sh /opt/testtask/backups/xxx.sql
```

---

## 四、预发环境（staging）

- 地址：`http://192.2.100.30:8899`（与生产 8888 完全隔离：独立容器名、独立网络、独立数据卷）
- 目录：`/opt/testtask-staging`
- 数据：搭建时用生产的 `pg_dump` 导入；可以反复 `setup_staging.sh` 刷新成最新生产数据
- 用途：L2 以上改动先在这里点一遍；DB 结构变更先在这里跑
- 注意：staging 的 `/app/uploads` 是自己的，不影响生产附件

---

## 五、已知隐患（2026-09-11 已全部修复）

1. ~~**`/app/uploads` 没挂载**~~ → **已修**：compose 加了 `- ./backend/uploads:/app/uploads`。
   附件已迁到宿主 `/opt/testtask/backend/uploads`（31 个文件，迁移前先 `docker cp` 导出，避免空目录覆盖丢失）。
   现在重建容器附件不丢，也具备做双实例/蓝绿的条件。
2. ~~**后端没有 healthcheck**~~ → **已修**：加了 30s 间隔探测 `/docs`，`docker ps` 现在显示 healthy。
3. ~~**Git 仓库不是完整副本**~~ → **已修**：补齐 `auth.py / users.py / caselib.py / suites.py / __init__.py / zentao.py`
   及 `models|schemas|services` 的 `__init__.py`，已提交并推送（commit `f3130d7`）。

## 六、⚠️ 重建容器前必做：先同步宿主源码

**血泪教训**：我们的后端改动一直是用 `docker cp` 直接灌进容器的，**不在镜像里，也不在宿主 `/opt/testtask/backend`**。
所以：

- 如果直接 `docker compose up -d`（不 build）→ 新容器从旧镜像起，**所有改动全部丢失**；
- 如果直接 `docker compose up -d --build` → 用**陈旧的宿主源码**构建，同样回退到旧代码（2026-09-11 实测宿主有 3 个文件是旧的：
  `caselib.py / schemas.py / task_service.py`）。

**正确顺序**：
```bash
docker exec testtask-backend tar czf /tmp/app.tgz -C /app app
docker cp testtask-backend:/tmp/app.tgz /tmp/app.tgz
cd /opt/testtask/backend && tar xzf /tmp/app.tgz     # 用容器源码反盖宿主
# 确认关键改动还在（例：grep _delete_cases_cascade app/api/caselib.py）
docker compose up -d --build
```
建议以后**每次 docker cp 之后都顺手同步一次宿主**，或干脆改成"改宿主 → build → 重建容器"的标准流程。

---

## 七、日常改动最小清单（贴墙版）

- [ ] 改代码（落在 git 里，别在预发现场改）
- [ ] 前端：`node --check` 抽取 `<script>` 校验
- [ ] `refresh_staging.sh` 把生产代码刷到预发 → 再把自己的改动发到预发
- [ ] 在 http://192.2.56.76:8888 点一遍（**含回归**）
- [ ] 验证通过 → `sync_staging_to_prod.sh --dry-run` 看差异 → 去掉参数正式同步
- [ ] L3/L4：放低峰时段（12:00-12:30 / 18:00 后）
- [ ] 改后：主流程点一遍 + 看日志 + 留观 10 分钟
- [ ] 出问题：立刻 `rollback_frontend.sh` / 恢复 app 备份

## 八、预发环境档案（2026-09-11 建成）

| 项 | 值 |
|---|---|
| 机器 | **192.2.56.76**（Ubuntu 22.04，root / 123，128 核 881G 内存，50G 可用磁盘） |
| 目录 | `/opt/testtask`（与生产同构） |
| 地址 | http://192.2.56.76:8888 |
| 数据库 | 独立容器 `testtask-db`，5433 端口 |
| 数据 | 生产 `pg_dump` 灌入（10 个在用任务 / 298 用例 / 11 用户，与生产一致） |
| 附件 | 31 个已复制到 `/opt/testtask/backend/uploads` |
| 镜像 | 从生产 `docker save` 传过来 `docker load`（该机拉不到 Docker Hub，只能用这招） |
| 免密 | 生产 root 的 ed25519 公钥已写入预发 `authorized_keys`，prod → staging 免密 |

常用命令（**在生产机上执行**）：
```bash
bash /opt/testtask/ops/refresh_staging.sh              # 生产代码 -> 预发
bash /opt/testtask/ops/refresh_staging.sh --with-data   # + 用生产最新数据重灌预发库
bash /opt/testtask/ops/sync_staging_to_prod.sh --dry-run # 看预发与生产的差异
bash /opt/testtask/ops/sync_staging_to_prod.sh           # 预发 -> 生产（带自检和回滚）
```
