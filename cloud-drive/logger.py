"""操作日志记录模块。"""

import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "access.log"


class Logger:
    """操作日志记录器，同时写入文件和控制台。"""

    def __init__(self):
        LOG_DIR.mkdir(exist_ok=True)

    def log(self, username: str, action: str, filename: str = "", size: int = 0) -> None:
        """记录一条操作日志。

        Args:
            username: 操作用户
            action: 操作类型（上传/下载/删除/移动/创建文件夹）
            filename: 相关文件名
            size: 文件大小（字节）
        """
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        size_str = format_size(size) if size > 0 else "—"

        line = f"[{timestamp}] 用户: {username} | 操作: {action} | 文件: {filename or '—'} | 大小: {size_str}"
        print(line, file=sys.stdout)

        # 写入日志文件（按天分文件）
        day_file = LOG_DIR / f"{now.strftime('%Y-%m-%d')}.log"
        with open(day_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # 同时追加到总日志
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def get_recent_logs(self, limit: int = 50) -> list[str]:
        """获取最近的日志记录。"""
        if not LOG_FILE.exists():
            return []
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [line.strip() for line in lines[-limit:] if line.strip()]

    def clear_logs(self) -> None:
        """清空日志文件。"""
        if LOG_FILE.exists():
            LOG_FILE.write_text("", encoding="utf-8")


def format_size(size_bytes: int) -> str:
    """将字节数格式化为可读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
