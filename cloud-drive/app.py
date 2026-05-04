"""Cloud Drive 主程序 — 文件云盘系统。"""

import json as _json
import os
import socket
import subprocess
import sys
import threading
import time as _time
import urllib.request
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_sock import Sock
from jinja2 import TemplateNotFound
from winpty import PTY

from auth import (
    check_credentials, change_password, get_all_usernames, is_admin, is_super_admin,
    validate_invite_code, register_user, get_pending_users, approve_user, reject_user,
    has_permission, request_permission, get_pending_permissions,
    approve_permission, reject_permission, get_user_permissions,
    get_user_role, get_user_password_hash, get_config_password_hash,
)
from config import get, reload as reload_config, update as config_update
from logger import Logger
from messenger import Messenger
from storage import Storage

# ── 初始化 ────────────────────────────────────────────────────────────────────

import logging

app = Flask(__name__)
sock = Sock(app)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# 自动生成并持久化 secret key
_secret = get("server", "secret_key")
if not _secret:
    _secret = os.urandom(24).hex()
    from config import update as _config_update
    _config_update("server", "secret_key", _secret)
app.config["SECRET_KEY"] = _secret
app.config["MAX_CONTENT_LENGTH"] = get("upload", "max_folder_size_mb", 2048) * 1024 * 1024

DATA_DIR = Path(__file__).parent / "data"
storage = Storage(DATA_DIR)
logger = Logger()
messenger = Messenger()

# ── 登录限速 ──────────────────────────────────────────────────────────────────

_login_attempts: dict[str, list[float]] = {}  # IP → [失败时间戳]
_MAX_ATTEMPTS = 5       # 最多失败次数
_LOCKOUT_SECONDS = 300  # 锁定时间（秒）


def _is_locked_out(ip: str) -> bool:
    """检查 IP 是否被锁定。"""
    import time
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # 清理过期记录
    attempts = [t for t in attempts if now - t < _LOCKOUT_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def _record_failure(ip: str) -> None:
    """记录一次登录失败。"""
    import time
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    _login_attempts[ip].append(time.time())


def _clear_attempts(ip: str) -> None:
    """登录成功后清除失败记录。"""
    _login_attempts.pop(ip, None)


# ── 注册限速 ──────────────────────────────────────────────────────────────────

_register_attempts: dict[str, list[float]] = {}
_MAX_REGISTER = 3        # 最多注册尝试次数
_REGISTER_LOCKOUT = 600  # 锁定时间（秒）


def _is_register_locked(ip: str) -> bool:
    """检查 IP 注册是否被锁定。"""
    import time
    now = time.time()
    attempts = _register_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _REGISTER_LOCKOUT]
    _register_attempts[ip] = attempts
    return len(attempts) >= _MAX_REGISTER


def _record_register_attempt(ip: str) -> None:
    """记录一次注册尝试。"""
    import time
    if ip not in _register_attempts:
        _register_attempts[ip] = []
    _register_attempts[ip].append(time.time())


# ── 在线用户追踪 ────────────────────────────────────────────────────────────

import time as _time

_online_users: dict[str, dict] = {}  # username → {"ip", "last_active", "login_at"}
_kicked_users: set[str] = set()      # 被踢出的用户名
_ONLINE_TIMEOUT = 2400  # 40 分钟无活动自动下线


def _get_client_ip() -> str:
    """获取客户端真实 IP。"""
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def _update_online(username: str, session_token: str | None = None) -> None:
    """更新在线用户状态。session_token 首次登录时必传，用于单点登录校验。"""
    now = _time.time()
    ip = _get_client_ip()
    if username not in _online_users:
        _online_users[username] = {
            "ip": ip,
            "last_active": now,
            "login_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "token": session_token,
        }
    else:
        _online_users[username]["ip"] = ip
        _online_users[username]["last_active"] = now
        if session_token:
            _online_users[username]["token"] = session_token


def _cleanup_online() -> None:
    """清理超时的在线用户。"""
    now = _time.time()
    expired = [u for u, d in _online_users.items() if now - d["last_active"] > _ONLINE_TIMEOUT]
    for u in expired:
        del _online_users[u]


def get_online_users() -> list[dict]:
    """获取在线用户列表。"""
    _cleanup_online()
    result = []
    for username, info in _online_users.items():
        ago = int(_time.time() - info["last_active"])
        if ago < 60:
            time_str = f"{ago} 秒前"
        else:
            time_str = f"{ago // 60} 分钟前"
        result.append({
            "username": username,
            "ip": info["ip"],
            "last_active": time_str,
            "login_at": info["login_at"],
            "role": get_user_role(username),
        })
    return result


# ── 权限装饰器 ──────────────────────────────────────────────────────────────

def permission_required(perm: str):
    """权限检查装饰器。perm: 'download' 或 'upload'"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            username = session.get("username", "")
            if not has_permission(username, perm):
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


def _index_redirect(folder_path: str = "", **kwargs):
    """重定向到文件列表页。空路径时跳转根目录。"""
    folder_path = folder_path.strip().strip("/")
    if folder_path:
        return redirect(url_for("index", folder_path=folder_path, **kwargs))
    return redirect(url_for("index", **kwargs))


# ── 认证装饰器 ────────────────────────────────────────────────────────────────

def login_required(f):
    """登录验证装饰器。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """管理员权限装饰器（super_admin 或 admin）。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if not is_admin(session.get("username", "")):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def super_admin_required(f):
    """超级管理员权限装饰器。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if not is_super_admin(session.get("username", "")):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── 模板全局变量 ──────────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    """注入全局模板变量。"""
    username = session.get("username", "")
    return {
        "public_url": get("server", "public_url"),
        "innet_ip_url": get("server", "innet_ip_url", ""),
        "is_admin_user": is_admin(username),
        "is_super_admin": is_super_admin(username),
        "registration_enabled": get("auth", "registration_enabled", False),
        "user_permissions": get_user_permissions(username) if username else [],
        "can_download": has_permission(username, "download") if username else False,
        "can_upload": has_permission(username, "upload") if username else False,
        "can_terminal": has_permission(username, "terminal") if username else False,
        "terminal_enabled": get("terminal", "enabled", True),
    }


# ── HTTP 请求日志 ─────────────────────────────────────────────────────────────

_HTTP_LOG = Path(__file__).parent / "logs" / "http.log"


