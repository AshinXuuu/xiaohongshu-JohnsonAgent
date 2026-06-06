"""把数据库里所有「未知 / unknown / 空」用户的 QA 事件归到徐昕/市场部/888888。

服务器上跑一次即可:
    python3 scripts/migrate_qa_unknown_users.py

幂等,可重复跑(已经迁过的记录不会再动)。
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'data' / 'usage.db'

if not DB_PATH.exists():
    raise SystemExit(f'❌ DB 不存在: {DB_PATH}')

conn = sqlite3.connect(str(DB_PATH))

WHERE = """
  WHERE action IN ('qa','qa_failed')
    AND (
      emp_id IS NULL OR emp_id = '' OR emp_id = 'unknown' OR emp_id = '未知'
      OR user_name = '未知' OR user_name IS NULL OR user_name = ''
    )
"""

# 先列出来看一下范围
cur = conn.execute(f"""
  SELECT emp_id, user_name, department, COUNT(*) c
  FROM events {WHERE}
  GROUP BY 1,2,3
""").fetchall()

if not cur:
    print('✓ 没有需要迁移的 QA 未知用户事件。')
    raise SystemExit(0)

print('待迁移分布:')
total = 0
for row in cur:
    print(f"  emp_id={row[0]!r}  name={row[1]!r}  dept={row[2]!r}  → {row[3]} 条")
    total += row[3]
print(f'合计: {total} 条')

ans = input('归并到 徐昕/市场部/888888,确认?[y/N] ').strip().lower()
if ans != 'y':
    print('已取消。')
    raise SystemExit(0)

n = conn.execute(f"""
  UPDATE events SET emp_id='888888', user_name='徐昕', department='市场部'
  {WHERE}
""").rowcount
conn.commit()
conn.close()
print(f'✓ 已合并 {n} 条')
