"""
POST /api/canva-make-cover

请求体:
{
    "brand": "乔山Johnson",
    "product": "智能跑步机TX-5",
    "template_id": "EAFxxxxx",            // 从 /api/canva-templates 选
    "title": "客厅多了它",                  // 主标题
    "subtitle": "30+ 真香",                // 副标题(可选)
    "tag": "居家健身"                       // 角标(可选)
}

响应:
{
    "edit_url": "https://www.canva.com/design/xxx/edit",  // 直接跳这个
    "design_id": "xxx"
}
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cover.canva_client import make_cover


ROOT = Path(__file__).resolve().parent.parent


def find_product_photo(brand, product):
    """根据品牌+产品名找到对应的 封面图.jpg"""
    candidates = [
        ROOT / "产品库" / brand / product / "封面图.jpg",
        ROOT / "产品库" / brand / product / "封面图.png",
        ROOT / "产品库" / brand / product / "封面图.jpeg",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


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

            brand = (req.get("brand") or "").strip()
            product = (req.get("product") or "").strip()
            template_id = (req.get("template_id") or "").strip()
            title = (req.get("title") or "").strip()
            subtitle = (req.get("subtitle") or "").strip()
            tag = (req.get("tag") or "").strip()

            if not all([brand, product, template_id, title]):
                return self._json(400, {"error": "缺少必填:brand, product, template_id, title"})

            refresh = os.environ.get("CANVA_REFRESH_TOKEN", "")
            if not refresh:
                return self._json(503, {
                    "error": "Canva 集成尚未完成首次授权,请管理员访问 /api/canva-auth 走一次 OAuth"
                })

            photo = find_product_photo(brand, product)
            if not photo:
                return self._json(404, {
                    "error": f"找不到产品图: 产品库/{brand}/{product}/封面图.jpg,请先放置"
                })

            # 拼 autofill 文字数据(模板里有的字段才会被填,没的就忽略)
            autofill_text = {"title": title}
            if subtitle:
                autofill_text["subtitle"] = subtitle
            if tag:
                autofill_text["tag"] = tag

            result = make_cover(
                refresh_token=refresh,
                template_id=template_id,
                autofill_text=autofill_text,
                photo_path=str(photo),
            )

            # 如果 Canva 返回了新的 refresh_token,提示管理员更新 env
            response = {
                "edit_url": result["edit_url"],
                "view_url": result["view_url"],
                "design_id": result["design_id"],
            }
            if result.get("new_refresh_token"):
                response["_admin_note"] = (
                    "Canva 返回了新的 refresh_token,建议更新 Vercel env: "
                    f"{result['new_refresh_token']}"
                )

            self._json(200, response)

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
