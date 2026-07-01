"""usage.db 每日备份(一致性快照 + 自动清理旧备份)。

用 SQLite 在线备份 API,即使程序正在写库也能拿到一致快照;
不依赖服务器上的 sqlite3 命令(只用 python3)。

用法(服务器):
    cd /home/ubuntu/xiaohongshu-JohnsonAgent
    python3 scripts/backup_db.py

配 cron(每天凌晨 3 点):
    0 3 * * * cd /home/ubuntu/xiaohongshu-JohnsonAgent && python3 scripts/backup_db.py >> backups/backup.log 2>&1

环境变量:
    USAGE_DB_PATH     数据库路径(默认 data/usage.db)
    BACKUP_KEEP_DAYS  保留天数(默认 14)
"""
import glob
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get('USAGE_DB_PATH', str(ROOT / 'data' / 'usage.db')))
BK = ROOT / 'backups'
KEEP_DAYS = int(os.environ.get('BACKUP_KEEP_DAYS', '14'))


def main():
    BK.mkdir(parents=True, exist_ok=True)
    if not DB.exists():
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 跳过:{DB} 不存在")
        return
    ts = time.strftime('%Y%m%d-%H%M%S')
    dest = BK / f"usage_{ts}.db"
    src = sqlite3.connect(str(DB))
    dst = sqlite3.connect(str(dest))
    try:
        with dst:
            src.backup(dst)          # 一致性快照
    finally:
        src.close()
        dst.close()
    size_mb = dest.stat().st_size / 1024 / 1024

    # 清理超期备份
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for f in glob.glob(str(BK / "usage_*.db")):
        if os.path.getmtime(f) < cutoff:
            os.remove(f)
            removed += 1

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 备份完成 → {dest.name} "
          f"({size_mb:.1f} MB),清理旧备份 {removed} 个,保留 {KEEP_DAYS} 天")


if __name__ == "__main__":
    main()
