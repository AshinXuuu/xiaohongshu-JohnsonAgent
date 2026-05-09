"""
POST /api/generate
请求体:
{
    "brand": "乔山Johnson",
    "product": "TX-5智能跑步机",
    "copy_type": "种草",   // 种草/场景/生活/促销/干货/封面金句
    "extra": "可选,业务的额外补充(目标人群/场景偏好/活动信息等)"
}

响应:
{
    "titles": ["..."],
    "body": "...",
    "tags": ["..."],
    "model": "deepseek-chat"
}
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import json
import os
import re
import urllib.request
import urllib.error


ROOT = Path(__file__).resolve().parent.parent
ALLOWED_TYPES = {"种草", "场景", "生活", "促销", "干货", "封面金句"}


def load_products():
    with (ROOT / "data" / "products.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def find_product(data, brand_name, product_name):
    for b in data.get("brands", []):
        if b["name"] == brand_name:
            for p in b.get("products", []):
                if p["name"] == product_name:
                    return b, p
    return None, None


def load_prompt(copy_type):
    base = (ROOT / "prompts" / "base.txt").read_text(encoding="utf-8")
    type_prompt = (ROOT / "prompts" / f"{copy_type}.txt").read_text(encoding="utf-8")
    return base + "\n\n" + type_prompt


def build_user_message(brand, product, copy_type, extra):
    parts = [
        f"【品牌】{brand['name']}",
        f"【产品】{product['name']}",
        f"【文案类型】{copy_type}",
    ]
    if brand.get("guidelines"):
        parts.append(f"\n【品牌调性参考】\n{brand['guidelines']}")
    parts.append(f"\n【产品资料】\n{product['content']}")
    if extra:
        parts.append(f"\n【业务补充信息】\n{extra}")
    parts.append("\n请严格按 JSON 格式输出,不要加任何解释或代码块标记。")
    return "\n".join(parts)


def call_deepseek(system_prompt, user_prompt):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未配置")

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 2000,
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


def enforce_brand_product_tags(tags, brand_name, product_name):
    """
    强制保证标签列表第 1 个为品牌、第 2 个为产品。
    - 如果 AI 已经在某个位置写了对应标签,挪到最前面去重
    - 如果 AI 完全没写,直接补在最前
    - 比较时忽略 # 号和大小写,例如 '#搏飞bowflex' 和 '搏飞BowFlex' 视为同一个
    """
    brand_tag = f"#{brand_name}"
    product_tag = f"#{product_name}"

    def norm(t):
        return t.lstrip("#").strip().lower()

    brand_norm = norm(brand_tag)
    product_norm = norm(product_tag)

    # 移除已有的品牌/产品标签(无论 AI 用了什么大小写或位置)
    rest = [
        t for t in tags
        if isinstance(t, str) and norm(t) not in (brand_norm, product_norm)
    ]
    # 强制插到最前
    return [brand_tag, product_tag] + rest


def parse_model_output(raw):
    """模型偶尔会带 markdown 代码块,做一次容错"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


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
                return self._error(400, "请选择品牌和产品")
            if copy_type not in ALLOWED_TYPES:
                return self._error(400, f"文案类型必须是: {', '.join(ALLOWED_TYPES)}")

            data = load_products()
            brand, product = find_product(data, brand_name, product_name)
            if not brand or not product:
                return self._error(404, f"找不到产品: {brand_name} / {product_name}")

            system_prompt = load_prompt(copy_type)
            user_prompt = build_user_message(brand, product, copy_type, extra)

            raw = call_deepseek(system_prompt, user_prompt)
            parsed = parse_model_output(raw)

            # 字段补全防御
            parsed.setdefault("titles", [])
            parsed.setdefault("body", "")
            parsed.setdefault("tags", [])

            # 强制注入品牌和产品标签到最前面(去重 + 保序)
            parsed["tags"] = enforce_brand_product_tags(
                parsed["tags"], brand["name"], product["name"]
            )

            parsed["model"] = "deepseek-chat"

            self._json(200, parsed)
        except json.JSONDecodeError as e:
            self._error(500, f"解析模型输出失败,请重试一次。原因:{e}")
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
