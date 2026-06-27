"""组织(租户)配置加载器。

现在只有一个组织 johnson(乔山),但接口按"多组织就绪"设计:
所有"乔山专属"的信息(品牌名、主色、启用哪些模块、谁能进后台)都从
data/orgs/<org>.json 读,不写死在代码里。未来复制给别的公司 = 多一份配置文件。

用法:
    from lib.org import load_org, public_org, visible_modules
    org = load_org("johnson")
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORG_DIR = ROOT / "data" / "orgs"
DEFAULT_ORG = "johnson"

_cache = {}


def load_org(org_id: str = DEFAULT_ORG) -> dict:
    """读组织配置(带简单缓存)。找不到就回最小可用默认值。"""
    org_id = (org_id or DEFAULT_ORG).strip() or DEFAULT_ORG
    if org_id in _cache:
        return _cache[org_id]
    path = ORG_DIR / f"{org_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {
            "id": org_id,
            "name": "销售助手",
            "short_name": "",
            "logo": None,
            "theme": {"accent": "#6366f1", "accent_strong": "#4338ca", "accent_soft": "#eef2ff"},
            "modules": [],
            "admin": {"href": "/admin.html", "roles": ["org_admin", "super_admin"]},
        }
    _cache[org_id] = data
    return data


def public_org(org_id: str = DEFAULT_ORG) -> dict:
    """对前端暴露的公开配置(此处本就无敏感字段,保留函数便于将来过滤)。"""
    org = load_org(org_id)
    return {
        "id": org.get("id"),
        "name": org.get("name"),
        "short_name": org.get("short_name"),
        "logo": org.get("logo"),
        "theme": org.get("theme", {}),
        "modules": org.get("modules", []),
        "admin": org.get("admin", {}),
    }
