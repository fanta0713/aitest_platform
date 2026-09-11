#!/bin/bash
# ============================================================
#  在新机器上从备份包全量恢复测试任务管理平台
#
#  用法（新机器上执行，需要已安装 docker + docker compose）：
#    bash restore_to_new_host.sh <bundle.tar.gz> [目标目录] [对外端口] [images.tgz]
#
#  例：
#    bash restore_to_new_host.sh /opt/testtask-backups/20260911_160000.tar.gz
#    bash restore_to_new_host.sh /opt/testtask-backups/20260911_160000.tar.gz /opt/testtask 8888
#    bash restore_to_new_host.sh /opt/testtask-backups/20260911_160000.tar.gz /opt/testtask 8888 \
#                                /opt/testtask-backups/images_20260911_160000.tgz
# ============================================================
set -u

BUNDLE=${1:-}
DEST=${2:-/opt/testtask}
PORT=${3:-8888}
IMAGES=${4:-}
DB_PORT=${DB_PORT:-5433}
# 演练用：给容器名/数据卷加后缀，避免和机器上已有的实例冲突（正式恢复留空）
NAME_SUFFIX=${NAME_SUFFIX:-}

[ -z "$BUNDLE" ] && { echo "用法: $0 <bundle.tar.gz> [目标目录] [端口] [images.tgz]"; exit 1; }
[ -f "$BUNDLE" ] || { echo "✗ 备份包不存在: $BUNDLE"; exit 1; }
command -v docker >/dev/null || { echo "✗ 没装 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "✗ 没装 docker compose 插件"; exit 1; }

# 固定项目名，保证 compose 生成的镜像名与备份里的 testtask-testtask-backend 对得上
# 演练(NAME_SUFFIX 非空)时项目名一并加后缀，否则会和机器上已有的实例抢同一个 compose 项目，
# 把人家正在跑的容器顶掉（2026-09-11 演练时踩过：直接用 NAME_SUFFIX 但项目名没改 → 预发容器被重建）
if [ -n "$NAME_SUFFIX" ]; then
  export COMPOSE_PROJECT_NAME="testtask${NAME_SUFFIX}"
else
  export COMPOSE_PROJECT_NAME=testtask
fi

TMP=/tmp/dr_restore_$$
rm -rf "$TMP" && mkdir -p "$TMP"
echo "==> [1/7] 解包备份"
tar xzf "$BUNDLE" -C "$TMP" || exit 1
SRC="$TMP/$(ls "$TMP" | head -1)"
ls "$SRC" || exit 1

echo "==> [2/7] 还原目录到 $DEST"
mkdir -p "$DEST"
[ -d "$SRC/backend" ]  && cp -a "$SRC/backend"  "$DEST/"
[ -d "$SRC/frontend" ] && cp -a "$SRC/frontend" "$DEST/"
[ -f "$SRC/docker-compose.yml" ] && cp -a "$SRC/docker-compose.yml" "$DEST/"
[ -f "$SRC/.env" ] && cp -a "$SRC/.env" "$DEST/"
[ -f "$SRC/start.sh" ] && cp -a "$SRC/start.sh" "$DEST/"

# 附件：优先用源码目录里已挂载的 backend/uploads，否则用 bundle 里单独导出的 uploads/
mkdir -p "$DEST/backend/uploads"
if [ -z "$(ls -A "$DEST/backend/uploads" 2>/dev/null)" ] && [ -d "$SRC/uploads" ]; then
  cp -a "$SRC/uploads/." "$DEST/backend/uploads/"
fi
echo "    附件 $(ls "$DEST/backend/uploads" | wc -l) 个"

echo "==> [3/7] 端口改为 $PORT，容器名后缀 '${NAME_SUFFIX}'"
sed -i "s/\"8888:8888\"/\"$PORT:8888\"/" "$DEST/docker-compose.yml"
# 数据库宿主端口（新机上 5433 可能被占用，可改）
sed -i "s/\"5433:5432\"/\"${DB_PORT}:5432\"/" "$DEST/docker-compose.yml"
if [ -n "$NAME_SUFFIX" ]; then
  sed -i "s/container_name: testtask-backend/container_name: testtask-backend$NAME_SUFFIX/" "$DEST/docker-compose.yml"
  sed -i "s/container_name: testtask-db/container_name: testtask-db$NAME_SUFFIX/" "$DEST/docker-compose.yml"
  sed -i "s/testtask-db-data/testtask-db-data$NAME_SUFFIX/g" "$DEST/docker-compose.yml"
fi
DB_CT="testtask-db${NAME_SUFFIX}"
BE_CT="testtask-backend${NAME_SUFFIX}"

echo "==> [4/7] 载入 docker 镜像"
if [ -n "$IMAGES" ] && [ -f "$IMAGES" ]; then
  gunzip -c "$IMAGES" | docker load || echo "    （镜像载入失败，将尝试本地构建/拉取）"
  # 演练时项目名带了后缀，镜像名也要对齐
  if [ -n "$NAME_SUFFIX" ]; then
    docker tag testtask-testtask-backend:latest "${COMPOSE_PROJECT_NAME}-testtask-backend:latest" 2>/dev/null
  fi
elif [ -n "$IMAGES" ]; then
  echo "    （镜像包不存在: $IMAGES，跳过）"
else
  echo "    未提供镜像包，依赖本机已有镜像或能访问 Docker Hub"
fi

cd "$DEST" || exit 1

echo "==> [5/7] 先只起数据库"
docker compose up -d --no-build testtask-db 2>&1 | tail -3
for i in $(seq 1 60); do
  docker inspect -f '{{.State.Health.Status}}' "$DB_CT" 2>/dev/null | grep -q healthy && break
  sleep 1
done
docker inspect -f '数据库状态: {{.State.Health.Status}}' "$DB_CT"

echo "==> [6/7] 导入数据库"
if [ -f "$SRC/db.sql" ]; then
  docker cp "$SRC/db.sql" "$DB_CT":/tmp/db.sql
  docker exec "$DB_CT" psql -U testtask -d testtask -f /tmp/db.sql > /tmp/dr_db.log 2>&1 \
    && echo "    db.sql 导入完成" \
    || { echo "    ⚠ 导入有告警/错误，日志尾部："; tail -5 /tmp/dr_db.log; }
  docker exec "$DB_CT" rm -f /tmp/db.sql
  docker exec "$DB_CT" psql -U testtask -d testtask -tAc \
    "select 'test_tasks 行数: '||count(*) from test_tasks" 2>/dev/null
else
  echo "    ✗ 备份包里没有 db.sql"
fi

echo "==> [7/7] 启动后端并健康检查"
docker compose up -d --no-build 2>&1 | tail -3
CODE=""
for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/docs")
  [ "$CODE" = "200" ] && break
  sleep 1
done

rm -rf "$TMP"
echo ""
echo "=============================================="
echo " 恢复完成"
echo "   访问地址: http://<本机IP>:$PORT"
echo "   健康检查: /docs = $CODE"
echo "   目录:     $DEST"
echo "=============================================="
echo " 接下来："
echo "   1) 浏览器打开上面地址，用原账号登录验证"
echo "   2) 抽查 1-2 个任务的附件能否下载"
echo "   3) 前端 API 用的是相对路径，换 IP 不需要改代码"
echo "   4) 通知大家新地址（或把域名解析指到新机）"
[ "$CODE" = "200" ] || echo "   ⚠ /docs 不是 200，检查: docker logs --tail=50 $BE_CT"
