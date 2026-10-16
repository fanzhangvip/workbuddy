#!/bin/sh
# 从备份恢复（**危险操作**，故做成显式调用而非默认路径）
#
# 用法（在宿主机，compose 目录下）：
#   # 1) 先看有哪些备份
#   ls -lh backups/*.dump
#   # 2) 恢复（会先自动备份当前库到 pre-restore 文件，再覆盖）
#   docker compose exec -T postgres sh -c \
#     'PGPASSWORD=$POSTGRES_PASSWORD pg_restore -c -d weiban_chat' < backups/weiban_chat-YYYYmmdd-HHMMSS.dump
#
# 这个脚本把上面那步固化成一条命令，并在恢复前强制先做一次「恢复前快照」。
set -eu

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
    echo "用法: $0 <备份文件.dump>"
    echo "可用备份："
    ls -lh /backups/*.dump 2>/dev/null || echo "  （无）"
    exit 2
fi
if [ ! -f "$DUMP" ]; then
    echo "❌ 找不到备份文件：$DUMP"
    exit 2
fi

# ① 校验要恢复的文件（不能拿一个坏文件去覆盖生产库）
if ! pg_restore --list "$DUMP" >/dev/null 2>&1; then
    echo "❌ 备份文件损坏或格式不对，拒绝恢复：$DUMP"
    exit 3
fi

# ② 恢复前快照（万一同一次恢复也有问题，还有退路）
SAFE="/backups/pre-restore-$(date +%Y%m%d-%H%M%S).dump"
echo "[restore] 先做恢复前快照：$(basename "$SAFE")"
pg_dump -Fc -d "$PGDATABASE" -f "$SAFE"
pg_restore --list "$SAFE" >/dev/null || { echo "❌ 恢复前快照不可用，中止"; exit 4; }

# ③ 恢复：-c 先清理对象（否则可能与现有表冲突）；--if-exists 避免"不存在"报错
echo "[restore] 开始恢复：$DUMP"
pg_restore --clean --if-exists --no-owner -d "$PGDATABASE" "$DUMP"

echo "[restore] ✅ 恢复完成。若应用在跑，建议重启以重建连接池："
echo "          docker compose restart app"
