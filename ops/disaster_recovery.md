# 灾难恢复教程：服务器挂了，如何在另一台机器上快速全量恢复

适用：192.2.100.30 上的测试任务管理平台
配套脚本：`ops/offsite_backup.sh`（平时备份）、`ops/restore_to_new_host.sh`（恢复）

---

## 0. 先说结论（最重要的一句）

**现在所有备份都在生产本机 `/opt/testtask/backups` —— 机器一挂，备份一起没。**
所以灾难恢复的第一件事不是写教程，而是**把备份复制一份到别的机器**。

本教程分两部分：**平时怎么做离线备份** → **真出事了怎么恢复**。

---

## 1. 要能恢复，必须提前备齐这 5 样

| # | 内容 | 从哪来 | 没有会怎样 |
|---|---|---|---|
| 1 | **数据库** `db.sql` | `pg_dump`（`backup.sh` 自动做） | 所有任务/用例/用户全丢，无法恢复 |
| 2 | **附件** `uploads/`（31 个，12MB） | `backup.sh` 自动做；现已挂在宿主 `backend/uploads` | 版本配套表、环境自检报告打不开 |
| 3 | **后端源码** | ① GitHub 仓库（已补齐，可 clone）② 备份包里的 `backend/`（权威，含容器里的最新改动） | 起不来 |
| 4 | **前端** `frontend/index.html` | 同上 | 页面打不开 |
| 5 | **docker-compose.yml**（含挂载、healthcheck） | 备份包 + git 仓库 | 配置丢失，附件挂载会漏 |

**可选但强烈建议：docker 镜像 `images_*.tgz`（约 270MB）**
—— 新机器未必能连 Docker Hub（预发机 192.2.56.76 实测就拉不到）。带上镜像包 = 恢复时不依赖外网。

---

## 2. 平时：定时离线备份

在生产机上跑（默认备份到 `root@192.2.56.76:/opt/testtask-backups`）：

```bash
# 常规（db + 附件 + 源码 + 配置，几百 KB~十几 MB）
bash /opt/testtask/ops/offsite_backup.sh

# 连镜像一起备（新机拉不到镜像时用；约 270MB，建议每周一次）
bash /opt/testtask/ops/offsite_backup.sh --with-image
```

**2026-09-11 已在生产机装好 crontab**（`crontab -l` 可查）：
```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 每 2 小时整点：常规备份（db + 附件 + 源码 + 配置）
0 */2 * * * bash /opt/testtask/ops/offsite_backup.sh >> /var/log/testtask_backup.log 2>&1
# 每周日 03:30：连 docker 镜像一起备
30 3 * * 0 bash /opt/testtask/ops/offsite_backup.sh --with-image >> /var/log/testtask_backup.log 2>&1
```
- 备份方向：**生产 192.2.100.30 → 备份机 root@192.2.56.76:/opt/testtask-backups**
- 日志：`/var/log/testtask_backup.log`；检查是否真在跑：`ls -lt /opt/testtask-backups/`
- 想更密（RPO 更小）就把 `0 */2 * * *` 改成 `0 * * * *`（每小时）

> 远端默认保留最近 10 份包 + 3 份镜像，自动清理。

---

## 3. 故障分级与对应打法

| 级别 | 现象 | 打法 | 预计耗时 |
|---|---|---|---|
| **A. 服务挂了** | 页面打不开但机器能 SSH | `docker restart testtask-backend`；不行就用 `rollback_frontend.sh` / 恢复 `backups/app_*.tgz` | 1-5 分钟 |
| **B. 系统盘坏了但能读盘** | 能进救援模式/能挂盘 | 抢救 `/opt/testtask`（含 `backend/uploads`）到别的机器，用 `restore_to_new_host.sh` 恢复 | 30-60 分钟 |
| **C. 机器彻底没了** | 起不来/找不到 | **用离线备份在新机全量恢复**（下面第 4 节） | 15-30 分钟 |

B 级提醒：如果还能跑 `docker exec`，优先再补一次最新数据：
```bash
docker exec testtask-db pg_dump -U testtask -d testtask > /tmp/last.sql
```

---

## 4. C 级：新机全量恢复（重点）

### 4.0 准备新机器

- Ubuntu 22.04（或其他 Linux），装好 **docker + docker compose 插件**
- 磁盘 ≥ 20G，开放 **8888** 端口（可改，见下）
- 把备份包放到新机上（scp / U盘 / 从备份机拉）：
  ```bash
  scp root@192.2.56.76:/opt/testtask-backups/20260911_160000.tar.gz /tmp/
  scp root@192.2.56.76:/opt/testtask-backups/images_20260911_160000.tgz /tmp/   # 有就一起拿
  ```

### 4.1 一键恢复（推荐）

```bash
bash restore_to_new_host.sh /tmp/20260911_160000.tar.gz \
                            /opt/testtask 8888 \
                            /tmp/images_20260911_160000.tgz
```

脚本会依次做完：解包 → 还原目录（含附件）→ 改端口 → 载入镜像 → **先只起数据库** → 导入 `db.sql` → 起后端 → 健康检查。

### 4.2 手工步骤（脚本出问题时照这个来）

