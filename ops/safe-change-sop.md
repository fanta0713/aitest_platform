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
3. **上预发**（L2 及以上必做）
   ```bash
   bash /opt/testtask/ops/setup_staging.sh     # 首次搭建/刷新，之后访问 http://192.2.100.30:8899
   bash /opt/testtask/ops/deploy_frontend.sh /tmp/deploy/index.html staging
   bash /opt/testtask/ops/deploy_backend.sh app/api/xxx.py staging
   ```
4. **上生产**
   - 前端：`bash /opt/testtask/ops/deploy_frontend.sh /tmp/deploy/index.html`（自动备份 + 原子替换）
   - 后端：`bash /opt/testtask/ops/deploy_backend.sh app/api/xxx.py`（自动备份容器代码 → 拷入 → import 自检 → 重启 → 健康检查，失败自动回滚）
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

## 五、当前已知隐患（建议排一次窗口修掉）

1. **`/app/uploads` 没挂载** —— 现在附件在容器可写层，重建容器就丢；也导致**无法做双实例/蓝绿**（两个实例各存各的附件）。
   修法：`docker-compose.yml` 的 testtask-backend 加 `- ./backend/uploads:/app/uploads`，重建容器（会中断约 1 分钟，需窗口）。
2. **后端没有 healthcheck** —— 现在 db 有、backend 没有；加了之后 `docker restart` 能自动判断就绪，配合 `restart: unless-stopped` 更稳。
3. **Git 仓库不是完整副本**（缺 `auth.py / users.py / caselib.py / suites.py`）—— 真要恢复只能靠容器/宿主源码。建议补齐后提交，作为最后一道防线。

---

## 六、日常改动最小清单（贴墙版）

- [ ] 改前：`ops/backup.sh`（DB）+ 前端自动备份
- [ ] 前端：`node --check` 抽取 `<script>` 校验
- [ ] L2+：先上 8899 预发点一遍
- [ ] 后端：拷入后 `docker exec python -c "import app.main"` 自检，失败不重启
- [ ] L3/L4：放低峰时段
- [ ] 改后：主流程点一遍 + 看日志 + 留观 10 分钟
- [ ] 出问题：立刻 `rollback_frontend.sh` / 恢复 app 备份
