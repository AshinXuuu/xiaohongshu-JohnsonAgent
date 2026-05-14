"""Vercel KV (Upstash Redis) 用量日志存储。

Vercel 接入 Upstash Redis 后,会自动注入 2 个环境变量:
  - KV_REST_API_URL
  - KV_REST_API_TOKEN

代码无需任何配置,导入即用。如果 KV 未配置,所有写入都会**静默 no-op**,
不影响主流程的请求处理。
"""
import os
import json
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error


def _kv_available() -> bool:
    return bool(
        os.environ.get('KV_REST_API_URL')
        and os.environ.get('KV_REST_API_TOKEN')
    )


def _kv_req(method: str, path: str, body=None, timeout: int = 5):
    """通用 KV REST 调用。失败返回 None,不抛异常(避免影响主流程)。"""
    base = os.environ.get('KV_REST_API_URL', '').rstrip('/')
    token = os.environ.get('KV_REST_API_TOKEN', '')
    if not base or not token:
        return None
    url = f'{base}{path}'
    headers = {'Authorization': f'Bearer {token}'}
    data = None
    if body is not None:
        if isinstance(body, str):
            data = body.encode('utf-8')
        else:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def log_event(action: str, user: dict, details: dict = None):
    """记录一次用量事件。

    action:  'login' / 'generate' / 'cover_fields' / 'cover_generate'
    user:    {'emp_id', 'name', 'department'}
    details: 自由附加信息,如 {'brand': 'XX', 'style': 'XX', 'title': 'XX'}

    一次性写入(pipeline):
      1. event 详情塞到 list usage:logs
      2. 各项 counter 同步 +1
    """
    if not _kv_available():
        return

    event = {
        'time': int(time.time() * 1000),
        'action': action,
        'user': user or {},
        'details': details or {},
    }

    emp_id = (user or {}).get('emp_id') or 'unknown'
    dept = (user or {}).get('department') or 'unknown'
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    # 用 pipeline 一次发,降低延迟
    commands = [
        ['LPUSH', 'usage:logs', json.dumps(event, ensure_ascii=False)],
        ['LTRIM', 'usage:logs', '0', '999'],
        ['INCR', 'usage:total'],
        ['INCR', f'usage:user:{emp_id}'],
        ['INCR', f'usage:dept:{dept}'],
        ['INCR', f'usage:action:{action}'],
        ['INCR', f'usage:daily:{today}'],
    ]
    # 加细粒度计数(风格/产品)
    style = (details or {}).get('style')
    if style:
        commands.append(['INCR', f'usage:style:{style}'])
    brand = (details or {}).get('brand')
    if brand:
        commands.append(['INCR', f'usage:brand:{brand}'])

    _kv_req('POST', '/pipeline', body=commands, timeout=3)


def get_recent_logs(n: int = 100):
    """取最近 N 条事件(新→旧)"""
    if not _kv_available():
        return []
    res = _kv_req('GET', f'/lrange/usage:logs/0/{n-1}')
    if not res:
        return []
    items = res.get('result') or []
    parsed = []
    for x in items:
        try:
            parsed.append(json.loads(x))
        except Exception:
            continue
    return parsed


def _list_counters_by_prefix(prefix: str) -> dict:
    """枚举所有 usage:xxx:* 计数器并取值,返回 {key: int}"""
    if not _kv_available():
        return {}
    pattern = f'{prefix}*'
    res = _kv_req('GET', f'/keys/{urllib.parse.quote(pattern)}')
    if not res:
        return {}
    keys = res.get('result') or []
    if not keys:
        return {}
    # 一次性 MGET
    path = '/mget/' + '/'.join(urllib.parse.quote(k) for k in keys)
    res2 = _kv_req('GET', path)
    if not res2:
        return {}
    values = res2.get('result') or []
    out = {}
    for k, v in zip(keys, values):
        try:
            out[k] = int(v) if v is not None else 0
        except (TypeError, ValueError):
            out[k] = 0
    return out


def get_counter(key: str) -> int:
    if not _kv_available():
        return 0
    res = _kv_req('GET', f'/get/{urllib.parse.quote(key)}')
    if not res:
        return 0
    val = res.get('result')
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def get_stats():
    """聚合所有维度数据,供 /api/admin-stats 返回"""
    if not _kv_available():
        return None

    total = get_counter('usage:total')
    by_user = _list_counters_by_prefix('usage:user:')
    by_dept = _list_counters_by_prefix('usage:dept:')
    by_action = _list_counters_by_prefix('usage:action:')
    by_style = _list_counters_by_prefix('usage:style:')
    by_brand = _list_counters_by_prefix('usage:brand:')
    by_daily = _list_counters_by_prefix('usage:daily:')

    def _strip_prefix(d, prefix):
        return [{'key': k[len(prefix):], 'count': v} for k, v in d.items()]

    return {
        'total': total,
        'by_user_raw': _strip_prefix(by_user, 'usage:user:'),
        'by_dept': sorted(_strip_prefix(by_dept, 'usage:dept:'), key=lambda x: -x['count']),
        'by_action': sorted(_strip_prefix(by_action, 'usage:action:'), key=lambda x: -x['count']),
        'by_style': sorted(_strip_prefix(by_style, 'usage:style:'), key=lambda x: -x['count']),
        'by_brand': sorted(_strip_prefix(by_brand, 'usage:brand:'), key=lambda x: -x['count']),
        'by_daily': sorted(_strip_prefix(by_daily, 'usage:daily:'), key=lambda x: x['key'], reverse=True)[:30],
        'recent': get_recent_logs(100),
    }
