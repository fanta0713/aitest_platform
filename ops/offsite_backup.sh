#!/bin/bash
# ============================================================
#  离线（异机）备份 —— 灾难恢复的前提
#  备份只放在生产本机 = 单点故障，机器一挂全没了。
#  本脚本：调用 backup.sh 打包 → 传到备份机 → 远端只保留最近 N 份
#
#  在生产机 192.2.100.30 上执行（需已配置到备份机的 SSH 免密）：
#    bash /opt/testtask/ops/offsite_backup.sh                  # 常规备份
#    bash /opt/testtask/ops/offsite_backup.sh --with-image      # 连 docker 镜像一起备（新机拉不到镜像时必选）
#
#  可用环境变量覆盖：
#    BAK_HOST=root@192.2.56.76   BAK_DIR=/opt/testtask-backups   KEEP=10
# ============================================================
set -u

BAK_HOST=${BAK_HOST:-root@192.2.56.76}
BAK_DIR=${BAK_DIR:-/opt/testtask-backups}
KEEP=${KEEP:-10}
WITH_IMAGE=0
[ "${1:-}" = "--with-image" ] && WITH_IMAGE=1

TS=$(date +%Y%m%d_%H%M%S)
cd /opt/testtask || exit 1

echo "==> [1/4] 生成本地备份包"
bash /opt/testtask/ops/backup.sh "/opt/testtask/backups/$TS" || exit 1
BUNDLE="/opt/testtask/backups/$TS.tar.gz"
[ -f "$BUNDLE" ] || { echo "✗ 备份包没生成: $BUNDLE"; exit 1; }
echo "    $(du -h "$BUNDLE" | cut -f1)"

IMG=""
if [ $WITH_IMAGE -eq 1 ]; then
  echo "==> [2/4] 导出 docker 镜像（约 270MB，压缩后）"
  IMG="/opt/testtask/backups/images_$TS.tgz"
  docker save testtask-testtask-backend:latest postgres:15-alpine | gzip -1 > "$IMG" || exit 1
  echo "    $(du -h "$IMG" | cut -f1)"
else
  echo "==> [2/4] 跳过镜像（新机能连 Docker Hub 时可跳过；否则请加 --with-image）"
fi

echo "==> [3/4] 传到备份机 $BAK_HOST:$BAK_DIR"
ssh -o StrictHostKeyChecking=no "$BAK_HOST" "mkdir -p $BAK_DIR" || exit 1
scp -o StrictHostKeyChecking=no "$BUNDLE" "$BAK_HOST:$BAK_DIR/" || exit 1
[ -n "$IMG" ] && scp -o StrictHostKeyChecking=no "$IMG" "$BAK_HOST:$BAK_DIR/"

echo "==> [4/4] 远端只保留最近 $KEEP 份"
ssh -o StrictHostKeyChecking=no "$BAK_HOST" \
  "ls -1t $BAK_DIR/*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f; \
   ls -1t $BAK_DIR/images_*.tgz 2>/dev/null | tail -n +3 | xargs -r rm -f; \
   ls -lh $BAK_DIR | tail -8"
[ -n "$IMG" ] && rm -f "$IMG"

echo ""
echo "✓ 离线备份完成：$BAK_HOST:$BAK_DIR/$(basename "$BUNDLE")"
echo "  恢复方式见 /opt/testtask/ops/disaster_recovery.md 或："
echo "    bash /opt/testtask/ops/restore_to_new_host.sh <bundle.tar.gz>"
