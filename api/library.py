"""产品资料库 —— 在线下载产品资料(单页 / 中英文说明书)。

资料文件存放在腾讯云对象存储(COS)私有桶里,本接口:
  - GET  /api/library                 → 返回资料清单(品牌→产品→文件,不含直链)
  - POST /api/library  {key, _user}   → 校验 key 合法后,返回一条有时效的 COS 预签名下载链接,
                                         并记录一次 download 事件

设计要点:
  - 桶保持私有,链接由后端用密钥临时签发(默认 5 分钟过期),外泄也会失效
  - 只有出现在 manifest 里的 key 才允许下载(防止有人构造 key 去拿「价格与政策」等未开放资料)
  - COS 凭据全部读环境变量,不写死在代码里

需要在服务器 .env 配置:
  COS_SECRET_ID=...
  COS_SECRET_KEY=...
  COS_REGION=ap-shanghai          # 桶所在地域
  COS_BUCKET=xxxx-1250000000      # 桶名(含 APPID)
  COS_PREFIX=产品库/               # 上传时的 Key 前缀(若直接传了产品库文件夹就是「产品库/」)
  COS_URL_EXPIRE=300              # 链接有效期(秒),可选,默认 300
"""
import json
import os
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MANIFEST_PATH = ROOT / "data" / "library_manifest.json"


# ──────────────────────── 数据 ────────────────────────

def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"brands": {}}


def allowed_keys(manifest: dict) -> dict:
    """返回 {key: {brand, product, name, type}},既做白名单也做下载时的元信息。"""
    out = {}
    for brand, products in (manifest.get("brands") or {}).items():
        for p in products:
            for f in p.get("files", []):
                out[f["key"]] = {
                    "brand": brand,
                    "product": p["name"],
                    "name": f["name"],
                    "type": f.get("type", ""),
                }
    return out


# ──────────────────────── COS ────────────────────────

def cos_config():
    cfg = {
        "secret_id": os.environ.get("COS_SECRET_ID", "").strip(),
        "secret_key": os.environ.get("COS_SECRET_KEY", "").strip(),
        "region": os.environ.get("COS_REGION", "").strip(),
        "bucket": os.environ.get("COS_BUCKET", "").strip(),
        "prefix": os.environ.get("COS_PREFIX", "产品库/"),
        "expire": int(os.environ.get("COS_URL_EXPIRE", "300") or "300"),
    }
    return cfg


def make_presigned_url(key: str, filename: str) -> str:
    """用 COS SDK 生成一条预签名下载链接;强制浏览器下载并保留中文文件名。"""
    from qcloud_cos import CosConfig, CosS3Client

    cfg = cos_config()
    missing = [k for k in ("secret_id", "secret_key", "region", "bucket") if not cfg[k]]
    if missing:
        raise RuntimeError(f"COS 未配置(缺 {', '.join(missing)}),请在服务器 .env 补齐")

    config = CosConfig(
        Region=cfg["region"],
        SecretId=cfg["secret_id"],
        SecretKey=cfg["secret_key"],
        Scheme="https",
    )
    client = CosS3Client(config)
    object_key = f"{cfg['prefix']}{key}"

    # 让浏览器以「下载」方式保存,并用 RFC5987 编码保住中文文件名
    disp = "attachment; filename*=UTF-8''" + quote(filename)
    return client.get_presigned_download_url(
        Bucket=cfg["bucket"],
        Key=object_key,
        Expired=cfg["expire"],
        Params={"response-content-disposition": disp},
    )


# ──────────────────────── 日志 ────────────────────────

def log_download(user: dict, info: dict):
    try:
        sys.path.insert(0, str(ROOT))
        from lib.kv_store import log_event
        log_event("download", user or {}, {
            "brand": info.get("brand"),
            "product": info.get("product"),
            "file": info.get("name"),
            "file_type": info.get("type"),
        })
    except Exception:
        pass


# ──────────────────────── HTTP handler ────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        """GET /api/library[?action=manifest] → 资料清单(不含直链)"""
        try:
            qs = parse_qs(urlparse(self.path).query)
            action = (qs.get("action", ["manifest"])[0]).strip()
            if action in ("", "manifest"):
                from lib.library_store import grouped
                return self._json(200, {"brands": grouped()})
            return self._error(400, "未知 action")
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._error(500, "服务器开小差了,请稍后重试")

    def do_POST(self):
        """POST /api/library {key, _user} → 返回预签名下载链接"""
        req = {}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            key = (req.get("key") or "").strip()
            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._error(401, "未登录或登录已过期,请重新登录")
            if not key:
                return self._error(400, "缺少 key")

            from lib.library_store import allowed_keys as _allowed
            info = _allowed().get(key)
            if not info:
                # 不在白名单 → 拒绝(防止越权拿未开放资料)
                return self._error(403, "该资料不在可下载范围内")

            try:
                url = make_presigned_url(key, info["name"])
            except RuntimeError as e:
                # 配置缺失:给前端一个明确提示
                return self._error(503, str(e))

            log_download(user, info)
            self._json(200, {"url": url, "name": info["name"]})
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
