#!/bin/bash
# 把预发(192.2.56.76)验证过的改动同步到生产(192.2.100.30)
# 在生产机 192.2.100.30 上运行。只同步「代码」，绝不动生产数据库和附件。
#
# 用法：
#   bash /opt/testtask/ops/sync_staging_to_prod.sh --dry-run   # 只看差异，不发布
#   bash /opt/testtask/ops/sync_staging_to_prod.sh             # 发布差异文件（走自检 + 自动回滚）
set -u

STG=root@192.2.56.76
STGDIR=/opt/testtask
PRODDIR=/opt/testtask
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

# 1) 取预发侧所有 .py 的 md5
ssh -o StrictHostKeyChecking=no "$STG" "cd $STGDIR/backend && find app -name '*.py' | sort | xargs md5sum" > /tmp/stg_md5_$$.txt 2>/dev/null || { echo "✗ 无法读取预发文件列表"; exit 1; }

# 2) 逐个比对，收集差异
DIFFS=()
while read -r h f; do
  [ -z "${f:-}" ] && continue
  ph=$(md5sum "$PRODDIR/backend/$f" 2>/dev/null | awk '{print $1}')
  if [ "$ph" != "$h" ]; then
    DIFFS+=("$f")
    if [ $DRY -eq 1 ]; then
      if [ -z "$ph" ]; then echo "  [新增] $f"; else echo "  [不同] $f"; fi
    fi
  fi
done < /tmp/stg_md5_$$.txt

# 3) 前端
FE_SAME=1
sh=$(ssh -o StrictHostKeyChecking=no "$STG" "md5sum $STGDIR/frontend/index.html" | awk '{print $1}')
ph=$(md5sum "$PRODDIR/frontend/index.html" | awk '{print $1}')
[ "$sh" != "$ph" ] && FE_SAME=0

echo "差异统计：后端 ${#DIFFS[@]} 个文件，前端 $([ $FE_SAME -eq 1 ] && echo 无变化 || echo 有变化)"
if [ $DRY -eq 1 ]; then rm -f /tmp/stg_md5_$$.txt; exit 0; fi

if [ ${#DIFFS[@]} -eq 0 ] && [ $FE_SAME -eq 1 ]; then
  echo "✓ 预发与生产代码一致，无需同步"
  rm -f /tmp/stg_md5_$$.txt
  exit 0
fi

# 4) 发布后端差异文件（一次调用 deploy_backend.sh，内部有备份 + import 自检 + 自动回滚）
if [ ${#DIFFS[@]} -gt 0 ]; then
  mkdir -p /tmp/deploy
  for f in "${DIFFS[@]}"; do
    scp -o StrictHostKeyChecking=no "$STG:$STGDIR/backend/$f" "/tmp/deploy/$(basename "$f")" || { echo "✗ 拉取失败: $f"; exit 1; }
  done
  echo "→ 发布后端：${DIFFS[*]}"
  bash /opt/testtask/ops/deploy_backend.sh "${DIFFS[@]}" || exit 1
fi

# 5) 发布前端
if [ $FE_SAME -eq 0 ]; then
  scp -o StrictHostKeyChecking=no "$STG:$STGDIR/frontend/index.html" /tmp/deploy/index.html || exit 1
  echo "→ 发布前端"
  bash /opt/testtask/ops/deploy_frontend.sh /tmp/deploy/index.html || exit 1
fi

rm -f /tmp/stg_md5_$$.txt
echo "✓ 同步完成。生产地址: http://192.2.100.30:8888"
echo "  提醒：若本次改动涉及数据库结构（加表/加字段），生产重启时 create_all 会自动建；"
echo "        删字段/改类型/清数据必须另外手工执行 SQL，本脚本不会碰数据库。"