```bash
export COMPOSE_PROJECT_NAME=testtask          # 关键：让镜像名对得上
mkdir -p /tmp/dr && tar xzf 20260911_160000.tar.gz -C /tmp/dr
SRC=/tmp/dr/$(ls /tmp/dr | head -1)

mkdir -p /opt/testtask
cp -a $SRC/backend $SRC/frontend /opt/testtask/
cp -a $SRC/docker-compose.yml /opt/testtask/
mkdir -p /opt/testtask/backend/uploads && cp -a $SRC/uploads/. /opt/testtask/backend/uploads/

# 有镜像包就载入，没有只能靠 build/pull
gunzip -c images_*.tgz | docker load

cd /opt/testtask
docker compose up -d --no-build testtask-db     # 先只起库，避免 create_all 抢先建表
# 等 healthy
docker cp $SRC/db.sql testtask-db:/tmp/db.sql
docker exec testtask-db psql -U testtask -d testtask -f /tmp/db.sql
docker compose up -d --no-build                 # 再起后端
curl -s -o /dev/null -w "%{http_code}" http://localhost:8888/docs   # 应为 200
```

> ⚠️ **顺序不能反**：必须先起库导数据，再起后端。后端一启动会 `create_all`，
> 空库建表后再导入 `db.sql` 会主键冲突。

---

## 5. 恢复后验证清单

- [ ] `curl http://<新IP>:8888/docs` = 200，容器 `docker ps` 显示 healthy
- [ ] 浏览器打开，用原账号（admin / Admin@123）登录成功
- [ ] 任务数量与故障前一致：`docker exec testtask-db psql -U testtask -d testtask -c "select count(*) from test_tasks;"`
- [ ] 抽查一个任务：点进去看步骤、关联用例、执行结果正常
- [ ] **抽查附件能下载**：`GET /api/tasks/{tid}/steps/{sid}/files/{fid}/download` 返回 200
- [ ] 用例库、用例套件能打开
- [ ] 新建一个测试任务，走通步骤 1→2（确认写入正常）

---

## 6. 切换给大家用

- **前端 API 用的是相对路径**（`const API = ''`），**换机器/换 IP 不需要改任何代码**，只要 8888 端口映射一致
- 通知大家新地址；如果用域名，改 DNS 解析指向新机即可
- 旧机器如果能救回来，先**不要**同时对外服务（两套库会分叉）

---

## 7. RTO / RPO（心里有数）

| 指标 | 当前能力 | 说明 |
|---|---|---|
| **RPO**（最多丢多少数据） | **最多 2 小时**（每 2 小时备份一次） | 想更小改 crontab 为每小时；要**接近 0** 必须上 PostgreSQL 流复制（见下） |
| **RTO**（多久能恢复） | **15-30 分钟** | 有镜像包 ≈ 15 分钟；需要现拉镜像则看网速，可能 1 小时+ |

### ⚠️ 关于「0 数据丢失」——现在的方案做不到

备份是**时间点快照**，不是实时复制：

- 故障发生在两次备份之间 → **这段时间的改动会丢**（现在最多丢 2 小时）
- 例：16:00 备份完，17:50 宕机 → 16:00~17:50 之间新建/修改的任务、上传的附件全部丢失

要真正做到接近 0 丢失，只有一条路：**PostgreSQL 主从流复制**（生产主库 → 192.2.56.76 备库，实时同步）。
代价：需要改 compose、初始化备库、备库长期占资源，且要决定故障时是否自动切主。
当前是**单机单库**，没有复制 —— 如果业务上真的一分钟数据都不能丢，那这个要做，可以另开一次改动来搞。

---

## 8. 演练（每季度做一次，别等真出事）

在任意一台空闲机器上用**非 8888 端口**跑一遍，不影响生产：

```bash
NAME_SUFFIX=-dr DB_PORT=5435 bash restore_to_new_host.sh \
    /opt/testtask-backups/20260911_155227.tar.gz /opt/testtask-dr 8889 \
    /opt/testtask-backups/images_20260911_155227.tgz
# 验证完清理（项目名也要带后缀，否则会误删正在跑的那套）
cd /opt/testtask-dr && COMPOSE_PROJECT_NAME=testtask-dr docker compose down -v
rm -rf /opt/testtask-dr
```

> ⚠️ 血的教训：`NAME_SUFFIX` 只改容器名/数据卷还不够，**compose 项目名必须一起加后缀**。
> 2026-09-11 第一次演练时项目名没改，compose 认为「这是同一个项目」，
> 直接把预发正在跑的容器顶掉了（预发 8888 短暂中断）。脚本已修，但手工操作时务必注意。

**2026-09-11 演练实录**（在 192.2.56.76 上，与预发 8888 并存）：
```
演练实例 8889：login 200 / 任务 10 个 / 套件 200 / 用例库 1 个 / 附件下载 200
预发     8888：全程 200 未受影响
恢复耗时：约 2 分钟（含镜像载入 + 导入 81 行任务数据）
```

## 9. 当前离线备份位置

| 项 | 位置 |
|---|---|
| 备份包 | `root@192.2.56.76:/opt/testtask-backups/*.tar.gz`（保留 10 份） |
| 镜像包 | `root@192.2.56.76:/opt/testtask-backups/images_*.tgz`（保留 3 份） |
| 首份 | `20260911_155227.tar.gz` 25M + `images_20260911_155227.tgz` 261M |

> 备份机和预发在同一台机器上（192.2.56.76）。如果追求更高等级，建议再往 NAS / 对象存储 / 你的开发机同步一份。

演练要检查的：**备份包是否完整、脚本能否跑通、恢复后的数据对不对** —— 这三件事不演练根本不知道。
