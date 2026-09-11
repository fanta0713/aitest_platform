#!/bin/bash
# ============================================================
#  测试任务管理平台 - 异地恢复脚本（新机器执行）
#  前置：新机器已安装 Docker + Docker Compose，且已拿到备份包
#  用法：
#    tar -xzf testtask_backup_YYYYmmdd_HHMMSS.tar.gz -C /tmp
#    bash restore.sh /tmp/20260907_120000            # 解压后的备份目录
#    bash restore.sh /tmp/20260907_120000 /opt/testtask  # 指定安装目录
# ============================================================
set -e

BAK=$1
TARGET=${2:-/opt/testtask}

if [ -z "$BAK" ] || [ ! -d "$BAK" ]; then
  echo "用法: bash restore.sh <备份目录> [安装目录]"
  exit 1
fi

echo "==> [1/7] 准备安装目录 $TARGET"
mkdir -p "$TARGET"

echo "==> [2/7] 还原后端源码 + 前端 + 配置"
cp -a "$BAK/backend"  "$TARGET/backend"
cp -a "$BAK/frontend" "$TARGET/frontend"
cp -a "$BAK/docker-compose.yml" "$TARGET/"
[ -f "$BAK/.env" ] && cp -a "$BAK/.env" "$TARGET/" || echo "    (无 .env，将使用 compose 默认值)"
[ -f "$BAK/start.sh" ] && cp -a "$BAK/start.sh" "$TARGET/"

cd "$TARGET"

echo "==> [3/7] 构建并启动容器（此时会建空库表）"
docker compose up -d --build

echo "==> [4/7] 等待数据库就绪"
for i in $(seq 1 60); do
  if docker exec testtask-db pg_isready -U testtask -d testtask >/dev/null 2>&1; then
    echo "    数据库已就绪"; break
  fi
  sleep 2
done

echo "==> [5/7] 清空自动建的空表并导入真实数据"
docker exec -i testtask-db psql -U testtask -d testtask \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
docker exec -i testtask-db psql -U testtask -d testtask < "$BAK/db.sql"

echo "==> [6/7] 还原附件到容器 /app/uploads"
docker exec testtask-backend mkdir -p /app/uploads
docker cp "$BAK/uploads/." testtask-backend:/app/uploads

echo "==> [7/7] 重启后端加载数据"
docker restart testtask-backend
sleep 5

echo ""
echo "=========================================="
echo "  恢复完成，请验证："
echo "  1) 容器状态: docker compose ps"
echo "  2) 健康检查: curl -s -o /dev/null -w '%{http_code}' http://<新机器IP>:8888/docs"
echo "  3) 数据核对:"
docker exec testtask-db psql -U testtask -d testtask -tAc \
  "select 'test_tasks='||count(*) from test_tasks"
docker exec testtask-db psql -U testtask -d testtask -tAc \
  "select 'test_cases='||count(*) from test_cases"
docker exec testtask-db psql -U testtask -d testtask -tAc \
  "select 'step_files='||count(*) from step_files"
echo "  4) 浏览器访问 http://<新机器IP>:8888 用原账号登录"
echo ""
echo "  提示：前端 API 使用相对路径（const API=''），换 IP 无需改代码，"
echo "        只需保证 8888 端口映射一致，或同步修改 docker-compose.yml 端口。"
echo "=========================================="
