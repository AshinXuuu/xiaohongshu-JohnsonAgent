"""一次性迁移脚本:把 Upstash(Vercel KV)里的历史事件搬到本地 SQLite。

用法:
  1. 确保 .env 里仍有 KV_REST_API_URL / KV_REST_API_TOKEN(Upstash 的)
  2. 在项目根跑:python3 scripts/migrate_upstash_to_sqlite.py
  3. 跑完后脚本会输出本次迁了多少条事件
  4. 旧 Upstash 的数据不会被删除 — 你确认 SQLite 数据完整后,可以自行去 Vercel/Upstash 后台清空

注意:Upstash 的 usage:logs LIST 是 LTRIM 到最近 1000 条的,更早的事件已经丢了,
迁过来的就是这 1000 条以内的最新事件。计数器 usage:total / usage:user:xxx 等
不会被迁移 — 因为 SQLite 改用 GROUP BY 实时聚合,迁完后旧计数器自动失效。
"""
import os
import sys
import json
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_env():
    """简单的 .env 加载"""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def kv_req(method, path, body=None, timeout=10):
    base = os.environ.get('KV_REST_API_URL', '').rstrip('/')
    token = os.environ.get('KV_REST_API_TOKEN', '')
    if not base or not token:
        raise RuntimeError("KV_REST_API_URL / KV_REST_API_TOKEN 未配置 — 无法读 Upstash")
    url = f'{base}{path}'
    headers = {'Authorization': f'Bearer {token}'}
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_upstash_logs():
    """从 Upstash 拉取 usage:logs 列表(最多 1000 条)"""
    print("→ 拉取 Upstash 历史事件...")
    res = kv_req('GET', '/lrange/usage:logs/0/999')
    raw = res.get('result') or []
    print(f"  Upstash 返回 {len(raw)} 条原始字符串")

    events = []
    parse_fail = 0
    for x in raw:
        try:
            events.append(json.loads(x))
        except Exception:
            parse_fail += 1
    if parse_fail:
        print(f"  ⚠ {parse_fail} 条 JSON 解析失败,已跳过")
    print(f"  成功解析 {len(events)} 条事件")
    return events


def fetch_upstash_counters():
    """顺便看看 Upstash 上有哪些计数器,只打印不迁移"""
    try:
        res = kv_req('GET', '/keys/' + urllib.parse.quote('usage:*'))
        keys = res.get('result') or []
        return keys
    except Exception:
        return []


def write_to_sqlite(events):
    """把事件批量写到本地 SQLite events 表"""
    if not events:
        print("→ 没有事件可迁移,跳过 SQLite 写入")
        return 0

    # 此时再 import,避免 schema 在 .env 加载前初始化导致路径偏差
    from lib.kv_store import _get_conn, _init_schema, DB_PATH
    _init_schema()
    print(f"→ 写入本地 SQLite: {DB_PATH}")

    inserted = 0
    skipped_duplicate = 0
    conn = _get_conn()
    try:
        # 先看一下表里现在有多少行
        existing = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"  迁移前表里已有 {existing} 行")

        # 拿当前已存在的 time_ms 做去重(避免重复运行迁移脚本)
        existing_times = {r[0] for r in conn.execute(
            "SELECT time_ms FROM events"
        ).fetchall()}

        for e in events:
            time_ms = e.get('time') or 0
            if time_ms in existing_times:
                skipped_duplicate += 1
                continue
            action = e.get('action', '')
            user = e.get('user') or {}
            details = e.get('details') or {}
            emp_id = (user.get('emp_id') or 'unknown')
            name = user.get('name') or ''
            dept = (user.get('department') or 'unknown')
            details_json = json.dumps(details, ensure_ascii=False)
            # 用 time_ms 反推 day
            import datetime
            day = datetime.datetime.fromtimestamp(time_ms / 1000).strftime('%Y-%m-%d') if time_ms else ''

            conn.execute(
                "INSERT INTO events (time_ms, action, emp_id, user_name, department, "
                "details_json, day) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time_ms, action, emp_id, name, dept, details_json, day)
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(f"  ✓ 新写入 {inserted} 条")
    if skipped_duplicate:
        print(f"  ⏭️  按 time_ms 去重,跳过 {skipped_duplicate} 条已存在")
    return inserted


def main():
    load_env()
    if not (os.environ.get('KV_REST_API_URL') and os.environ.get('KV_REST_API_TOKEN')):
        print("✗ .env 里缺 KV_REST_API_URL / KV_REST_API_TOKEN — 无法读 Upstash")
        print("  请把这两个变量从 Vercel/Upstash 后台拷过来填到 .env")
        return 1

    print("=" * 60)
    print("Upstash → SQLite 用量数据迁移")
    print("=" * 60)

    events = fetch_upstash_logs()
    keys = fetch_upstash_counters()
    if keys:
        print(f"\nUpstash 上还有 {len(keys)} 个 usage:* 计数器(仅参考,不迁移):")
        for k in sorted(keys)[:20]:
            print(f"  · {k}")
        if len(keys) > 20:
            print(f"  ... 还有 {len(keys) - 20} 个")

    inserted = write_to_sqlite(events)

    print()
    print("=" * 60)
    print(f"完成 ✓ 共迁入 {inserted} 条事件到 SQLite")
    print("=" * 60)
    print()
    print("下一步:")
    print("  1. systemctl restart agent  # 重启服务,后续新事件直接写 SQLite")
    print("  2. 打开管理员后台确认数据正常显示")
    print("  3. 数据确认无误后,可去 Upstash 后台清空 usage:* 键,或干脆删项目")
    print("  4. .env 里的 KV_REST_API_URL / KV_REST_API_TOKEN 也可以删除了(SQLite 不需要)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
