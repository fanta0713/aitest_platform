#!/bin/bash
# 把「生产当前代码」同步到预发机 192.2.56.76（可选同时用生产最新数据覆盖预发库）
# 在生产机 192.2.100.30 上运行（已配置 prod -> staging 的 root SSH 免密）
#
# 用法：
#   bash /opt/testtask/ops/refresh_staging.sh              # 只同步代码
#   bash /opt/testtask/ops/refresh_staging.sh --with-data   # 代码 + 数据（会清空预发库重灌）
set -u

STG=root@192.2.56.76
STGDIR=/opt/testtask
TS=$(date +%Y%m%d%H%M%S)
WITH_DATA=0
[ "${1:-}" = "--with-data" ] && WITH_DATA=1

echo "→ [1/4] 打包生产源码"
cd /opt/testtask || exit 1
tar czf /tmp/src_$TS.tgz backend/app backend/Dockerfile backend/requirements.txt backend/.dockerignore frontend docker-compose.yml || exit 1

echo "→ [2/4] 传到预发并解包"
scp -o StrictHostKeyChecking=no /tmp/src_$TS.tgz "$STG":/tmp/ || exit 1
ssh -o StrictHostKeyChecking=no "$STG" "cd $STGDIR && tar xzf /tmp/src_$TS.tgz && rm -f /tmp/src_$TS.tgz" || exit 1
rm -f /tmp/src_$TS.tgz

echo "→ [3/4] 预发容器加载新代码并重启"
ssh -o StrictHostKeyChecking=no "$STG" \
  "docker cp $STGDIR/backend/app/. testtask-backend:/app/app/ && docker restart testtask-backend >/dev/null" || exit 1

if [ $WITH_DATA -eq 1 ]; then
  echo "→ [4/4] 刷新预发数据库（用生产最新数据覆盖）"
  docker exec testtask-db pg_dump -U testtask -d testtask -Fc > /tmp/stgdb_$TS.dump || exit 1
  scp -o StrictHostKeyChecking=no /tmp/stgdb_$TS.dump "$STG":/tmp/ || exit 1
  ssh -o StrictHostKeyChecking=no "$STG" \
    "docker cp /tmp/stgdb_$TS.dump testtask-db:/tmp/ && \
     docker exec testtask-db pg_restore -U testtask -d testtask --clean --if-exists --no-owner /tmp/stgdb_$TS.dump; \
     docker exec testtask-db rm -f /tmp/stgdb_$TS.dump; \
     docker restart testtask-backend >/dev/null" || exit 1
  rm -f /tmp/stgdb_$TS.dump
else
  echo "→ [4/4] 跳过数据刷新（如需最新数据加 --with-data）"
fi

CODE=""
for i in $(seq 1 30); do
  CODE=$(ssh -o StrictHostKeyChecking=no "$STG" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8888/docs")
  [ "$CODE" = "200" ] && break
  sleep 1
done
echo "✓ 预发健康检查 /docs = $CODE"
echo "  预发地址: http://192.2.56.76:8888"
