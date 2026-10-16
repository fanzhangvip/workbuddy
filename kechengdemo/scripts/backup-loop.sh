#!/bin/sh
# 定时备份 + 校验 + 按保留期清理（跑在 backup 容器里，见 docker-compose.yml 的 ops profile）
#
# 设计要点
#   1. **备份必须校验**：没校验过的备份等于没有备份 —— 一个写了一半的 dump
#      在恢复时才会暴露，而那时已经晚了。这里每次都用 `pg_restore --list` 验证可读性。
#   2. **失败不退出循环**：一次备份失败（如磁盘满）不该让整个备份机制静默死掉，
#      而是打日志、下一轮继续（`docker logs weiban-scoring-backup` 能看到告警）。
#   3. **先备份再清理**：清理只删"已经成功备份过"的旧文件，绝不在备份失败时把历史删掉。
#   4. **保留策略可按需调**：BACKUP_RETENTION_DAYS，默认 14 天。
set -eu

INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
DEST="/backups"

echo "[backup] 启动：每 ${INTERVAL_HOURS} 小时一次，保留 ${RETENTION_DAYS} 天"
echo "[backup] 目标库：${PGUSER}@${PGHOST}/${PGDATABASE} → ${DEST}"

mkdir -p "$DEST"

do_backup() {
    ts="$(date +%Y%m%d-%H%M%S)"
    out="$DEST/${PGDATABASE}-${ts}.dump"
    echo "[backup] $(date '+%F %T') 开始 → $(basename "$out")"

    # -Fc（custom format）：支持并行恢复与选择性恢复，且自带校验和
    if ! pg_dump -Fc -d "$PGDATABASE" -f "$out"; then
        echo "[backup] ❌ pg_dump 失败，保留上一份备份，不执行清理"
        rm -f "$out"
        return 1
    fi

    # ★ 校验：能列出归档目录才算可用；否则当成失败并删掉半截文件
    if ! pg_restore --list "$out" >/dev/null 2>&1; then
        echo "[backup] ❌ 备份文件校验失败（可能写了一半），已删除：$(basename "$out")"
        rm -f "$out"
        return 1
    fi

    size="$(du -h "$out" | cut -f1)"
    echo "[backup] ✅ 完成：$(basename "$out")（$size，已校验）"

    # 清理：只删超过保留期的 .dump（先备份成功才走到这里）
    find "$DEST" -maxdepth 1 -name "${PGDATABASE}-*.dump" -type f \
         -mtime "+${RETENTION_DAYS}" -print 2>/dev/null | while read -r old; do
        echo "[backup] 清理过期备份：$(basename "$old")"
        rm -f "$old"
    done
    echo "[backup] 当前保留 $(find "$DEST" -maxdepth 1 -name "${PGDATABASE}-*.dump" | wc -l) 份"
    return 0
}

# 先立刻跑一次（新部署不必等一个周期才有第一份备份），然后进入周期循环
do_backup || true

while true; do
    sleep "$((INTERVAL_HOURS * 3600))"
    do_backup || true
done