@app.after_request
def _log_http_request(response):
    """记录 HTTP 请求到 logs/http.log。"""
    if request.endpoint == "static":
        return response
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "-"
    )
    user = session.get("username", "-")
    line = f"[{now}] {ip} {user} {request.method} {request.path} → {response.status_code}\n"
    try:
        _HTTP_LOG.parent.mkdir(exist_ok=True)
        with open(_HTTP_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    return response


# ── 请求前处理：在线追踪 + 踢出检测 ─────────────────────────────────────────

@app.before_request
def _before_request_handler():
    """每次请求前：更新在线状态，检测被踢用户。"""
    # 静态资源和登录/注册页跳过
    if request.endpoint in ("static", "login", "register"):
        return

    username = session.get("username")
    if not username:
        return

    # 检测被踢出
    if username in _kicked_users:
        _kicked_users.discard(username)
        _online_users.pop(username, None)
        session.clear()
        return redirect(url_for("login", msg="你已被管理员强制下线"))

    # 单点登录校验：session token 不匹配说明已在其他地方登录
    stored = _online_users.get(username)
    if stored and stored.get("token") and stored["token"] != session.get("session_token"):
        session.clear()
        return redirect(url_for("login", msg="你的账号已在其他地方登录"))

    # 检测用户是否被删除或密码已变更（通过 config.json 校验）
    cfg_hash = get_config_password_hash(username)
    if cfg_hash is None:
        _online_users.pop(username, None)
        session.clear()
        return redirect(url_for("login", msg="你的账号已被移除"))
    if session.get("pw_hash") != cfg_hash:
        _online_users.pop(username, None)
        session.clear()
        return redirect(url_for("login", msg="密码已变更，请重新登录"))

    # 更新在线状态
    _update_online(username)


# ── 模板过滤器 ────────────────────────────────────────────────────────────────

@app.template_filter("format_size")
def format_size_filter(size_bytes: int) -> str:
    """模板用：格式化文件大小。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@app.template_filter("format_time")
def format_time_filter(iso_str: str) -> str:
    """模板用：截取时间字符串。"""
    return iso_str[:16].replace("T", " ")


# ── 登录路由 ──────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    """登录页面。"""
    error = None
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "unknown"

    if request.method == "POST":
        if _is_locked_out(ip):
            error = f"登录失败次数过多，请 {_LOCKOUT_SECONDS // 60} 分钟后再试"
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if check_credentials(username, password):
                _clear_attempts(ip)
                session["logged_in"] = True
                session["username"] = username
                session["pw_hash"] = get_config_password_hash(username)
                session["session_token"] = os.urandom(16).hex()
                _update_online(username, session["session_token"])
                logger.log(username, "登录系统")
                return redirect(url_for("index"))
            _record_failure(ip)
            remaining = _MAX_ATTEMPTS - len(_login_attempts.get(ip, []))
            if remaining > 0:
                error = f"用户名或密码错误（剩余 {remaining} 次机会）"
            else:
                error = f"登录失败次数过多，请 {_LOCKOUT_SECONDS // 60} 分钟后再试"
                logger.log("系统", f"IP {ip} 被锁定（频繁登录失败）")

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    """登出。"""
    username = session.get("username", "未知")
    logger.log(username, "退出系统")
    session.clear()
    return redirect(url_for("login"))


# ── 注册路由 ──────────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    """用户注册页面。"""
    # 已登录则跳转
    if session.get("logged_in"):
        return redirect(url_for("index"))

    # 注册功能关闭
    if not get("auth", "registration_enabled", False):
        return render_template("register.html", error="注册功能已关闭", disabled=True, need_invite_code=False)

    error = None
    success = None
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "unknown"
    need_invite_code = bool(get("auth", "invite_code"))

    if request.method == "POST":
        if _is_register_locked(ip):
            error = f"注册请求过多，请 {_REGISTER_LOCKOUT // 60} 分钟后再试"
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            invite_code = request.form.get("invite_code", "").strip() if need_invite_code else ""
            message = request.form.get("message", "").strip()

            _record_register_attempt(ip)

            if not validate_invite_code(invite_code):
                error = "邀请码错误"
            elif password != confirm:
                error = "两次输入的密码不一致"
            else:
                ok, msg = register_user(username, password, message)
                if ok:
                    logger.log(username, "提交注册申请")
                    success = msg
                else:
                    error = msg

    max_msg_len = get("auth", "register_message_max_length") or 128
    return render_template("register.html", error=error, success=success, max_msg_len=max_msg_len, need_invite_code=need_invite_code)


# ── 管理员审批路由 ─────────────────────────────────────────────────────────────

@app.route("/admin/registrations")
@admin_required
def admin_registrations():
    """查看待审批注册列表。"""
    pending = get_pending_users()
    pending_perms = get_pending_permissions()
    return render_template(
        "admin_registrations.html",
        pending=pending,
        pending_permissions=pending_perms,
        username=session.get("username"),
        tree=storage.get_folder_tree(),
        current_path="",
    )


@app.route("/admin/registrations/<action>/<username>", methods=["POST"])
@admin_required
def admin_registration_action(action, username):
    """审批操作：approve 或 reject。"""
    if action == "approve":
        if approve_user(username):
            logger.log(session.get("username", "未知"), "批准注册", username)
        else:
            return _index_redirect(msg="审批失败：用户不存在")
    elif action == "reject":
        if reject_user(username):
            logger.log(session.get("username", "未知"), "拒绝注册", username)
        else:
            return _index_redirect(msg="审批失败：用户不存在")
    else:
        abort(400)
    return redirect(url_for("admin_registrations"))


# ── 在线用户管理 ─────────────────────────────────────────────────────────────

@app.route("/api/online")
@login_required
def api_online():
    """在线用户列表（JSON）。"""
    return jsonify(get_online_users())


@app.route("/admin/online")
@admin_required
def admin_online():
    """在线用户管理页面。"""
    return render_template(
        "admin_online.html",
        online_users=get_online_users(),
        username=session.get("username"),
        tree=storage.get_folder_tree(),
        current_path="",
    )


@app.route("/admin/kick/<username>", methods=["POST"])
@admin_required
def admin_kick(username):
    """强制下线指定用户。"""
    if username == session.get("username"):
        return _index_redirect(msg="不能踢出自己")
    if username not in _online_users:
        return _index_redirect(msg=f"用户 {username} 不在线")
    _kicked_users.add(username)
    _online_users.pop(username, None)
    logger.log(session.get("username", "未知"), "强制下线", username)
    return redirect(url_for("admin_online"))


# ── 远程终端 ────────────────────────────────────────────────────────────────

import uuid as _uuid

_terminal_sessions: dict[str, dict] = {}  # id → {"type": "shell"|"console", "cwd": str}

# 危险命令黑名单
_CMD_BLOCKED = [
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){", "fork bomb",
    "shutdown", "reboot", "halt", "poweroff", "init 0", "init 6",
]

# 网页控制台 /restart server 双步确认（key=username, value=过期时间戳）
_web_pending_confirm: dict[str, float] = {}


def _execute_console_command(cmd: str, username: str = "") -> str:
    """执行控制台命令，返回输出文本。"""
    cmd = cmd.strip()
    if not cmd:
        return ""

    if cmd.lower() == "/help":
        return (
            "  Cloud Drive 控制台命令\n"
            "  ─────────────────────────────────────────\n"
            "  /help                        显示此帮助信息\n"
            "  /online                      查看当前在线用户列表（用户名、IP、活跃时间、角色）\n"
            "  /kick <用户名>               将指定用户强制下线，对方下次操作跳转登录页\n"
            "  /approve <用户名>            批准注册申请，用户加入 users 列表\n"
            "  /reject <用户名>             拒绝注册申请，从待审批列表中移除\n"
            "  /grant <用户名> <权限>       授予用户权限: download/upload/admin/console/terminal\n"
            "  /revoke <用户名> <权限>      撤销用户权限: download/upload/admin/console/terminal\n"
            "  /read config                 重新加载 config.json 中的非服务端配置\n"
            "                                 （用户、权限、邀请码、注册开关、上传限制等）\n"
            "  /read server                 重新加载服务端配置（端口、域名、隧道等）\n"
            "  /restart server              重启整个服务进程（需 30 秒内二次确认）\n"
            "  /quit                        退出服务（需 30 秒内二次确认）\n"
            "  /close                       关闭所有终端会话\n"
            "  其他文本                     作为站内消息发送给所有在线用户\n"
            "  ─────────────────────────────────────────\n"
            "  权限说明:\n"
            "    download  — 可以下载文件\n"
            "    upload    — 可以上传、下载、删除、移动、新建文件夹\n"
            "    admin     — 管理用户和权限\n"
            "    console   — 访问控制台\n"
            "    terminal  — 访问服务器终端\n"
            "  ─────────────────────────────────────────\n"
            "  示例:\n"
            "    /approve alice              批准用户 alice 的注册\n"
            "    /grant alice download       授予 alice 下载权限\n"
            "    /kick bob                   强制用户 bob 下线\n"
            "    /revoke alice upload        撤销 alice 的上传权限"
        )

    if cmd.lower() == "/quit":
        if _web_pending_confirm.get(username + "/quit", 0) > _time.time():
            _web_pending_confirm.pop(username + "/quit", None)
            logger.log("控制台", "退出服务", f"用户 {username} 触发退出")

            def _do_quit():
                _time.sleep(0.5)
                os._exit(0)

            threading.Thread(target=_do_quit, daemon=True).start()
            return "  正在退出服务 ..."
        _web_pending_confirm[username + "/quit"] = _time.time() + 30
        return "  ⚠ 确认退出？30 秒内再次输入 /quit 确认"

    if cmd.lower() == "/close":
        with _term_lock:
            count = len(_term_sessions)
            for sess in _term_sessions.values():
                try:
                    sess["pty"].close()
                except Exception:
                    pass
            _term_sessions.clear()
        if count:
            logger.log("控制台", "关闭终端会话", f"{count} 个会话已关闭")
            return f"  已关闭 {count} 个终端会话"
        return "  当前无活跃终端会话"

    if cmd.lower() == "/online":
        users = get_online_users()
        if not users:
            return "  当前无在线用户"
        lines = [f"  在线用户 ({len(users)}):"]
        for u in users:
            lines.append(f"    {u['username']}  IP:{u['ip']}  {u['last_active']}  角色:{u['role']}")
        return "\n".join(lines)

    if cmd.lower().startswith("/kick "):
        target = cmd[6:].strip()
        if target in _online_users:
            _kicked_users.add(target)
            _online_users.pop(target, None)
            logger.log("控制台", "强制下线", target)
            return f"  已踢出用户: {target}"
        return f"  用户 {target} 不在线"

    if cmd.lower().startswith("/approve "):
        target = cmd[9:].strip()
        if approve_user(target):
            logger.log("控制台", "批准注册", target)
            return f"  已批准注册: {target}"
        return f"  未找到待审批用户: {target}"

    if cmd.lower().startswith("/reject "):
        target = cmd[8:].strip()
        if reject_user(target):
            logger.log("控制台", "拒绝注册", target)
            return f"  已拒绝注册: {target}"
        return f"  未找到待审批用户: {target}"

    if cmd.lower().startswith("/grant "):
        parts = cmd[7:].strip().split()
        if len(parts) == 2:
            target, perm = parts
            from auth import grant_permission
            if grant_permission(target, perm):
                logger.log("控制台", "授予权限", f"{target} → {perm}")
                return f"  已授予 {target} 权限: {perm}"
            return "  授予失败（用户不存在或权限类型无效）"
        return "  用法: /grant <用户名> <download|upload|admin|console|terminal>"

    if cmd.lower().startswith("/revoke "):
        parts = cmd[8:].strip().split()
        if len(parts) == 2:
            target, perm = parts
            from auth import revoke_permission
            if revoke_permission(target, perm):
                logger.log("控制台", "撤销权限", f"{target} → {perm}")
                return f"  已撤销 {target} 权限: {perm}"
            return "  撤销失败"
        return "  用法: /revoke <用户名> <download|upload|admin|console|terminal>"

    if cmd.lower() == "/read config":
        try:
            reload_config()
            return "  配置已重新加载（用户、权限、邀请码、上传限制等）"
        except Exception as e:
            return f"  重载失败: {e}"

    if cmd.lower() == "/read server":
        try:
            reload_config()
            if get("self_domain", "enabled", False):
                return f"  服务器配置已重新加载（自定义域名: {get('self_domain', 'domain', '')}）\n  提示: 快速隧道 URL 不变，修改端口/域名需 /restart server"
            return "  服务器配置已重新加载\n  提示: 快速隧道 URL 不变，修改端口需 /restart server"
        except Exception as e:
            return f"  重载失败: {e}"

    if cmd.lower() == "/restart server":
        if _web_pending_confirm.get(username, 0) > _time.time():
            _web_pending_confirm.pop(username, None)
            logger.log("控制台", "重启服务", f"用户 {username} 触发重启")

            def _do_restart():
                _time.sleep(0.5)
                reload_config()
                subprocess.Popen([sys.executable] + sys.argv)
                os._exit(0)

            threading.Thread(target=_do_restart, daemon=True).start()
            return "  正在重启服务 ..."
        _web_pending_confirm[username] = _time.time() + 30
        return "  ⚠ 确认重启？30 秒内再次输入 /restart server 确认"

    # 普通消息 → 发送到站内消息
    messenger.send("服务器", cmd, is_system=True)
    return f"  [已发送] {cmd}"


@app.route("/admin/terminal")
@admin_required
def admin_terminal():
    """远程终端页面。"""
    return render_template(
        "admin_terminal.html",
        username=session.get("username"),
        tree=storage.get_folder_tree(),
        current_path="",
    )


import re as _re

_BLOCK_RE = _re.compile(
    r'<!-- __SPA_TOPBAR__ -->(.*?)<!-- __/SPA_TOPBAR__ -->'
    r'|<!-- __SPA_CONTENT__ -->(.*?)<!-- __/SPA_CONTENT__ -->'
    r'|<!-- __SPA_SCRIPTS__ -->(.*?)<!-- __/SPA_SCRIPTS__ -->',
    _re.DOTALL,
)


def _render_blocks(template_name: str, **ctx) -> dict:
    """渲染完整模板并提取 topbar / content / scripts 三个片段。

    使用 render_template 渲染完整 HTML，然后通过标记注释提取各 block。
    这样模板中的宏（如 render_move_tree）可以正常工作。
    """
    html = render_template(template_name, **ctx)
    blocks = {"topbar": "", "content": "", "scripts": ""}
    for m in _BLOCK_RE.finditer(html):
        if m.group(1) is not None:
            blocks["topbar"] = m.group(1).strip()
        elif m.group(2) is not None:
            blocks["content"] = m.group(2).strip()
        elif m.group(3) is not None:
            blocks["scripts"] = m.group(3).strip()
    return blocks


SPA_PAGE_MAP = {
    "files":          "index.html",
    "logs":           "logs.html",
    "settings":       "settings.html",
    "admin/registrations": "admin_registrations.html",
    "admin/online":   "admin_online.html",
    "admin/terminal": "admin_terminal.html",
}


@app.route("/api/page/<path:page_name>")
@login_required
def api_page(page_name: str):
    """SPA 片段 API — 返回页面 topbar / content / scripts HTML。"""
    if page_name not in SPA_PAGE_MAP:
        abort(404)

    # 权限检查
    username = session.get("username", "")
    if page_name.startswith("admin/") and not is_admin(username):
        abort(403)
    if page_name == "admin/terminal" and not has_permission(username, "terminal"):
        abort(403)

    # 终端页不需要返回内容（终端容器始终在 DOM 中）
    if page_name == "admin/terminal":
        return jsonify({
            "topbar": "", "content": "", "scripts": "",
            "title": "远程终端 - Cloud Drive",
            "is_terminal": True,
        })

    ctx = {
        "username": session.get("username"),
        "tree": storage.get_folder_tree(),
        "current_path": "",
    }

    # 各页面特定的模板变量
    folder_path = request.args.get("path", "")
    if page_name == "files":
        ctx["files"] = storage.get_folder_contents(folder_path)
        ctx["current_path"] = folder_path
        ctx["msg"] = request.args.get("msg")
        ctx["is_admin_user"] = is_admin(session.get("username", ""))
        ctx["user_permissions"] = get_user_permissions(session.get("username", ""))
    elif page_name == "logs":
        ctx["logs"] = logger.get_recent_logs(100)
    elif page_name == "settings":
        ctx["msg"] = None
    elif page_name == "admin/registrations":
        ctx["pending"] = get_pending_users()
        ctx["pending_permissions"] = get_pending_permissions()
    elif page_name == "admin/online":
        ctx["online_users"] = get_online_users()

    try:
        blocks = _render_blocks(SPA_PAGE_MAP[page_name], **ctx)
    except TemplateNotFound:
        abort(404)

    title = {
        "files":          "全部文件 - Cloud Drive",
        "logs":           "操作日志 - Cloud Drive",
        "settings":       "设置 - Cloud Drive",
        "admin/registrations": "注册审批 - Cloud Drive",
        "admin/online":   "在线用户 - Cloud Drive",
    }.get(page_name, "Cloud Drive")

    return jsonify({
        "topbar": blocks["topbar"],
        "content": blocks["content"],
        "scripts": blocks["scripts"],
        "title": title,
    })


@app.route("/api/terminal/create", methods=["POST"])
@admin_required
def terminal_create():
    """创建终端会话。"""
    term_type = request.json.get("type", "shell") if request.is_json else "shell"
    if term_type not in ("shell", "console"):
        return jsonify({"error": "类型必须是 shell 或 console"}), 400

    session_id = _uuid.uuid4().hex[:8]
    cwd = str(Path(__file__).parent.parent)
    _terminal_sessions[session_id] = {"type": term_type, "cwd": cwd}

    # 分配序号
    type_count = sum(1 for s in _terminal_sessions.values() if s["type"] == term_type)
    label = f"{'系统终端' if term_type == 'shell' else '控制台'} {type_count}"

    return jsonify({"session_id": session_id, "type": term_type, "cwd": cwd, "label": label})


@app.route("/api/terminal/close", methods=["POST"])
@admin_required
def terminal_close():
    """关闭终端会话。"""
    session_id = request.json.get("session_id", "") if request.is_json else ""
    if session_id in _terminal_sessions:
        del _terminal_sessions[session_id]
    # 同时关闭 WebSocket PTY 会话
    with _term_lock:
        sess = _term_sessions.pop(session_id, None)
        if sess:
            try:
                sess["pty"].close()
            except Exception:
                pass
    return jsonify({"ok": True})


@app.route("/api/terminal/restart", methods=["POST"])
@admin_required
def terminal_restart():
    """关闭所有终端会话（下次连接自动创建新会话）。"""
    with _term_lock:
        for sess in _term_sessions.values():
            try:
                sess["pty"].close()
            except Exception:
                pass
        _term_sessions.clear()
    return jsonify({"ok": True, "message": "所有终端会话已关闭"})


@app.route("/api/terminal/exec", methods=["POST"])
@admin_required
def terminal_exec():
    """执行命令并以 SSE 流式返回输出。"""
    import subprocess

    data = request.json if request.is_json else {}
    cmd = data.get("cmd", "").strip()
    session_id = data.get("session_id", "")

    if not cmd:
        return jsonify({"error": "命令不能为空"}), 400

    # 获取会话
    sess = _terminal_sessions.get(session_id)
    if not sess:
        return jsonify({"error": "终端会话不存在，请重新打开终端"}), 404

    username = session.get("username", "未知")
    logger.log(username, f"执行命令({sess['type']})", cmd[:120])

    # ── 控制台模式 ──
    if sess["type"] == "console":
        def generate():
            try:
                output = _execute_console_command(cmd, username)
                for line in output.split("\n"):
                    yield f"data: {line}\n\n"
            except Exception as e:
                yield f"data: [错误: {e}]\n\n"
            yield "event: done\ndata: end\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── 系统终端模式 ──
    # 危险命令黑名单
    cmd_lower = cmd.lower()
    for b in _CMD_BLOCKED:
        if b in cmd_lower:
            return jsonify({"error": "命令被拒绝：检测到危险操作"}), 403

    # 处理 cd 命令
    if cmd_lower == "cd":
        # cd 无参数 → 显示当前目录
        def gen():
            yield f"data: [cwd: {sess['cwd']}]\n\n"
            yield "event: done\ndata: end\n\n"
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if cmd_lower.startswith("cd "):
        target = cmd[3:].strip().strip('"').strip("'")
        new_cwd = _resolve_cd_path(sess["cwd"], target)
        def gen():
            if new_cwd and Path(new_cwd).is_dir():
                sess["cwd"] = new_cwd
                yield f"data: [cwd: {new_cwd}]\n\n"
            else:
                yield f"data: 系统找不到指定的路径。: {target}\n\n"
            yield "event: done\ndata: end\n\n"
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 执行普通命令
    def generate():
        try:
            import locale
            encoding = locale.getpreferredencoding() or "utf-8"
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=sess["cwd"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding=encoding,
                errors="replace",
            )
            for line in iter(proc.stdout.readline, ""):
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()
            yield f"data: \n[退出码: {proc.returncode}]\n\n"
        except Exception as e:
            yield f"data: [错误: {e}]\n\n"
        yield "event: done\ndata: end\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _resolve_cd_path(cwd: str, target: str) -> str | None:
    """解析 cd 目标路径，返回绝对路径或 None。"""
    p = Path(target)
    if p.is_absolute():
        return str(p) if p.is_dir() else None
    # 处理 ..
    new = Path(cwd) / target
    try:
        resolved = new.resolve()
        return str(resolved) if resolved.is_dir() else None
    except Exception:
        return None


# ── 权限管理 ────────────────────────────────────────────────────────────────

@app.route("/admin/permissions")
@admin_required
def admin_permissions():
    """权限申请审批页面（与注册审批合并显示）。"""
    return redirect(url_for("admin_registrations"))


@app.route("/admin/permissions/<action>/<username>/<perm>", methods=["POST"])
@admin_required
def admin_permission_action(action, username, perm):
    """审批权限申请：approve 或 reject。"""
    if action == "approve":
        if approve_permission(username, perm):
            logger.log(session.get("username", "未知"), "批准权限", f"{username} → {perm}")
        else:
            return _index_redirect(msg="审批失败：申请不存在")
    elif action == "reject":
        if reject_permission(username, perm):
            logger.log(session.get("username", "未知"), "拒绝权限", f"{username} → {perm}")
        else:
            return _index_redirect(msg="审批失败：申请不存在")
    else:
        abort(400)
    return redirect(url_for("admin_permissions"))


@app.route("/admin/grant/<username>/<perm>", methods=["POST"])
@super_admin_required
def admin_grant(username, perm):
    """直接授予权限（仅 super_admin）。"""
    from auth import grant_permission
    if grant_permission(username, perm):
        logger.log(session.get("username", "未知"), "授予权限", f"{username} → {perm}")
    else:
        return _index_redirect(msg="授予失败")
    return redirect(url_for("admin_permissions"))


@app.route("/admin/revoke/<username>/<perm>", methods=["POST"])
@super_admin_required
def admin_revoke(username, perm):
    """撤销权限（仅 super_admin）。"""
    from auth import revoke_permission
    if revoke_permission(username, perm):
        logger.log(session.get("username", "未知"), "撤销权限", f"{username} → {perm}")
    else:
        return _index_redirect(msg="撤销失败")
    return redirect(url_for("admin_permissions"))


@app.route("/request-permission", methods=["POST"])
@login_required
def request_perm():
    """普通用户申请权限。"""
    perm = request.form.get("permission", "").strip()
    username = session.get("username", "")
    ok, msg = request_permission(username, perm)
    if ok:
        logger.log(username, "申请权限", perm)
    return _index_redirect(msg=msg)


# ── 主页路由 ──────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/folder/")
@app.route("/folder/<path:folder_path>")
@login_required
def index(folder_path=""):
    """文件管理主页。"""
    if folder_path and not storage.folder_exists(folder_path):
        return redirect(url_for("index", msg="文件夹不存在或已被删除"))
    files = storage.get_folder_contents(folder_path)
    tree = storage.get_folder_tree()
    return render_template(
        "index.html",
        files=files,
        tree=tree,
        current_path=folder_path,
        msg=request.args.get("msg"),
        username=session.get("username"),
    )


# ── 文件上传 ──────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
@permission_required("upload")
def upload():
    """上传文件或文件夹。"""
    folder_path = request.form.get("folder_path", "")
    if folder_path and not storage.folder_exists(folder_path):
        return _index_redirect(msg="目标文件夹不存在或已被删除")
    files = request.files.getlist("files")

    if not files or all(f.filename == "" for f in files):
        return _index_redirect(folder_path, msg="未选择任何文件")

    # 获取单文件大小限制
    max_file_size = get("upload", "max_file_size_mb", 500) * 1024 * 1024
    username = session.get("username", "未知")
    count = 0
    total_size = 0

    for f in files:
        if not f.filename:
            continue

        # 读取文件内容以检查大小
        content = f.read()
        if len(content) > max_file_size:
            logger.log(username, "上传失败（超过大小限制）", f.filename, len(content))
            continue
        f.stream.seek(0)

        # 从文件名中提取相对路径
        # webkitdirectory 上传时，浏览器在 filename 中包含路径（如 ".claude/CLAUDE.md"）
        raw_name = f.filename.replace("\\", "/")
        if "/" in raw_name:
            relative_path = raw_name
        else:
            relative_path = None

        file_id = storage.save_file(f, folder_path, relative_path)
        count += 1
        total_size += len(content)

        info = storage.get_file_info(file_id)
        if info:
            logger.log(username, "上传", info["path"], info["size"])

    msg = f"成功上传 {count} 个文件" if count > 0 else "上传失败"
    return _index_redirect(folder_path, msg=msg)


# ── 文件下载 ──────────────────────────────────────────────────────────────────

@app.route("/download/<file_id>")
@permission_required("download")
def download(file_id):
    """下载文件。"""
    info = storage.get_file_info(file_id)
    if not info or info.get("is_dir"):
        abort(404)
    abs_path = DATA_DIR / info["path"]
    if not abs_path.exists():
        return _index_redirect(msg="文件不存在或已被删除")
    logger.log(session.get("username", "未知"), "下载", info["path"], info["size"])
    abs_dir = str(DATA_DIR / Path(info["path"]).parent)
    return send_from_directory(abs_dir, info["name"], as_attachment=True)


# ── 文件删除 ──────────────────────────────────────────────────────────────────

@app.route("/delete/<file_id>", methods=["POST"])
@permission_required("upload")
def delete(file_id):
    """删除单个文件或空文件夹。"""
    folder_path = request.form.get("folder_path", "")
    success, msg, info = storage.delete_file(file_id)
    if success and info:
        logger.log(session.get("username", "未知"), "删除", info["path"], info.get("size", 0))
    # 如果删除的是当前正在浏览的文件夹，跳转到上级
    if success and info and info.get("is_dir") and folder_path == info["path"]:
        parent = str(Path(folder_path).parent) if "/" in folder_path else ""
        return _index_redirect(parent, msg=msg)
    return _index_redirect(folder_path, msg=msg)


@app.route("/batch-delete", methods=["POST"])
@permission_required("upload")
def batch_delete():
    """批量删除文件。"""
    file_ids = request.form.getlist("file_ids")
    current_path = request.form.get("current_path", "")
    username = session.get("username", "未知")
    deleted = 0
    current_folder_deleted = False
    for fid in file_ids:
        success, _, info = storage.delete_file(fid)
        if success and info:
            deleted += 1
            logger.log(username, "删除", info["path"], info.get("size", 0))
            if info.get("is_dir") and info["path"] == current_path:
                current_folder_deleted = True
    msg = f"已删除 {deleted} 个项目" if deleted > 0 else "删除失败"
    if current_folder_deleted:
        parent = str(Path(current_path).parent) if "/" in current_path else ""
        return _index_redirect(parent, msg=msg)
    return _index_redirect(current_path, msg=msg)


# ── 文件移动 ──────────────────────────────────────────────────────────────────

@app.route("/move", methods=["POST"])
@permission_required("upload")
def move():
    """移动文件到指定文件夹。"""
    file_ids = request.form.getlist("file_ids")
    dest_folder = request.form.get("dest_folder", "")
    current_path = request.form.get("current_path", "")
    if dest_folder and not storage.folder_exists(dest_folder):
        return _index_redirect(current_path, msg="目标文件夹不存在")
    username = session.get("username", "未知")

    moved = 0
    current_folder_moved = False
    for fid in file_ids:
        # 移动前记录原始路径
        info_before = storage.get_file_info(fid)
        success, msg = storage.move_file(fid, dest_folder)
        if success:
            moved += 1
            info = storage.get_file_info(fid)
            if info:
                logger.log(username, "移动", info["path"], info.get("size", 0))
            # 检查是否移动了当前浏览的文件夹
            if info_before and info_before.get("is_dir") and info_before["path"] == current_path:
                current_folder_moved = True

    msg = f"已移动 {moved} 个项目" if moved > 0 else "移动失败"
    if current_folder_moved:
        parent = str(Path(current_path).parent) if "/" in current_path else ""
        return _index_redirect(parent, msg=msg)
    return _index_redirect(current_path, msg=msg)


# ── 创建文件夹 ────────────────────────────────────────────────────────────────

@app.route("/mkdir", methods=["POST"])
@permission_required("upload")
def mkdir():
    """创建新文件夹。"""
    parent_path = request.form.get("parent_path", "")
    folder_name = request.form.get("folder_name", "").strip()
    if parent_path and not storage.folder_exists(parent_path):
        return _index_redirect(msg="上级文件夹不存在或已被删除")
    success, msg = storage.create_folder(parent_path, folder_name)
    if success:
        logger.log(session.get("username", "未知"), "创建文件夹", folder_name)
    return _index_redirect(parent_path, msg=msg)


# ── 设置页面 ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """账户设置页面 — 修改当前用户密码。"""
    msg = None
    if request.method == "POST":
        old_pass = request.form.get("old_password", "")
        new_pass = request.form.get("new_password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()

        if not check_credentials(session.get("username", ""), old_pass):
            msg = "当前密码错误"
        elif not new_pass:
            msg = "新密码不能为空"
        elif new_pass != confirm:
            msg = "两次输入的密码不一致"
        elif len(new_pass) < 6:
            msg = "密码长度至少 6 位"
        else:
            change_password(session.get("username"), new_pass)
            logger.log(session.get("username", "未知"), "修改密码")
            session.clear()
            return redirect(url_for("login", msg="密码已修改，请重新登录"))

    return render_template("settings.html", msg=msg, username=session.get("username"), tree=storage.get_folder_tree(), current_path="")


# ── 日志页面 ──────────────────────────────────────────────────────────────────

@app.route("/logs")
@login_required
def logs():
    """操作日志页面。"""
    log_lines = logger.get_recent_logs(100)
    return render_template("logs.html", logs=log_lines, username=session.get("username"), tree=storage.get_folder_tree(), current_path="")


@app.route("/logs/clear", methods=["POST"])
@login_required
def clear_logs():
    """清空日志。"""
    logger.clear_logs()
    return redirect(url_for("logs", msg="日志已清空"))


# ── 文件变化检测 ──────────────────────────────────────────────────────────────

@app.route("/api/files-version")
@login_required
def files_version():
    """返回当前文件索引的版本号（基于修改时间），供前端轮询检测变化。"""
    index_file = DATA_DIR / ".index.json"
    if index_file.exists():
        return jsonify({"version": int(index_file.stat().st_mtime * 1000)})
    return jsonify({"version": 0})


# ── 站内消息 ──────────────────────────────────────────────────────────────────

@app.route("/api/messages")
@login_required
def get_messages():
    """获取新消息（轮询接口）。传 ?last_id=N 只获取 N 之后的消息。"""
    last_id = request.args.get("last_id", 0, type=int)
    messages = messenger.get_since(last_id)
    return jsonify(messages)


@app.route("/api/messages/send", methods=["POST"])
@login_required
def send_message():
    """发送消息。"""
    content = request.form.get("content", "").strip()
    if not content:
        return jsonify({"error": "消息不能为空"}), 400
    username = session.get("username", "未知")
    msg = messenger.send(username, content)
    logger.log(username, "发送消息", content[:50])
    return jsonify(msg)


# ── 公网 IP 获取 ──────────────────────────────────────────────────────────────

def get_public_ip() -> str | None:
    """获取本机公网 IP 地址。"""
    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CloudDrive/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode().strip()
        except Exception:
            continue
    return None


# ── 服务端控制台发消息 ────────────────────────────────────────────────────────

def _console_input():
    """后台线程：监听控制台输入，发送系统消息或执行管理命令。"""
    print("  提示: 输入消息后回车，可从服务端发送消息到所有客户端")
    print("  命令: /quit /online /kick /approve /reject /grant /revoke")
    print("        /read config    重载用户/权限/邀请码等配置")
    print("        /read server    重载服务器配置（端口/隧道等）")
    print("        /restart server 重启整个服务")
    print("-" * 55)

    _pending_confirm = {}  # {命令: 截止时间}，超时自动清除

    while True:
        try:
            line = input().strip()
            if not line:
                continue

            # ── 退出（双步确认） ──
            if line.lower() == "/quit":
                if _pending_confirm.get("/quit", 0) > _time.time():
                    _pending_confirm.pop("/quit", None)
                    print("正在关闭服务...")
                    os._exit(0)
                else:
                    _pending_confirm["/quit"] = _time.time() + 30
                    print("  ⚠ 确认退出？30 秒内再次输入 /quit 确认")

            # ── 查看在线用户 ──
            elif line.lower() == "/online":
                users = get_online_users()
                if not users:
                    print("  当前无在线用户")
                else:
                    print(f"  在线用户 ({len(users)}):")
                    for u in users:
                        print(f"    {u['username']}  IP:{u['ip']}  {u['last_active']}  角色:{u['role']}")

            # ── 踢出用户 ──
            elif line.lower().startswith("/kick "):
                target = line[6:].strip()
                if target in _online_users:
                    _kicked_users.add(target)
                    _online_users.pop(target, None)
                    print(f"  已踢出用户: {target}")
                    logger.log("控制台", "强制下线", target)
                else:
                    print(f"  用户 {target} 不在线")

            # ── 批准注册 ──
            elif line.lower().startswith("/approve "):
                target = line[9:].strip()
                if approve_user(target):
                    print(f"  已批准注册: {target}")
                    logger.log("控制台", "批准注册", target)
                else:
                    print(f"  未找到待审批用户: {target}")

            # ── 拒绝注册 ──
            elif line.lower().startswith("/reject "):
                target = line[8:].strip()
                if reject_user(target):
                    print(f"  已拒绝注册: {target}")
                    logger.log("控制台", "拒绝注册", target)
                else:
                    print(f"  未找到待审批用户: {target}")

            # ── 授予权限 ──
            elif line.lower().startswith("/grant "):
                parts = line[7:].strip().split()
                if len(parts) == 2:
                    target, perm = parts
                    from auth import grant_permission
                    if grant_permission(target, perm):
                        print(f"  已授予 {target} 权限: {perm}")
                        logger.log("控制台", "授予权限", f"{target} → {perm}")
                    else:
                        print(f"  授予失败（用户不存在或权限类型无效）")
                else:
                    print("  用法: /grant <用户名> <download|upload>")

            # ── 撤销权限 ──
            elif line.lower().startswith("/revoke "):
                parts = line[8:].strip().split()
                if len(parts) == 2:
                    target, perm = parts
                    from auth import revoke_permission
                    if revoke_permission(target, perm):
                        print(f"  已撤销 {target} 权限: {perm}")
                        logger.log("控制台", "撤销权限", f"{target} → {perm}")
                    else:
                        print(f"  撤销失败")
                else:
                    print("  用法: /revoke <用户名> <download|upload>")

            # ── /read config: 重载非服务端配置 ──
            elif line.lower() == "/read config":
                try:
                    reload_config()
                    print("  配置已重新加载（用户、权限、邀请码、上传限制等）")
                except Exception as e:
                    print(f"  重载失败: {e}")

            # ── /read server: 重载服务器配置 ──
            elif line.lower() == "/read server":
                try:
                    reload_config()
                    if get("self_domain", "enabled", False):
                        print(f"  服务器配置已重新加载（自定义域名: {get('self_domain', 'domain', '')}）")
                    else:
                        print("  服务器配置已重新加载")
                    print("  提示: 快速隧道 URL 不变，修改端口需 /restart server")
                except Exception as e:
                    print(f"  重载失败: {e}")

            # ── /restart server: 重启整个服务（双步确认） ──
            elif line.lower() == "/restart server":
                if _pending_confirm.get("/restart server", 0) > _time.time():
                    _pending_confirm.pop("/restart server", None)
                    print("  正在重启服务 ...")
                    try:
                        reload_config()
                        subprocess.Popen([sys.executable] + sys.argv)
                        os._exit(0)
                    except Exception as e:
                        print(f"  重启失败: {e}")
                else:
                    _pending_confirm["/restart server"] = _time.time() + 30
                    print("  ⚠ 确认重启？30 秒内再次输入 /restart server 确认")

            # ── 普通消息 ──
            else:
                messenger.send("服务器", line, is_system=True)
                print(f"  [已发送] {line}")

        except (EOFError, KeyboardInterrupt):
            break
        except Exception as e:
            print(f"  错误: {e}")


def _start_cloudflared(port: int) -> str | None:
    """启动 cloudflared 快速隧道，返回公网 URL。输出写入日志文件。自动重试 3 次。"""
    import subprocess
    import shutil
    import re

    LOG_DIR = Path(__file__).parent / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / "cloudflared.log"

    if shutil.which("cloudflared"):
        cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
    else:
        cmd = ["npx", "cloudflared", "tunnel", "--url", f"http://localhost:{port}"]

    for attempt in range(1, 4):
        print(f"  第 {attempt} 次尝试启动隧道 ...")

        try:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(f"\n=== 尝试 {attempt} ===\n")
                proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.PIPE, text=True, shell=True)
        except FileNotFoundError:
            return None

        url = None
        deadline = _time.time() + 45

        while _time.time() < deadline:
            line = proc.stderr.readline()
            if not line:
                if proc.poll() is not None:
                    break
                _time.sleep(0.5)
                continue
            if "trycloudflare.com" in line:
                match = re.search(r"https://[\w.-]+\.trycloudflare\.com", line)
                if match:
                    url = match.group()
                    break

        if url:
            def _drain(pipe):
                try:
                    with open(log_file, "a", encoding="utf-8") as lf:
                        while True:
                            l = pipe.readline()
                            if not l:
                                break
                            lf.write(l)
                except Exception:
                    pass
            threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
            return url

        try:
            proc.terminate()
        except Exception:
            pass
        if attempt < 3:
            print(f"  第 {attempt} 次失败，2 秒后重试 ...")
            _time.sleep(2)

    return None


# ── 终端 WebSocket ──────────────────────────────────────────────────────────────

_term_sessions: dict[str, dict] = {}
_term_lock = threading.Lock()
_TERM_MAX_HISTORY = 200
_DSR_RE = __import__("re").compile(r"\x1b\[\??\d*(?:;\d+)*[Rc]")


def _term_clean(data: str) -> str:
    return _DSR_RE.sub("", data)


def _term_get_or_create(sid: str | None) -> tuple[str, PTY, list[str]]:
    import uuid as _uuid
    with _term_lock:
        if sid and sid in _term_sessions:
            sess = _term_sessions[sid]
            return sid, sess["pty"], list(sess["history"])
        new_sid = sid or _uuid.uuid4().hex[:12]
        pty = PTY(120, 40)
        pty.spawn(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoLogo -NoProfile",
            cwd=str(Path(__file__).parent.parent),
        )
        _term_sessions[new_sid] = {"pty": pty, "history": [], "last_active": _time.time()}
        return new_sid, pty, []


def _term_record(sid: str, data: str):
    with _term_lock:
        if sid in _term_sessions:
            h = _term_sessions[sid]["history"]
            h.append(data)
            if len(h) > _TERM_MAX_HISTORY:
                _term_sessions[sid]["history"] = h[-_TERM_MAX_HISTORY:]
            _term_sessions[sid]["last_active"] = _time.time()


@app.route("/terminal-page")
@login_required
def terminal_page():
    """终端页面（iframe 内嵌，连接 /terminal-ws）。"""
    if not has_permission(session.get("username", ""), "terminal"):
        return "Forbidden", 403
    return render_template("terminal_page.html")


@sock.route("/terminal-ws")
def terminal_ws(ws):
    """WebSocket 终端端点（同端口，走主服务隧道）。"""
    username = session.get("username", "")
    if not session.get("logged_in"):
        ws.close()
        return
    if not get("terminal", "enabled", True):
        ws.close()
        return
    if not has_permission(username, "terminal"):
        ws.close()
        return

    sid_arg = request.args.get("session")
    try:
        sid, pty, history = _term_get_or_create(sid_arg)
    except Exception as e:
        print(f"[terminal-ws] PTY 创建失败: {e}")
        ws.close()
        return

    # 发送初始数据
    try:
        ws.send(_json.dumps({"type": "session", "session_id": sid}))
        for chunk in history:
            ws.send(chunk)
        ws.send(_json.dumps({"type": "replay_done"}))
    except Exception:
        ws.close()
        return

    # 用 threading.Event 实现 PTY→WebSocket 的转发
    stop_event = threading.Event()

    def pty_to_ws():
        """后台线程：持续读取 PTY 输出并发送到 WebSocket。"""
        while not stop_event.is_set():
            try:
                data = pty.read(blocking=False)
                if data:
                    cleaned = _term_clean(data)
                    if cleaned.strip():
                        _term_record(sid, cleaned)
                        try:
                            ws.send(cleaned)
                        except Exception:
                            break
                else:
                    _time.sleep(0.02)
            except Exception:
                break
        stop_event.set()

    reader = threading.Thread(target=pty_to_ws, daemon=True)
    reader.start()

    last_active = _time.time()

    # 主循环：接收客户端消息
    try:
        while not stop_event.is_set():
            try:
                msg = ws.receive(timeout=0.1)
            except Exception:
                break
            if msg:
                last_active = _time.time()
                try:
                    data = _json.loads(msg)
                    msg_type = data.get("type", "")
                    if msg_type == "resize":
                        pty.set_size(data["cols"], data["rows"])
                    elif msg_type == "input" and pty:
                        pty.write(data["data"])
                    elif msg_type == "ping":
                        pass
                except (_json.JSONDecodeError, KeyError):
                    pass
            # 闲置超时
            timeout_sec = get("terminal", "timeout_minutes", 40) * 60
            if _time.time() - last_active > timeout_sec:
                try:
                    ws.send("\r\n\x1b[33m[闲置 40 分钟，连接已断开]\x1b[0m")
                except Exception:
                    pass
                break
            # 读取线程已退出
            if not reader.is_alive():
                break
    except Exception as e:
        print(f"[terminal-ws] 异常: {e}")
    finally:
        stop_event.set()


# ── 内网 IP 检测 ────────────────────────────────────────────────────────────────

def _get_innet_ip() -> str:
    """获取本机局域网 IP 地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = get("server", "host", "0.0.0.0")
    port = get("server", "port", 8080)
    debug = get("server", "debug", False)
    users = get("auth", "users") or []

    # 内网地址
    innet_ip = _get_innet_ip()
    innet_url = f"http://{innet_ip}:{port}"
    config_update("server", "innet_ip_url", innet_url)

    print("=" * 55)
    print(f"  Cloud Drive 启动于 http://{host}:{port}")
    print(f"  内网访问: {innet_url}")

    # 公网访问
    self_domain_enabled = get("self_domain", "enabled", False)
    self_domain = get("self_domain", "domain", "")
    expose_lan = get("expose_to_local_area_network", "enabled", False)

    if self_domain_enabled and self_domain:
        public_url = f"https://{self_domain}"
        config_update("server", "public_url", public_url)
        print(f"  公网访问: {public_url}（自定义域名，隧道由系统服务管理）")
    elif expose_lan:
        print("  正在启动快速隧道 ...")
        tunnel_url = _start_cloudflared(port)
        if tunnel_url:
            config_update("server", "public_url", tunnel_url)
            print(f"  公网访问: {tunnel_url}")
        else:
            config_update("server", "public_url", None)
            print("  公网访问: 隧道启动失败")
    else:
        config_update("server", "public_url", None)
        print("  公网访问: 未开启（仅内网访问）")

    terminal_on = get("terminal", "enabled", True)
    print(f"  终端服务: {'已启用（/terminal-ws）' if terminal_on else '未启用'}")

    if users:
        print(f"  账户: {'、'.join(u['username'] for u in users)} ({len(users)} 个用户)")
    else:
        print("  警告: 未配置任何用户！请在 config.json 的 auth.users 中添加用户")
    print("=" * 55)

    # 启动控制台消息监听线程
    threading.Thread(target=_console_input, daemon=True).start()

    app.run(host=host, port=port, debug=debug, use_reloader=False)
