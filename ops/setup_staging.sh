#!/bin/bash
# 搭建 / 刷新预发环境（staging）
#   - 目录：/opt/testtask-staging
#   - 端口：8899（生产是 8888），数据库映射到 5434（不暴露也行）
#   - 独立容器名 / 独立网络 / 独立数据卷，与生产完全隔离
#   - 每次执行都会用生产最新的 pg_dump 覆盖 staging 数据库
#
# 用法: bash /opt/testtask/ops/setup_staging.sh
set -u

PROD=/opt/testtask
STG=/opt/testtask-staging
TS=$(date +%Y%m%d%H%M%S)

if [ ! -d "$PROD" ]; then echo "✗ 生产目录不存在: $PROD"; exit 1; fi

if [ ! -d "$STG" ]; then
  echo "→ 首次搭建 staging..."
  mkdir -p "$STG"
  cp -r "$PROD/backend" "$STG/" 2>/dev/null
  cp -r "$PROD/frontend" "$STG/"
  cp "$PROD/docker-compose.yml" "$STG/"
  [ -f "$PROD/.env" ] && cp "$PROD/.env" "$STG/"
  [ -f "$PROD/start.sh" ] && cp "$PROD/start.sh" "$STG/"

  # 改名/改端口/改网络/改数据卷，避免与生产冲突
  sed -i 's/container_name: testtask-backend/container_name: testtask-backend-staging/' "$STG/docker-compose.yml"
  sed -i 's/container_name: testtask-db/container_name: testtask-db-staging/' "$STG/docker-compose.yml"
  sed -i 's/"8888:8888"/"8899:8888"/' "$STG/docker-compose.yml"
  sed -i 's/"5433:5432"/"5434:5432"/' "$STG/docker-compose.yml"
  sed -i 's/testtask-network/testtask-staging-network/g' "$STG/docker-compose.yml"
  sed -i 's/testtask-db-data/testtask-staging-db-data/g' "$STG/docker-compose.yml"
  # staging 的前端目录里清掉历史备份，避免混淆
  rm -f "$STG"/frontend/index.html.bak_* "$STG"/frontend/index.html.broken_*
  echo "✓ 已生成 $STG/docker-compose.yml"
else
  echo "→ staging 已存在，刷新前端与配置（后端代码会从生产 backend 重新构建）"
  rm -rf "$STG/backend" && cp -r "$PROD/backend" "$STG/"
fi

# 启动（构建镜像）
echo "→ 启动 staging 容器..."
cd "$STG" && docker compose up -d --build
sleep 5

# 等数据库就绪
for i in $(seq 1 30); do
  docker exec testtask-db-staging pg_isready -U testtask -d testtask >/dev/null 2>&1 && break
  sleep 1
done

# 用生产最新数据覆盖 staging 库
echo "→ 导出生产数据库..."
docker exec testtask-db pg_dump -U testtask -d testtask -Fc > /tmp/staging_$TS.dump
docker cp /tmp/staging_$TS.dump testtask-db-staging:/tmp/staging.dump
echo "→ 导入 staging 数据库（--clean 会先清空 staging 现有表）..."
docker exec testtask-db-staging pg_restore -U testtask -d testtask --clean --if-exists --no-owner /tmp/staging.dump 2>&1 | tail -3
docker exec testtask-db-staging rm -f /tmp/staging.dump
rm -f /tmp/staging_$TS.dump

# 重启 staging 后端，让它跑一遍 create_all 并加载新数据
docker restart testtask-backend-staging >/dev/null
sleep 6
CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8899/docs)
echo "✓ staging 就绪，健康检查 /docs = $CODE"
echo "  预发地址: http://192.2.100.30:8899"
