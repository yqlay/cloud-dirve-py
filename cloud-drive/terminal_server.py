"""WebSocket 终端服务 — 基于 pywinpty + aiohttp 的 Windows PTY 终端服务器。"""

import asyncio
import json
import re
import threading
import time

from aiohttp import web
from winpty import PTY

# ── 配置 ──
PORT = 7681

# ── 会话管理 ──
_sessions: dict[str, dict] = {}  # session_id → {pty, history, last_active}
_session_lock = threading.Lock()
MAX_HISTORY = 200  # 最多保存 200 条输出用于回放


# ── xterm.js HTML 页面 ──
HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Cloud Drive Terminal</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<style>
  html, body { margin:0; padding:0; height:100%; background:#0c0e14; overflow:hidden; }
  #terminal { height:100%; }
</style>
</head>
<body>
<div id="terminal"></div>
<script>
const term = new Terminal({
  theme: {
    background: '#0c0e14',
    foreground: '#a8b0c0',
    cursor: '#7ec8e3',
    selectionBackground: '#264f78',
  },
  fontFamily: "'Cascadia Code','Fira Code','JetBrains Mono',monospace",
  fontSize: 14,
  cursorBlink: true,
});
const fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(document.getElementById('terminal'));
setTimeout(() => fitAddon.fit(), 100);

const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
const hashParams = new URLSearchParams(location.hash.substring(1));
const sessionId = hashParams.get('session') || '';
const wsUrl = sessionId
  ? `${proto}//${location.host}/ws?session=${sessionId}`
  : `${proto}//${location.host}/ws`;
const ws = new WebSocket(wsUrl);

ws.onopen = () => {
  ws.send(JSON.stringify({type: 'resize', cols: term.cols, rows: term.rows}));
};

ws.onmessage = (e) => {
  const data = e.data;
  if (data.startsWith('{')) {
    try {
      const msg = JSON.parse(data);
      if (msg.type === 'session') {
        hashParams.set('session', msg.session_id);
        location.hash = hashParams.toString();
        try { parent.postMessage({type: 'session', session_id: msg.session_id}, '*'); } catch(_){}
        return;
      }
      if (msg.type === 'replay_done') return;
    } catch(_) {}
  }
  term.write(data);
  try { parent.postMessage({type: 'output', data: data}, '*'); } catch(_){}
};

// 每 30 秒发一次心跳，防止连接被中间层断开
setInterval(() => {
  if (ws.readyState === 1) ws.send(JSON.stringify({type: 'ping'}));
}, 30000);

ws.onclose = () => {
  term.write('\r\n\x1b[31m[连接已断开]\x1b[0m');
};

term.onData((data) => {
  if (ws.readyState === 1) ws.send(JSON.stringify({type: 'input', data: data}));
});

term.onResize(({cols, rows}) => {
  if (ws.readyState === 1) ws.send(JSON.stringify({type: 'resize', cols: cols, rows: rows}));
});

// 监听父页面 postMessage（前端输入框发送命令）
window.addEventListener('message', (e) => {
  if (e.data?.type === 'input' && ws.readyState === 1) {
    ws.send(JSON.stringify({type: 'input', data: e.data.data}));
  }
});

// 容器大小变化时自适应
const resizeObserver = new ResizeObserver(() => fitAddon.fit());
resizeObserver.observe(document.getElementById('terminal'));
window.addEventListener('resize', () => fitAddon.fit());
</script>
</body>
</html>"""


async def handle_index(request):
    """返回终端 HTML 页面。"""
    return web.Response(text=HTML_PAGE, content_type="text/html")


# 只过滤设备状态响应（DSR/DA 回复），不动光标/颜色等序列
_DSR_RE = re.compile(r"\x1b\[\??\d*(?:;\d+)*[Rc]")

import logging
logging.basicConfig(
    filename="terminal_server.log", level=logging.INFO,
    format="%(asctime)s %(message)s", encoding="utf-8",
)
_log = logging.getLogger("terminal").info


def _clean(data: str) -> str:
    """去除 ConPTY 设备状态响应，避免终端出现多余空格。"""
    return _DSR_RE.sub("", data)


IDLE_TIMEOUT = 40 * 60  # 40 分钟无操作断开
SESSION_TTL = 2 * 3600  # 2 小时无活动的会话自动清理


def _get_or_create_session(session_id: str | None) -> tuple[str, PTY, list[str]]:
    """获取已有会话或创建新会话。返回 (session_id, pty, history)。"""
    with _session_lock:
        if session_id and session_id in _sessions:
            sess = _sessions[session_id]
            _log("reconnected session: %s", session_id)
            return session_id, sess["pty"], list(sess["history"])

        # 创建新会话
        import uuid
        sid = session_id or uuid.uuid4().hex[:12]
        cols, rows = 120, 40
        pty = PTY(cols, rows)
        # 用 PowerShell 避免 cmd.exe 的 Conda AutoRun hook 干扰
        pty.spawn(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoLogo -NoProfile",
            cwd=r"D:\web",
        )
        _sessions[sid] = {
            "pty": pty,
            "history": [],  # 输出历史，用于回放
            "last_active": time.time(),
        }
        _log("created session: %s", sid)
        return sid, pty, []


def _record_output(session_id: str, data: str):
    """记录输出到会话历史。"""
    with _session_lock:
        if session_id in _sessions:
            hist = _sessions[session_id]["history"]
            hist.append(data)
            if len(hist) > MAX_HISTORY:
                _sessions[session_id]["history"] = hist[-MAX_HISTORY:]
            _sessions[session_id]["last_active"] = time.time()


async def handle_ws(request):
    """处理 WebSocket 终端连接。"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _log("ws connected: %s", request.remote)

    # 获取 session_id（从 query 参数）
    session_id = request.query.get("session")
    sid, pty, history = _get_or_create_session(session_id)

    alive = True
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()
    last_active = loop.time()

    # 发送 session_id 给客户端
    await ws.send_str(json.dumps({"type": "session", "session_id": sid}))

    # 回放历史输出
    if history:
        for chunk in history:
            try:
                await ws.send_str(chunk)
            except Exception:
                break
        await ws.send_str(json.dumps({"type": "replay_done"}))

    def blocking_read():
        """在独立线程中轮询读取 PTY。非阻塞避免闲置后卡死。"""
        nonlocal alive
        while alive and pty:
            try:
                data = pty.read(blocking=False)
                if data:
                    cleaned = _clean(data)
                    if cleaned.strip():
                        _record_output(sid, cleaned)
                        loop.call_soon_threadsafe(queue.put_nowait, cleaned)
                else:
                    time.sleep(0.01)  # 10ms 轮询
            except Exception as e:
                _log("read error: %s", e)
                break
        _log("read thread exited, alive=%s", alive)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=blocking_read, daemon=True).start()

    async def read_pty():
        """从队列读取 PTY 输出并发送到 WebSocket。"""
        nonlocal alive, last_active
        while alive and not ws.closed:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if data is None:
                break
            last_active = loop.time()
            try:
                await ws.send_str(data)
            except Exception:
                break
        alive = False

    async def write_pty():
        """从 WebSocket 读取输入并写入 PTY。"""
        nonlocal alive, last_active
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data["type"] == "resize":
                        cols, rows = data["cols"], data["rows"]
                        pty.set_size(cols, rows)
                    elif data["type"] == "input" and pty:
                        last_active = loop.time()
                        pty.write(data["data"])
                    elif data["type"] == "ping":
                        last_active = loop.time()
                except (json.JSONDecodeError, KeyError):
                    pass
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
        alive = False

    async def idle_watchdog():
        """闲置超时断开。"""
        nonlocal alive
        while alive and not ws.closed:
            await asyncio.sleep(30)
            if loop.time() - last_active > IDLE_TIMEOUT:
                try:
                    await ws.send_str("\r\n\x1b[33m[闲置 40 分钟，连接已断开]\x1b[0m")
                    await ws.close()
                except Exception:
                    pass
                alive = False
                break

    read_task = asyncio.create_task(read_pty())
    write_task = asyncio.create_task(write_pty())
    idle_task = asyncio.create_task(idle_watchdog())

    done, pending = await asyncio.wait(
        [read_task, write_task, idle_task], return_when=asyncio.FIRST_COMPLETED
    )
    alive = False
    for task in pending:
        task.cancel()
    _log("ws disconnected: %s (session=%s)", request.remote, sid)

    return ws


async def _cleanup_stale_sessions():
    """定期清理长时间无活动的会话。"""
    while True:
        await asyncio.sleep(300)  # 每 5 分钟检查一次
        now = time.time()
        with _session_lock:
            stale = [sid for sid, s in _sessions.items()
                     if now - s["last_active"] > SESSION_TTL]
            for sid in stale:
                sess = _sessions.pop(sid)
                try:
                    sess["pty"].close()
                except Exception:
                    pass
                _log("cleaned stale session: %s", sid)


async def _on_startup(app):
    asyncio.create_task(_cleanup_stale_sessions())


def main():
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    print(f"  终端服务已启动: http://localhost:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda _: None)


if __name__ == "__main__":
    main()
