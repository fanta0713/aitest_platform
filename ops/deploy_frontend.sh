#!/bin/bash
# 前端发布（自动备份 + 基本校验 + 原子替换）
# 用法：
#   bash /opt/testtask/ops/deploy_frontend.sh /tmp/deploy/index.html [prod|staging]
# 默认 prod。staging 发布到 /opt/testtask-staging/frontend/index.html（8899 端口）
set -u

SRC=${1:-/tmp/deploy/index.html}
ENV=${2:-prod}
TS=$(date +%Y%m%d%H%M%S)

if [ "$ENV" = "staging" ]; then
  DST=/opt/testtask-staging/frontend/index.html
else
  DST=/opt/testtask/frontend/index.html
fi

if [ ! -f "$SRC" ]; then echo "✗ 源文件不存在: $SRC"; exit 1; fi
if [ ! -f "$DST" ]; then echo "✗ 目标文件不存在: $DST（部署目录不对？）"; exit 1; fi

# 1) 基本校验：大小 + 首尾标签 + 必须有 script 块
SIZE=$(stat -c %s "$SRC")
if [ "$SIZE" -lt 50000 ]; then echo "✗ 源文件只有 ${SIZE} 字节，疑似截断，拒绝发布"; exit 1; fi
grep -q "</html>" "$SRC" || { echo "✗ 源文件缺少 </html>，拒绝发布"; exit 1; }
grep -q "<script>" "$SRC" || { echo "✗ 源文件缺少 <script>，拒绝发布"; exit 1; }

# 2) 备份现有版本
cp -p "$DST" "$DST.bak_$TS" || { echo "✗ 备份失败"; exit 1; }
echo "✓ 已备份: $DST.bak_$TS"

# 3) 原子替换（同目录 mv 是原子操作，避免用户刷到半截文件）
cp "$SRC" "$DST.new" || { echo "✗ 复制失败"; exit 1; }
mv -f "$DST.new" "$DST" || { echo "✗ 替换失败"; exit 1; }

echo "✓ 已发布到 $ENV: $DST (${SIZE} 字节)"
if [ "$ENV" = "staging" ]; then
  echo "  预发地址: http://192.2.100.30:8899"
else
  echo "  生产地址: http://192.2.100.30:8888"
fi
echo "  回滚: bash /opt/testtask/ops/rollback_frontend.sh $TS $ENV"

# 4) 只保留最近 20 份备份
ls -t "$DST".bak_* 2>/dev/null | tail -n +21 | xargs -r rm -f
