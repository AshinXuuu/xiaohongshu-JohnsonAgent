"""在服务器上把 /tmp/manuals_only.db 合并到 data/usage.db。
保留服务器原本的 events 表(线上真实日志),只动 manuals + manuals_files。

用法:
    python3 scripts/import_manuals.py /tmp/manuals_only.db [--wipe]

--wipe: 清掉服务器现有 manuals + manuals_files 再导入(等于完全替换)
       不加 --wipe 则用 INSERT OR REPLACE 增量合并(以源 file_path 为准)
"""
import sys
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DST = ROOT / 'data' / 'usage.db'

if len(sys.argv) < 2:
    raise SystemExit('用法: python3 scripts/import_manuals.py <来源 db> [--wipe]')

SRC = Path(sys.argv[1])
WIPE = '--wipe' in sys.argv[2:]

if not SRC.exists():
    raise SystemExit(f'❌ 来源不存在: {SRC}')

# 先确保目标数据库的 schema 存在(走 kv_store 的初始化)
sys.path.insert(0, str(ROOT))
from lib import kv_store  # 模块导入即触发 _init_schema()
_ = kv_store._kv_available()

conn = sqlite3.connect(str(DST))
conn.execute(f"ATTACH DATABASE '{SRC}' AS src")

# 老 stats 对比
def stats(db):
    cur = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(char_count),0) FROM {db}.manuals").fetchone()
    fcnt = conn.execute(f"SELECT COUNT(*) FROM {db}.manuals_files").fetchone()[0]
    return cur[0], cur[1], fcnt

src_m, src_chars, src_f = stats('src')
dst_m_before, dst_chars_before, dst_f_before = stats('main')
print(f'来源: {src_m:,} manuals 行 / {src_chars:,} 字 / {src_f:,} files')
print(f'目标(导入前): {dst_m_before:,} manuals 行 / {dst_chars_before:,} 字 / {dst_f_before:,} files')

if WIPE:
    print('--wipe: 清空目标 manuals + manuals_files')
    conn.execute("DELETE FROM main.manuals")
    conn.execute("DELETE FROM main.manuals_files")

# 1) 合并 manuals_files(以 file_path 为主键,源覆盖目标)
conn.execute("INSERT OR REPLACE INTO main.manuals_files SELECT * FROM src.manuals_files")

# 2) 合并 manuals
# 策略:对源里每个 (brand, product, source_file) 组合,先在目标里删旧的,再插新的
# 这样多次运行不会重复
conn.execute("""
    DELETE FROM main.manuals
    WHERE (brand, product, source_file) IN (
        SELECT DISTINCT brand, product, source_file FROM src.manuals
    )
""")
conn.execute("""
    INSERT INTO main.manuals
        (brand, product, source_type, source_file, page_no, content, char_count, created_at)
    SELECT brand, product, source_type, source_file, page_no, content, char_count, created_at
    FROM src.manuals
""")
conn.commit()

dst_m_after, dst_chars_after, dst_f_after = stats('main')
print(f'目标(导入后): {dst_m_after:,} manuals 行 / {dst_chars_after:,} 字 / {dst_f_after:,} files')

# 按品牌/产品出一份导入结果
print()
print('每个产品的资料(导入后):')
for row in conn.execute("""
    SELECT brand, product, source_type, COUNT(*) c
    FROM main.manuals
    GROUP BY 1,2,3 ORDER BY 1,2,3
"""):
    print(f'  {row[0]} / {row[1]:<28s} {row[2]:<14s} {row[3]:>4d} 段')

conn.close()
print()
print('✓ 完成 — 重启 agent 让 qa.py 生效:')
print('  sudo systemctl restart agent')
