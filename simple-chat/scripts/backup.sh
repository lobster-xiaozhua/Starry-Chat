#!/usr/bin/env bash
# SQLite 在线热备脚本（改动 7）。
#
# 使用 SQLite Online Backup API 做一致性快照：不锁表、不阻塞写入（WAL 下读写并行），
# 适合对运行中的 Simple Chat 库直接备份（比 cp 安全，不会拷到撕裂页）。
#
# 实现优先级：
#   1) sqlite3 CLI 的 .backup 命令；
#   2) 无 CLI 时回退到 python3 的 sqlite3 模块 conn.backup()（同一 Backup API）。
#
# 行为：
#   1. 生成时间戳命名的快照。
#   2. 事后校验：PRAGMA integrity_check 必须为 ok，否则删除坏档并以非零退出。
#   3. 保留最近 30 天，删除更旧的备份文件。
#   4. 输出：备份路径 + 大小 + 耗时。
#
# cron（每日 03:00）：
#   0 3 * * * /path/to/scripts/backup.sh >> /var/log/simple-chat-backup.log 2>&1
set -euo pipefail

# ───────────────────────── 可配置项 ─────────────────────────
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 与 app/config.py 的 database_url 对齐（默认 ./data/chat.db，可被环境变量覆盖）
DB_PATH="${DB_PATH:-$REPO_DIR/data/chat.db}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
RETAIN_DAYS=30
STAMP="$(date +%Y%m%d_%H%M%S)"
DST="$BACKUP_DIR/chat_$STAMP.db"

# ───────────────────────── 前置检查 ─────────────────────────
if [ ! -f "$DB_PATH" ]; then
  echo "ERROR: database not found: $DB_PATH" >&2
  exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: need sqlite3 CLI or python3 (for sqlite3 backup API)" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

START="$(date +%s)"

# ───────────────────────── 在线热备 ─────────────────────────
backup_via_cli() {
  sqlite3 "$DB_PATH" ".backup '$DST'"
}

backup_via_python() {
  python3 - "$DB_PATH" "$DST" <<'PYEOF'
import sys
from sqlite3 import connect

src, dst = sys.argv[1], sys.argv[2]
with connect(src) as conn:
    with connect(dst) as out:
        conn.backup(out)
PYEOF
}

if command -v sqlite3 >/dev/null 2>&1; then
  if ! backup_via_cli; then
    echo "ERROR: sqlite3 .backup failed" >&2
    rm -f "$DST"
    exit 1
  fi
else
  if ! backup_via_python; then
    echo "ERROR: python sqlite3 backup failed" >&2
    rm -f "$DST"
    exit 1
  fi
fi

# ───────────────────────── 事后校验 ─────────────────────────
# 必须通过完整性检查，否则视为坏档，删除并失败退出（不留假备份）
integrity_check() {
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$1" "PRAGMA integrity_check;"
  else
    python3 - "$1" <<'PYEOF'
import sys
from sqlite3 import connect

with connect(sys.argv[1]) as conn:
    row = conn.execute("PRAGMA integrity_check;").fetchone()
print(row[0] if row else "error")
PYEOF
  fi
}

if ! integrity_check "$DST" | grep -q "ok"; then
  echo "ERROR: integrity_check failed for $DST" >&2
  rm -f "$DST"
  exit 1
fi

END="$(date +%s)"
ELAPSED=$((END - START))
SIZE="$(du -h "$DST" | cut -f1)"

# ───────────────────────── 清理旧备份 ─────────────────────────
# 保留最近 30 天；-mtime +30 即修改时间超过 30 天的档
DELETED=$(find "$BACKUP_DIR" -name "chat_*.db" -type f -mtime +$RETAIN_DAYS -print -delete | wc -l)

echo "backup ok: $DST"
echo "size: $SIZE"
echo "elapsed: ${ELAPSED}s"
echo "pruned: $DELETED old backup(s) (retain ${RETAIN_DAYS}d)"
