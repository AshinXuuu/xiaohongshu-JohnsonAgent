"""服务端权限校验(RBAC)。

核心原则:**谁能做什么,一律以 users.json 的服务端记录为准**,
绝不信任前端传来的 _user.role / is_admin —— 那些字段前端可以伪造。
所有需要鉴权的接口都应调用本模块,而不是各自判断。

角色等级(从高到低):
    super_admin   平台超管(跨组织,未来多租户用)
    org_admin     组织管理员(本公司后台)
    dept_manager  部门管理员(预留,KOS 任务分发可能用到)
    staff         普通业务(默认)
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERS_PATH = ROOT / "data" / "users.json"

ADMIN_ROLES = ("org_admin", "super_admin")
ALL_ROLES = ("super_admin", "org_admin", "dept_manager", "staff")


def find_user_record(user: dict):
    """按 部门 + 工号 + 姓名 找到服务端记录;找不到返回 None。
    数据源已收口到数据库(lib/users_store),DB 异常时自动回退 users.json。
    """
    if not user:
        return None
    from lib.users_store import get_user
    return get_user(user.get("department"), user.get("name"), user.get("emp_id"))


def role_of(user: dict) -> str:
    """服务端核对后的真实角色;未匹配/未知用户 → 最小权限 staff。"""
    rec = find_user_record(user)
    if not rec:
        return "staff"
    return rec.get("role") or ("org_admin" if rec.get("is_admin") else "staff")


def org_of(user: dict) -> str:
    rec = find_user_record(user)
    return (rec or {}).get("org") or "johnson"


def has_role(user: dict, allowed) -> bool:
    """user 的服务端角色是否在 allowed 列表内。"""
    return role_of(user) in tuple(allowed)


def is_admin(user: dict) -> bool:
    """是否为管理员(org_admin 或 super_admin)。"""
    return role_of(user) in ADMIN_ROLES
