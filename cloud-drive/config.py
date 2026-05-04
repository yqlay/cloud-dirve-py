"""配置管理模块。"""

import json
import os
import re
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"

# 默认配置
_DEFAULTS = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "debug": False,
        "secret_key": None,
        "public_url": None,
    },
    "self_domain": {
        "enabled": False,
        "domain": "",
    },
    "expose_to_local_area_network": {
        "enabled": False,
    },
    "terminal": {
        "enabled": True,
        "timeout_minutes": 40,
    },
    "upload": {
        "max_file_size_mb": 5120,
        "max_folder_size_mb": 5120,
    },
    "auth": {
        "users": [],
        "register_message_max_length": 128,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 覆盖 base。"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _eval_json_expressions(text: str) -> str:
    """预处理 JSON 中的数学表达式（如 5*1024 → 5120）。"""
    def _replace_expr(match):
        try:
            return str(eval(match.group()))
        except Exception:
            return match.group()
    # 匹配 JSON 值中的简单数学表达式（数字 运算符 数字）
    return re.sub(r'\b(\d+[\s]*[+\-*/][\s]*\d+)\b', _replace_expr, text)


def _load_config() -> dict:
    """加载配置文件，不存在则用默认值创建。支持 JSON 中的简单数学表达式。"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        # 先尝试直接解析，失败则预处理表达式后重试
        try:
            user_config = json.loads(raw)
        except json.JSONDecodeError:
            user_config = json.loads(_eval_json_expressions(raw))
        return _deep_merge(_DEFAULTS, user_config)
    # 首次运行，写入默认配置
    _save_config(_DEFAULTS)
    return _DEFAULTS.copy()


def _save_config(config: dict) -> None:
    """保存配置到文件。"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# 全局配置实例
_config = _load_config()


def reload() -> None:
    """从磁盘重新加载配置文件。"""
    global _config
    _config = _load_config()


def get(section: str, key: str, default=None):
    """获取配置值。每次从磁盘读取，确保最新。"""
    cfg = _load_config()
    return cfg.get(section, {}).get(key, default)


def get_section(section: str) -> dict:
    """获取整个配置段。"""
    cfg = _load_config()
    return cfg.get(section, {}).copy()


def update(section: str, key: str, value) -> None:
    """更新配置并保存到文件。"""
    if section not in _config:
        _config[section] = {}
    _config[section][key] = value
    _save_config(_config)
