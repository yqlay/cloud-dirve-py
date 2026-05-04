"""站内消息通信模块。"""

import json
from datetime import datetime
from pathlib import Path

MSG_FILE = Path(__file__).parent / "data" / ".messages.json"


class Messenger:
    """站内消息管理器，支持服务端和 UI 双向发消息。"""

    def __init__(self):
        self._messages: list[dict] = []
        self._load()

    def _load(self) -> None:
        """从磁盘加载消息历史。"""
        if MSG_FILE.exists():
            try:
                with open(MSG_FILE, "r", encoding="utf-8") as f:
                    self._messages = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._messages = []

    def _save(self) -> None:
        """持久化消息到磁盘。"""
        MSG_FILE.parent.mkdir(exist_ok=True)
        with open(MSG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._messages, f, indent=2, ensure_ascii=False)

    def send(self, username: str, content: str, is_system: bool = False) -> dict:
        """发送一条消息。返回消息对象。"""
        msg = {
            "id": len(self._messages) + 1,
            "user": username,
            "content": content.strip(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_system": is_system,
        }
        self._messages.append(msg)
        # 只保留最近 500 条
        if len(self._messages) > 500:
            self._messages = self._messages[-500:]
        self._save()
        return msg

    def get_since(self, last_id: int = 0) -> list[dict]:
        """获取指定 ID 之后的所有消息（用于轮询）。"""
        return [m for m in self._messages if m["id"] > last_id]

    def get_recent(self, limit: int = 50) -> list[dict]:
        """获取最近 N 条消息。"""
        return self._messages[-limit:]
