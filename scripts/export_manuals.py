"""把 Mac 本机 data/usage.db 里的 manuals + manuals_files 两张表
导出成一个独立的小 SQLite 文件,便于 scp 到服务器,
不连带传输 events 表(它有线上真实日志,不能覆盖)。

用法:
    python3 scripts/export_manuals.py
输出:
    out/manuals_only.db  ← 上传到服务器用
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'data' / 'usage.db'
OUT = ROOT / 'out' / 'manuals_only.db'

if not SRC.exists():
    raise SystemExit(f'❌ 源数据库不存在: {SRC}')

OUT.parent.mkdir(parents=True, exist_ok=True)
if OUT.exists():
    OUT.unlink()

conn = sqlite3.connect(str(SRC))
conn.execute(f"ATTACH DATABASE '{OUT}' AS dst")
conn.executescript("""
    CREATE TABLE dst.manuals AS SELECT * FROM main.manuals;
    CREATE TABLE dst.manuals_files AS SELECT * FROM main.manuals_files;
""")
conn.commit()

# 统计
m_cnt = conn.execute("SELECT COUNT(*) FROM dst.manuals").fetchone()[0]
mf_cnt = conn.execute("SELECT COUNT(*) FROM dst.manuals_files").fetchone()[0]
total_chars = conn.execute("SELECT COALESCE(SUM(char_count), 0) FROM dst.manuals").fetchone()[0]
conn.close()

size_mb = OUT.stat().st_size / 1024 / 1024
print(f'✓ 已导出到: {OUT}')
print(f'  manuals      : {m_cnt:>6,} 行 / {total_chars:>9,} 字')
print(f'  manuals_files: {mf_cnt:>6,} 行')
print(f'  文件大小     : {size_mb:.1f} MB')
print()
print(f'下一步:scp 到服务器')
print(f'  scp {OUT} ubuntu@124.222.164.101:/tmp/manuals_only.db')
