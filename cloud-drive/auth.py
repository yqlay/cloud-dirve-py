"""认证管理模块 — 支持多用户、角色、权限、注册、审批。"""

import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path

from config import get, update

AUTH_FILE = Path(__file__).parent / "auth.json"

# 权限层级：upload 包含 download
_PERM_HIERARCHY = {"browse": 0, "download": 1, "upload": 2}
# 角色默认拥有的权限
_ROLE_AUTO_PERMS = {
    "super_admin": {"upload", "download", "admin", "console", "terminal"},
    "admin": {"upload", "download", "admin"},
    "user": {"download"},
}


# ── 密码 ──────────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """密码 SHA-256 哈希。"""
    return hashlib.sha256(password.encode()).hexdigest()


# ── 配置同步 ──────────────────────────────────────────────────────────────────

def _sync_from_config() -> None:
    """从 config.json 同步用户到 auth.json。补充缺失用户，删除已移除用户，不覆盖已有密码。"""
    cfg_users = get("auth", "users")
    if not cfg_users or not isinstance(cfg_users, list):
        return

    cfg_usernames = {u["username"] for u in cfg_users}
    auth_data = _load_auth_file()
    auth_users = auth_data.get("users", [])

    # 删除 config 中已不存在的用户
    auth_users = [u for u in auth_users if u["username"] in cfg_usernames]
    auth_usernames = {u["username"] for u in auth_users}

    # 添加 config 中存在但 auth 中不存在的用户
    changed = False
    for cfg in cfg_users:
        if cfg["username"] not in auth_usernames:
            auth_users.append({
                "username": cfg["username"],
                "password": _hash_password(cfg.get("password", "")),
                "role": cfg.get("role", "user"),
                "permissions": cfg.get("permissions", []),
            })
            changed = True

    if changed or len(auth_users) != len(auth_data.get("users", [])):
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": auth_users}, f, indent=2, ensure_ascii=False)


def _load_auth_file() -> dict:
    """读取 auth.json 原始内容。"""
    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_auth_users(users: list[dict]) -> None:
    """保存用户列表到 auth.json（密码哈希存储，保留 role/permissions）。"""
    auth_data = {
        "users": [
            {
                "username": u["username"],
                "password": _hash_password(u["password"]),
                "role": u.get("role", "user"),
                "permissions": u.get("permissions", []),
            }
            for u in users
        ]
    }
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(auth_data, f, indent=2, ensure_ascii=False)


# ── 登录验证 ──────────────────────────────────────────────────────────────────

def check_credentials(username: str, password: str) -> bool:
    """验证用户名和密码。遍历所有用户，使用 hmac 防时序攻击。"""
    _sync_from_config()
    auth_data = _load_auth_file()
    hashed = _hash_password(password)

    for user in auth_data.get("users", []):
        if hmac.compare_digest(username, user.get("username", "")) and \
           hmac.compare_digest(hashed, user.get("password", "")):
            return True
    return False


def change_password(username: str, new_password: str) -> bool:
    """修改指定用户的密码。同步更新 auth.json 和 config.json。"""
    _sync_from_config()

    auth_data = _load_auth_file()
    users = auth_data.get("users", [])
    found = False
    for user in users:
        if user["username"] == username:
            user["password"] = _hash_password(new_password)
            found = True
            break
    if not found:
        return False

    # 同步更新 config.json
    cfg_users = get("auth", "users") or []
    for u in cfg_users:
        if u["username"] == username:
            u["password"] = new_password
            break
    update("auth", "users", cfg_users)

    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, indent=2, ensure_ascii=False)

    return True


def get_all_usernames() -> list[str]:
    """获取所有已批准的用户名列表。"""
    _sync_from_config()
    auth_data = _load_auth_file()
    return [u["username"] for u in auth_data.get("users", [])]


def get_user_password_hash(username: str) -> str | None:
    """获取用户的当前密码哈希。优先从 auth.json 取，不存在则从 config.json 计算。"""
    _sync_from_config()
    auth_data = _load_auth_file()
    for u in auth_data.get("users", []):
        if u["username"] == username:
            return u.get("password")
    return None


def get_config_password_hash(username: str) -> str | None:
    """从 config.json 获取用户的密码哈希（用于校验 config 变更）。用户不存在返回 None。"""
    users = get("auth", "users") or []
    for u in users:
        if u["username"] == username:
            return _hash_password(u.get("password", ""))
    return None


# ── 角色与权限 ─────────────────────────────────────────────────────────────────

def is_admin(username: str) -> bool:
    """判断用户是否为管理员（super_admin 或 admin）。"""
    role = get_user_role(username)
    return role in ("super_admin", "admin")


def is_super_admin(username: str) -> bool:
    """判断用户是否为超级管理员。"""
    return get_user_role(username) == "super_admin"


def get_user_role(username: str) -> str:
    """获取用户角色。"""
    users = get("auth", "users") or []
    for u in users:
        if u["username"] == username:
            return u.get("role", "user")
    return "user"


def get_user_permissions(username: str) -> list[str]:
    """获取用户已授权的权限列表。"""
    users = get("auth", "users") or []
    for u in users:
        if u["username"] == username:
            return u.get("permissions", [])
    return []


def has_permission(username: str, perm: str) -> bool:
    """检查用户是否有指定权限。super_admin 拥有全部，admin 自动拥有 admin/console/terminal。"""
    role = get_user_role(username)
    auto = _ROLE_AUTO_PERMS.get(role, set())
    if perm in auto:
        return True
    perms = get_user_permissions(username)
    # upload 包含 download
    if perm == "download" and "upload" in perms:
        return True
    return perm in perms


