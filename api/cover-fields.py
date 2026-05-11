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
        "temperature": 0.85,
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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

            if not brand_name or not product_name:
                return self._error(400, "缺少 brand / product")

            data = load_products()
            brand, product = find_product(data, brand_name, product_name)
            if not brand or not product:
                return self._error(404, f"产品不存在:{brand_name} / {product_name}")

            system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
            user_msg_parts = [
                f"【品牌】{brand_name}",
                f"【产品】{product_name}",
                f"【文案类型】{copy_type or '通用'}",
                f"\n【产品资料】\n{product['content']}",
            ]
            if extra:
                user_msg_parts.append(f"\n【业务补充】{extra}")
            user_msg_parts.append("\n请严格按 JSON 格式输出 3 个字段。")
            user_prompt = "\n".join(user_msg_parts)

            raw = call_deepseek(system_prompt, user_prompt)
            parsed = parse(raw)

            self._json(200, {
                "main_title": (parsed.get("main_title") or "").strip(),
                "subtitle": (parsed.get("subtitle") or "").strip(),
                "hua_text": (parsed.get("hua_text") or "").strip(),
            })
        except Exception as e:
            self._error(500, str(e))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._json(code, {"error": msg})
