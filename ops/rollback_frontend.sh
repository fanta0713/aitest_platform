#!/bin/bash
# 前端回滚
# 用法：
#   bash /opt/testtask/ops/rollback_frontend.sh              # 回滚到最新一份备份
#   bash /opt/testtask/ops/rollback_frontend.sh 20260911140000 [prod|staging]
set -u

TS=${1:-}
ENV=${2:-prod}

if [ "$ENV" = "staging" ]; then
  DST=/opt/testtask-staging/frontend/index.html
else
  DST=/opt/testtask/frontend/index.html
fi

if [ -z "$TS" ]; then
  BAK=$(ls -t "$DST".bak_* 2>/dev/null | head -1)
  [ -z "$BAK" ] && { echo "✗ 没有找到任何备份"; exit 1; }
else
  BAK="$DST.bak_$TS"
  [ -f "$BAK" ] || { echo "✗ 备份不存在: $BAK"; exit 1; }
fi

# 回滚前先把当前（坏的）版本也留一份
cp -p "$DST" "$DST.broken_$(date +%Y%m%d%H%M%S)" 2>/dev/null
cp -p "$BAK" "$DST.new" && mv -f "$DST.new" "$DST"
echo "✓ 已回滚 $ENV 到: $BAK"
if [ "$ENV" = "staging" ]; then
  echo "  预发地址: http://192.2.100.30:8899"
else
  echo "  生产地址: http://192.2.100.30:8888（前端是 bind mount，刷新浏览器即可）"
fi
