#!/bin/bash
# 后端发布：备份容器代码 → 拷入新文件 → import 自检 → 重启 → 健康检查，任何一步失败自动回滚
#
# 用法（源文件必须先放到服务器的 /tmp/deploy/ 下，文件名与容器内一致）：
#   bash /opt/testtask/ops/deploy_backend.sh app/api/caselib.py
#   bash /opt/testtask/ops/deploy_backend.sh app/api/caselib.py app/models/models.py
#   bash /opt/testtask/ops/deploy_backend.sh app/api/caselib.py staging
#
set -u

SRCROOT=/tmp/deploy
BACKUPDIR=/opt/testtask/backups
TS=$(date +%Y%m%d%H%M%S)

ARGS=("$@")
ENV=prod
if [ ${#ARGS[@]} -gt 0 ]; then
  LAST=${ARGS[${#ARGS[@]}-1]}
  if [ "$LAST" = "prod" ] || [ "$LAST" = "staging" ]; then
    ENV=$LAST
    unset 'ARGS[${#ARGS[@]}-1]'
  fi
fi
[ ${#ARGS[@]} -eq 0 ] && { echo "用法: $0 <容器内相对路径 app/...> [其它文件...] [prod|staging]"; exit 1; }

if [ "$ENV" = "staging" ]; then
  CONTAINER=testtask-backend-staging
  PORT=8899
else
  CONTAINER=testtask-backend
  PORT=8888
fi

mkdir -p "$BACKUPDIR"
SNAP="$BACKUPDIR/app_${ENV}_$TS.tgz"

restore() {
  echo "!! 回滚中: $SNAP"
  rm -rf /tmp/restore_$TS && mkdir -p /tmp/restore_$TS
  tar xzf "$SNAP" -C /tmp/restore_$TS
  docker cp /tmp/restore_$TS/app/. "$CONTAINER":/app/app/
  docker restart "$CONTAINER" >/dev/null
  sleep 3
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/docs")
  echo "回滚后健康检查: $CODE"
  exit 1
}

# 1) 备份容器内 /app/app
docker exec "$CONTAINER" tar czf /tmp/app_$TS.tgz -C /app app || { echo "✗ 备份失败"; exit 1; }
docker cp "$CONTAINER":/tmp/app_$TS.tgz "$SNAP" || { echo "✗ 备份下载失败"; exit 1; }
docker exec "$CONTAINER" rm -f /tmp/app_$TS.tgz
echo "✓ 已备份容器代码: $SNAP"

# 2) 拷入新文件
for REL in "${ARGS[@]}"; do
  SRC="$SRCROOT/$(basename "$REL")"
  [ -f "$SRC" ] || { echo "✗ 源文件不存在: $SRC（先 SFTP 到 $SRCROOT）"; exit 1; }
  docker cp "$SRC" "$CONTAINER":/app/"$REL" || { echo "✗ 拷贝失败: $REL"; restore; }
  echo "✓ 已拷入: /app/$REL"
done

# 3) import 自检（此时容器还是旧进程，但文件已是新的，能提前抓出 ImportError / 语法错误）
if ! docker exec -w /app "$CONTAINER" python -c "import app.main" 2>&1 | tail -5; then
  echo "✗ import 自检失败，拒绝重启（生产仍在跑旧代码）"
  restore
fi
echo "✓ import 自检通过"

# 4) 重启（实测中断约 0.3 秒）
docker restart "$CONTAINER" >/dev/null
echo "✓ 已重启 $CONTAINER"

# 5) 健康检查，最多等 40 秒
OK=0
for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/docs")
  if [ "$CODE" = "200" ]; then OK=1; break; fi
  sleep 1
done
if [ "$OK" -ne 1 ]; then
  echo "✗ 重启后健康检查失败（40 秒内 /docs 未返回 200）"
  docker logs --tail=30 "$CONTAINER"
  restore
fi

echo "✓ $ENV 后端发布完成，/docs = 200"
if [ "$ENV" = "staging" ]; then echo "  预发地址: http://192.2.100.30:$PORT"; else echo "  生产地址: http://192.2.100.30:$PORT"; fi

# 6) 只保留最近 20 份代码快照
ls -t "$BACKUPDIR"/app_${ENV}_*.tgz 2>/dev/null | tail -n +21 | xargs -r rm -f
