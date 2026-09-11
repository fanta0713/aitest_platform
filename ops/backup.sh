#!/bin/bash
# ============================================================
#  测试任务管理平台 - 一键备份脚本
#  用法（在 192.2.100.30 上执行）：
#    bash /opt/testtask/ops/backup.sh
#    bash /opt/testtask/ops/backup.sh /data/backup/20260907   # 指定目录
#  产物：<目标目录>.tar.gz
# ============================================================
set -e

TS=$(date +%Y%m%d_%H%M%S)
BASE=${1:-/opt/testtask/backups/$TS}
mkdir -p "$BASE"
cd /opt/testtask

echo "==> [1/6] 导出数据库"
docker exec testtask-db pg_dump -U testtask testtask > "$BASE/db.sql"
echo "    db.sql $(du -h "$BASE/db.sql" | cut -f1)"

echo "==> [2/6] 导出附件 /app/uploads（注意：该目录未做宿主机挂载，只存在于容器内）"
docker cp testtask-backend:/app/uploads "$BASE/uploads" >/dev/null
echo "    $(ls "$BASE/uploads" | wc -l) 个文件"

echo "==> [3/6] 导出后端源码（权威版本，含 git 仓库里缺失的 auth.py/users.py/caselib.py）"
cp -a /opt/testtask/backend "$BASE/backend"

echo "==> [4/6] 导出前端"
cp -a /opt/testtask/frontend "$BASE/frontend"

echo "==> [5/6] 导出配置文件"
cp -a /opt/testtask/docker-compose.yml "$BASE/" 2>/dev/null || true
cp -a /opt/testtask/.env "$BASE/" 2>/dev/null || true
cp -a /opt/testtask/start.sh "$BASE/" 2>/dev/null || true

echo "==> [6/6] 打包"
tar -czf "${BASE}.tar.gz" -C "$(dirname "$BASE")" "$(basename "$BASE")"
echo ""
echo "备份完成：${BASE}.tar.gz  ($(du -h "${BASE}.tar.gz" | cut -f1))"
echo "目录内容："
ls -lh "$BASE"

# 保留最近 10 份，自动清理更老的
ls -1t /opt/testtask/backups/*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f
