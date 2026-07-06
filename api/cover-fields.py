"""
POST /api/cover-fields

业务在封面生成面板里点"一键导入"时调用。
基于:品牌 + 产品 + 文案类型 + (可选)补充信息,自动生成 3 个**适合上封面的短字段**:
{
    "main_title": "客厅多了它",
    "subtitle": "30+ 真香",
    "hua_text": "亲测✓"
}

复用现有 DeepSeek 通路,prompt 用 prompts/封面字段.txt。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import json
import os
import re
import urllib.request
import urllib.error


ROOT = Path(__file__).resolve().parent.parent
import sys as _sys_boot
if str(ROOT) not in _sys_boot.path:
    _sys_boot.path.insert(0, str(ROOT))
PROMPT_FILE = ROOT / "prompts" / "封面字段.txt"
PRODUCTS_FILE = ROOT / "data" / "products.json"


def load_products():
    with PRODUCTS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_product(data, brand_name, product_name):
    for b in data.get("brands", []):
        if b["name"] == brand_name:
            for p in b.get("products", []):
                if p["name"] == product_name:
                    return b, p
    return None, None


def call_deepseek(system_prompt, user_prompt):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 1.0,   # 提高温度,让每次"一键导入"结果更多样
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        raise RuntimeError(f"DeepSeek API 错误 {e.code}: {detail[:300]}")
    return data["choices"][0]["message"]["content"]


def parse(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            brand_name = (req.get("brand") or "").strip()
            product_name = (req.get("product") or "").strip()
            copy_type = (req.get("copy_type") or "").strip()
            extra = (req.get("extra") or "").strip()

            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._json(401, {"error": "未登录或登录已过期,请重新登录"})
            from lib.ratelimit import check as _rl_check
            _ok, _msg = _rl_check(user, self.client_address[0] if self.client_address else '', 'cover_fields')
            if not _ok:
                return self._json(429, {"error": _msg})
            print(f"[USAGE] action=cover_fields user={user.get('emp_id')}/{user.get('name')}/{user.get('department')} brand={brand_name} product={product_name}", flush=True)
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
                from lib.kv_store import log_event
                log_event('cover_fields', user, {'brand': brand_name, 'product': product_name})
            except Exception:
                pass

            # 上一次生成的文案上下文(可选,有的话作为素材给模型)
            existing_titles = req.get("titles") or []
            existing_body = (req.get("body") or "").strip()
            existing_tags = req.get("tags") or []

            if not brand_name or not product_name:
                return self._error(400, "缺少 brand / product")

            import sys as _sys
            _sys.path.insert(0, str(ROOT))
            from lib.products_store import find_product as _ps_find
            brand, product = _ps_find(brand_name, product_name)
            if not brand or not product:
                return self._error(404, f"产品不存在:{brand_name} / {product_name}")

            system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
            user_msg_parts = [
                f"【品牌】{brand_name}",
                f"【产品】{product_name}",
                f"【文案类型】{copy_type or '通用'}",
                f"\n【产品资料(基础信息)】\n{product['content']}",
            ]
            # 把已生成的文案做为额外素材给模型
            if existing_titles:
                titles_text = "\n".join(f"  - {t}" for t in existing_titles[:5])
                user_msg_parts.append(f"\n【已生成的 5 个候选标题(可借鉴/改写)】\n{titles_text}")
            if existing_body:
                # 正文截断 800 字以内,避免超 token
                snippet = existing_body[:800] + ("..." if len(existing_body) > 800 else "")
                user_msg_parts.append(f"\n【已生成的笔记正文(可萃取场景/情绪/数据点)】\n{snippet}")
            if existing_tags:
                user_msg_parts.append(f"\n【话题标签】\n{' '.join(existing_tags)}")
            if extra:
                user_msg_parts.append(f"\n【业务补充】{extra}")
            user_msg_parts.append(
                "\n请综合以上所有素材,提炼 3 个**互不重复**的封面字段。"
                "每次请求都要给出和之前不同的结果,不要重复套路。严格按 JSON 输出。"
            )
            user_prompt = "\n".join(user_msg_parts)

            raw = call_deepseek(system_prompt, user_prompt)
            parsed = parse(raw)

            self._json(200, {
                "main_title": (parsed.get("main_title") or "").strip(),
                "subtitle": (parsed.get("subtitle") or "").strip(),
                "hua_text": (parsed.get("hua_text") or "").strip(),
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._error(500, "服务器开小差了,请稍后重试")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._json(code, {"error": msg})