def grant_permission(username: str, perm: str) -> bool:
    """授予用户权限。"""
    if perm not in ("download", "upload", "admin", "console", "terminal"):
        return False
    users = get("auth", "users") or []
    for u in users:
        if u["username"] == username:
            perms = u.get("permissions", [])
            if perm not in perms:
                perms.append(perm)
                u["permissions"] = perms
                update("auth", "users", users)
            return True
    return False


def revoke_permission(username: str, perm: str) -> bool:
    """撤销用户权限。"""
    users = get("auth", "users") or []
    for u in users:
        if u["username"] == username:
            perms = u.get("permissions", [])
            if perm in perms:
                perms.remove(perm)
                u["permissions"] = perms
                update("auth", "users", users)
            return True
    return False


# ── 注册 ──────────────────────────────────────────────────────────────────────

def validate_invite_code(code: str) -> bool:
    """验证邀请码。支持单个字符串或列表。为空则跳过验证（开放注册）。"""
    raw = get("auth", "invite_code")
    if not raw:
        return True  # 未设邀请码 = 不需要邀请码

    # 兼容：单个字符串或列表
    codes = raw if isinstance(raw, list) else [raw]
    codes = [str(c).strip() for c in codes if c]

    if not codes:
        return True  # 列表为空 = 不需要邀请码

    return code.strip() in codes


def validate_username(username: str) -> tuple[bool, str]:
    """验证用户名格式和唯一性。返回 (合法, 错误信息)。"""
    if not username or len(username) < 3 or len(username) > 20:
        return False, "用户名长度需 3-20 位"
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return False, "用户名只能包含字母、数字和下划线"

    users = get("auth", "users") or []
    if any(u["username"] == username for u in users):
        return False, "该用户名已被注册"

    pending = get("auth", "pending_users") or []
    if any(u["username"] == username for u in pending):
        return False, "该用户名正在等待审批"

    return True, ""


def register_user(username: str, password: str, message: str = "") -> tuple[bool, str]:
    """注册用户到待审批列表。返回 (成功, 消息)。"""
    ok, msg = validate_username(username)
    if not ok:
        return False, msg

    if not password or len(password) < 6:
        return False, "密码长度至少 6 位"

    # 校验附加消息长度
    max_len = get("auth", "register_message_max_length") or 128
    message = message.strip()
    if len(message.encode("utf-8")) > max_len:
        return False, f"附言不能超过 {max_len} 字节"

    pending = get("auth", "pending_users") or []
    pending.append({
        "username": username,
        "password": password,
        "message": message,
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    update("auth", "pending_users", pending)
    return True, "注册成功，等待管理员审批"


# ── 注册审批 ──────────────────────────────────────────────────────────────────

def get_pending_users() -> list[dict]:
    """获取待审批用户列表。"""
    return get("auth", "pending_users") or []


def approve_user(username: str) -> bool:
    """批准用户：从 pending_users 移到 users（默认 role=user，无权限）。"""
    pending = get("auth", "pending_users") or []
    target = None
    for u in pending:
        if u["username"] == username:
            target = u
            break
    if not target:
        return False

    pending.remove(target)
    update("auth", "pending_users", pending)

    users = get("auth", "users") or []
    users.append({
        "username": target["username"],
        "password": target["password"],
        "role": "user",
        "permissions": [],
    })
    update("auth", "users", users)
    _sync_from_config()
    return True


def reject_user(username: str) -> bool:
    """拒绝用户：从 pending_users 中删除。"""
    pending = get("auth", "pending_users") or []
    target = None
    for u in pending:
        if u["username"] == username:
            target = u
            break
    if not target:
        return False

    pending.remove(target)
    update("auth", "pending_users", pending)
    return True


# ── 权限申请 ──────────────────────────────────────────────────────────────────

def request_permission(username: str, perm: str) -> tuple[bool, str]:
    """用户申请权限。返回 (成功, 消息)。"""
    if perm not in ("download", "upload", "admin", "console", "terminal"):
        return False, "无效的权限类型"

    if has_permission(username, perm):
        return False, "你已拥有该权限"

    pending = get("auth", "pending_permissions") or []

    # 检查是否已有相同申请
    for p in pending:
        if p["username"] == username and p["permission"] == perm:
            return False, "你已提交过该申请，请等待审批"

    pending.append({
        "username": username,
        "permission": perm,
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    update("auth", "pending_permissions", pending)
    return True, "权限申请已提交，等待管理员审批"


def get_pending_permissions() -> list[dict]:
    """获取待审批的权限申请列表。"""
    return get("auth", "pending_permissions") or []


def approve_permission(username: str, perm: str) -> bool:
    """批准权限申请。"""
    pending = get("auth", "pending_permissions") or []
    target = None
    for p in pending:
        if p["username"] == username and p["permission"] == perm:
            target = p
            break
    if not target:
        return False

    pending.remove(target)
    update("auth", "pending_permissions", pending)
    grant_permission(username, perm)
    return True


def reject_permission(username: str, perm: str) -> bool:
    """拒绝权限申请。"""
    pending = get("auth", "pending_permissions") or []
    target = None
    for p in pending:
        if p["username"] == username and p["permission"] == perm:
            target = p
            break
    if not target:
        return False

    pending.remove(target)
    update("auth", "pending_permissions", pending)
    return True
