"""
Vercel KV(Upstash Redis 兼容)REST API 极简客户端。

Vercel 在你的项目里接入 KV 后,会自动注入 2 个环境变量:
  - KV_REST_API_URL
  - KV_REST_API_TOKEN

代码无需做任何配置,导入即用。
"""
import os
import json
import urllib.request
import urllib.parse
import urllib.error


def _kv_request(method: str, path: str, body=None):
    base = os.environ.get("KV_REST_API_URL", "").rstrip("/")
    token = os.environ.get("KV_REST_API_TOKEN", "")
    if not base or not token:
        raise RuntimeError(
            "Vercel KV 未配置。请去 Vercel 项目 → Storage → 创建 KV 数据库并连接到项目,"
            "之后 KV_REST_API_URL/KV_REST_API_TOKEN 会自动注入。"
        )
    url = f"{base}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        if isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        raise RuntimeError(f"KV 操作失败 {e.code}: {detail[:300]}")


def kv_get(key: str):
    """取值,不存在返回 None"""
    res = _kv_request("GET", f"/get/{urllib.parse.quote(key)}")
    return res.get("result")


def kv_set(key: str, value: str, ttl_seconds: int = None):
    """写值,可选 TTL"""
    # 通过 POST body 传值,避免 URL 长度问题
    if ttl_seconds:
        path = f"/setex/{urllib.parse.quote(key)}/{int(ttl_seconds)}"
    else:
        path = f"/set/{urllib.parse.quote(key)}"
    return _kv_request("POST", path, body=value)


def kv_del(key: str):
    return _kv_request("POST", f"/del/{urllib.parse.quote(key)}")
