"""
Canva Connect API 客户端 — 封装 OAuth、上传素材、调用 autofill。

文档:https://www.canva.dev/docs/connect/

设计原则:
- 业务方共用一个 Canva 账号(运营/营销同事的 Pro 账号)托管所有"品牌模板"
- 首次让该账号完成 OAuth,得到一个长效 refresh_token,存到 Vercel env
- 之后每次调用时用 refresh_token 换一个短效 access_token(自动刷新)
- 业务点"做封面" → 后端调用 autofill API → 返回 Canva 编辑链接 → 浏览器跳转
"""
import os
import json
import time
import base64
import hashlib
import secrets
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path


CANVA_API_BASE = "https://api.canva.com/rest/v1"
CANVA_AUTH_BASE = "https://www.canva.com/api/oauth"

# OAuth 授权时申请的权限范围
SCOPES = [
    "asset:read",
    "asset:write",
    "design:meta:read",
    "design:content:read",
    "design:content:write",
    "brandtemplate:meta:read",
    "brandtemplate:content:read",
]


# ─────────── PKCE 辅助 ───────────

def gen_pkce():
    """生成 PKCE code_verifier + code_challenge(OAuth 2.0 PKCE 流程必需)"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ─────────── HTTP 工具 ───────────

def _http(method, url, headers=None, data=None, form=False):
    """通用 HTTP 请求,返回解析后的 JSON"""
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
        elif isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            headers = {**(headers or {}), "Content-Type": "application/json"}
        else:
            body = data  # bytes,用于上传素材
    else:
        body = None
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        raise RuntimeError(f"Canva API 错误 {e.code}: {detail[:400]}")


# ─────────── OAuth ───────────

def build_authorize_url(client_id, redirect_uri, code_challenge, state):
    """构造 Canva OAuth 授权页 URL,业务点击后跳过去"""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{CANVA_AUTH_BASE}/authorize?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(client_id, client_secret, code, code_verifier, redirect_uri):
    """OAuth 回调拿到 code 后,换 access_token + refresh_token"""
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return _http(
        "POST",
        f"{CANVA_API_BASE}/oauth/token",
        headers={"Authorization": f"Basic {auth}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        form=True,
    )


def refresh_access_token(client_id, client_secret, refresh_token):
    """用 refresh_token 换新的 access_token(短效,1 小时左右)"""
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return _http(
        "POST",
        f"{CANVA_API_BASE}/oauth/token",
        headers={"Authorization": f"Basic {auth}"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        form=True,
    )


# ─────────── 资源 API ───────────

def list_brand_templates(access_token):
    """列出所有品牌模板,前端的"选模板"下拉框就用它"""
    return _http(
        "GET",
        f"{CANVA_API_BASE}/brand-templates",
        headers={"Authorization": f"Bearer {access_token}"},
    )


def get_brand_template_dataset(access_token, template_id):
    """查模板里有哪些命名占位符(autofill 字段),用来知道能填什么"""
    return _http(
        "GET",
        f"{CANVA_API_BASE}/brand-templates/{template_id}/dataset",
        headers={"Authorization": f"Bearer {access_token}"},
    )


def upload_asset(access_token, name, image_bytes):
    """上传图片到 Canva 资产库,返回 asset_id 供 autofill 引用"""
    name_b64 = base64.b64encode(name.encode()).decode()
    return _http(
        "POST",
        f"{CANVA_API_BASE}/asset-uploads",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/octet-stream",
            "Asset-Upload-Metadata": json.dumps({"name_base64": name_b64}),
        },
        data=image_bytes,
    )


def wait_for_asset_upload(access_token, job_id, timeout=60):
    """素材上传是异步的,轮询直到完成"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _http(
            "GET",
            f"{CANVA_API_BASE}/asset-uploads/{job_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        status = res.get("job", {}).get("status")
        if status == "success":
            return res["job"]["asset"]
        if status == "failed":
            raise RuntimeError(f"素材上传失败: {res}")
        time.sleep(1)
    raise RuntimeError("素材上传超时")


def create_autofill_job(access_token, template_id, data):
    """
    用 autofill 数据从模板创建设计。data 形如:
    {
        "title": {"type": "text", "text": "客厅多了它"},
        "product_image": {"type": "image", "asset_id": "abc123"},
    }
    """
    return _http(
        "POST",
        f"{CANVA_API_BASE}/autofills",
        headers={"Authorization": f"Bearer {access_token}"},
        data={
            "brand_template_id": template_id,
            "data": data,
        },
    )


def wait_for_autofill(access_token, job_id, timeout=120):
    """autofill 也是异步,轮询拿结果"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _http(
            "GET",
            f"{CANVA_API_BASE}/autofills/{job_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        status = res.get("job", {}).get("status")
        if status == "success":
            return res["job"]["result"]["design"]
        if status == "failed":
            raise RuntimeError(f"Autofill 失败: {res}")
        time.sleep(1.5)
    raise RuntimeError("Autofill 超时")


# ─────────── 高级封装:一站式生成封面 ───────────

def make_cover(refresh_token, template_id, autofill_text, photo_path):
    """
    一站式调用:
    1. 用 refresh_token 换 access_token
    2. 上传产品照片 → 拿 asset_id
    3. 调 autofill 把照片和文字塞进模板
    4. 返回设计的编辑 URL

    autofill_text 形如:
    {
        "title": "客厅多了它",
        "subtitle": "30+ 真香",
        ...其他模板里命名的文字占位符
    }
    """
    client_id = os.environ["CANVA_CLIENT_ID"]
    client_secret = os.environ["CANVA_CLIENT_SECRET"]

    # 1. 刷新 access_token
    token = refresh_access_token(client_id, client_secret, refresh_token)
    access_token = token["access_token"]
    new_refresh = token.get("refresh_token", refresh_token)

    # 2. 上传图片
    with open(photo_path, "rb") as f:
        photo_bytes = f.read()
    upload = upload_asset(access_token, Path(photo_path).name, photo_bytes)
    asset = wait_for_asset_upload(access_token, upload["job"]["id"])
    asset_id = asset["id"]

    # 3. 拼 autofill 数据
    data = {k: {"type": "text", "text": v} for k, v in autofill_text.items()}
    data["product_image"] = {"type": "image", "asset_id": asset_id}

    job = create_autofill_job(access_token, template_id, data)
    design = wait_for_autofill(access_token, job["job"]["id"])

    return {
        "edit_url": design["urls"]["edit_url"],
        "view_url": design["urls"]["view_url"],
        "design_id": design["id"],
        "new_refresh_token": new_refresh if new_refresh != refresh_token else None,
    }
