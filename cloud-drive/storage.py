"""文件与文件夹存储管理模块。"""

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path


def format_size(size_bytes: int) -> str:
    """将字节数格式化为可读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


class Storage:
    """文件存储管理器。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.index_file = data_dir / ".index.json"
        self.data_dir.mkdir(exist_ok=True)
        self._index: dict[str, dict] = {}
        self._load_index()

    # ── 路径安全 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_path(path: str) -> str:
        """清理路径，防止路径遍历攻击。"""
        if not path:
            return ""
        # 统一分隔符，去除前后斜杠
        path = path.replace("\\", "/").strip("/")
        # 拆分并过滤掉 .. 和空段
        parts = [p for p in path.split("/") if p and p != ".."]
        return "/".join(parts)

    # ── 索引管理 ──────────────────────────────────────────────────────────────

    def _load_index(self) -> None:
        """从磁盘加载文件索引。"""
        if self.index_file.exists():
            with open(self.index_file, "r", encoding="utf-8") as f:
                self._index = json.load(f)
        # 清理磁盘上已不存在的条目，补充索引中缺失的文件
        self._sync_index()
        self._scan_directory(self.data_dir, "")

    def _sync_index(self) -> None:
        """同步索引与磁盘：移除磁盘上已不存在的文件条目，以及隐藏文件。"""
        stale_ids = []
        for fid, info in self._index.items():
            name = info["path"].split("/")[-1]
            if name.startswith(".") or name == ".index.json":
                stale_ids.append(fid)
                continue
            abs_path = self.data_dir / info["path"]
            if not abs_path.exists():
                stale_ids.append(fid)
            elif info.get("is_dir") and not abs_path.is_dir():
                stale_ids.append(fid)
            elif not info.get("is_dir") and not abs_path.is_file():
                stale_ids.append(fid)
        for fid in stale_ids:
            del self._index[fid]
        if stale_ids:
            self._save_index()

    def _save_index(self) -> None:
        """保存索引到磁盘。"""
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    def _scan_directory(self, dir_path: Path, rel_prefix: str) -> None:
        """递归扫描目录，补充索引中缺失的文件和文件夹。"""
        if not dir_path.exists():
            return
        for item in dir_path.iterdir():
            rel_path = f"{rel_prefix}/{item.name}" if rel_prefix else item.name
            if item.name.startswith(".") or item.name == ".index.json":
                continue
            if not any(info["path"] == rel_path for info in self._index.values()):
                if item.is_file():
                    fid = uuid.uuid4().hex[:8]
                    self._index[fid] = {
                        "name": item.name,
                        "path": rel_path,
                        "size": item.stat().st_size,
                        "is_dir": False,
                        "created": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                    }
                elif item.is_dir():
                    fid = uuid.uuid4().hex[:8]
                    self._index[fid] = {
                        "name": item.name,
                        "path": rel_path,
                        "size": 0,
                        "is_dir": True,
                        "created": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                    }
                    self._scan_directory(item, rel_path)

    # ── 文件夹操作 ────────────────────────────────────────────────────────────

    def get_folder_contents(self, folder_path: str = "") -> list[dict]:
        """获取指定文件夹下的文件和子文件夹列表。每次访问前同步磁盘状态。"""
        self._sync_index()
        self._scan_directory(self.data_dir, "")
        result = []
        for fid, info in self._index.items():
            item_path = info["path"]

            if folder_path:
                # 子文件夹：路径必须以 "folder_path/" 开头
                prefix = folder_path + "/"
                if not item_path.startswith(prefix):
                    continue
                remainder = item_path[len(prefix):]
            else:
                # 根目录：取完整路径作为 remainder
                remainder = item_path

            if info.get("is_dir"):
                # 文件夹：只显示当前层级的直接子文件夹
                if remainder == info["name"]:
                    result.append({**info, "id": fid, "size_fmt": "—"})
            else:
                # 文件：remainder 中不能有 "/"，否则属于子文件夹
                if "/" not in remainder:
                    result.append({**info, "id": fid, "size_fmt": format_size(info["size"])})

        result.sort(key=lambda x: (not x.get("is_dir", False), x["name"].lower()))
        return result

    def create_folder(self, parent_path: str, folder_name: str) -> tuple[bool, str]:
        """创建文件夹。返回 (成功, 消息)。"""
        parent_path = self._sanitize_path(parent_path)
        # 清理文件夹名
        folder_name = folder_name.strip().strip("/").replace("\\", "/")
        if not folder_name or "/" in folder_name or ".." in folder_name:
            return False, "无效的文件夹名称"

        rel_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
        abs_path = self.data_dir / rel_path

        if abs_path.exists():
            return False, f"文件夹 '{folder_name}' 已存在"

        # 检查索引中是否已存在
        if any(info["path"] == rel_path and info.get("is_dir") for info in self._index.values()):
            return False, f"文件夹 '{folder_name}' 已存在"

        abs_path.mkdir(parents=True, exist_ok=True)
        fid = uuid.uuid4().hex[:8]
        self._index[fid] = {
            "name": folder_name,
            "path": rel_path,
            "size": 0,
            "is_dir": True,
            "created": datetime.now().isoformat(),
        }
        self._save_index()
        return True, f"文件夹 '{folder_name}' 创建成功"

    def get_folder_tree(self) -> list[dict]:
        """获取完整的文件夹树结构，用于侧边栏和移动对话框。"""
        self._sync_index()
        self._scan_directory(self.data_dir, "")
        folders = []
        for info in self._index.values():
            if info.get("is_dir"):
                folders.append(info["path"])

        # 添加根目录
        tree = [{"name": "根目录", "path": "", "children": []}]
        self._build_tree(tree[0]["children"], sorted(folders), "")
        return tree

    def _build_tree(self, children: list, folders: list[str], parent: str) -> None:
        """递归构建文件夹树。"""
        seen = set()
        for folder_path in folders:
            if parent:
                prefix = parent + "/"
                if not folder_path.startswith(prefix):
                    continue
                remainder = folder_path[len(prefix):]
            else:
                remainder = folder_path

            # 只取直接子文件夹
            parts = remainder.split("/")
            name = parts[0]
            if name in seen:
                continue
            seen.add(name)

            full_path = f"{parent}/{name}" if parent else name
            node = {"name": name, "path": full_path, "children": []}
            self._build_tree(node["children"], folders, full_path)
            children.append(node)

    def folder_exists(self, folder_path: str) -> bool:
        """检查文件夹路径是否存在。"""
        if not folder_path:
            return True  # 根目录始终存在
        return any(
            info["path"] == folder_path and info.get("is_dir")
            for info in self._index.values()
        )

    # ── 文件操作 ──────────────────────────────────────────────────────────────

    def save_file(self, file_storage, folder_path: str, relative_path: str = None) -> str:
        """保存上传的文件。重名文件直接替换。

        Args:
            file_storage: Flask 的 FileStorage 对象
            folder_path: 目标文件夹路径
            relative_path: 文件夹上传时的相对路径（如 "docs/readme.txt"）

        Returns:
            文件 ID
        """
        # 路径遍历防护
        folder_path = self._sanitize_path(folder_path)
        if relative_path:
            relative_path = self._sanitize_path(relative_path)

        if relative_path:
            # 文件夹上传：保留目录结构
            parts = relative_path.replace("\\", "/").split("/")
            file_name = parts[-1]
            sub_dirs = parts[:-1]

            # 创建子目录
            current_path = folder_path
            for d in sub_dirs:
                current_path = f"{current_path}/{d}" if current_path else d
                sub_abs = self.data_dir / current_path
                if not sub_abs.exists():
                    sub_abs.mkdir(parents=True, exist_ok=True)
                    # 索引中记录子文件夹
                    if not any(
                        info["path"] == current_path and info.get("is_dir")
                        for info in self._index.values()
                    ):
                        fid = uuid.uuid4().hex[:8]
                        self._index[fid] = {
                            "name": d,
                            "path": current_path,
                            "size": 0,
                            "is_dir": True,
                            "created": datetime.now().isoformat(),
                        }

            rel_path = f"{current_path}/{file_name}" if current_path else file_name
        else:
            # 单文件上传
            file_name = secure_name(file_storage.filename)
            rel_path = f"{folder_path}/{file_name}" if folder_path else file_name

        # 重名处理：替换已有文件（删除旧索引条目）
        abs_path = self.data_dir / rel_path
        norm_path = rel_path.replace("\\", "/")
        stale_ids = [
            fid for fid, info in self._index.items()
            if info["path"] == norm_path and not info.get("is_dir")
        ]
        for fid in stale_ids:
            del self._index[fid]

        # 如果磁盘上已有同名文件，直接覆盖
        # 保存文件
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        file_storage.save(str(abs_path))

        # 更新索引
        fid = uuid.uuid4().hex[:8]
        self._index[fid] = {
            "name": abs_path.name,
            "path": norm_path,
            "size": abs_path.stat().st_size,
            "is_dir": False,
            "created": datetime.now().isoformat(),
        }
        self._save_index()
        return fid

    def move_file(self, file_id: str, dest_folder: str) -> tuple[bool, str]:
        """移动文件到目标文件夹。返回 (成功, 消息)。"""
        dest_folder = self._sanitize_path(dest_folder)
        info = self._index.get(file_id)
        if not info:
            return False, "文件不存在"

        src_path = self.data_dir / info["path"]
        new_rel_path = f"{dest_folder}/{info['name']}" if dest_folder else info["name"]

        # 目标已存在则添加后缀
        dest_path = self.data_dir / new_rel_path
        stem, suffix = dest_path.stem, dest_path.suffix
        counter = 1
        while dest_path.exists():
            new_name = f"{stem}_{counter}{suffix}"
            new_rel_path = str(Path(new_rel_path).parent / new_name)
            dest_path = self.data_dir / new_rel_path
            counter += 1

        # 移动文件
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dest_path))

        # 更新索引
        info["path"] = new_rel_path.replace("\\", "/")
        info["name"] = dest_path.name
        self._save_index()
        return True, f"已移动到 /{dest_folder}" if dest_folder else "已移动到根目录"

    def delete_file(self, file_id: str) -> tuple[bool, str, dict | None]:
        """删除文件或空文件夹。返回 (成功, 消息, 文件信息)。"""
        info = self._index.get(file_id)
        if not info:
            return False, "文件不存在", None

        abs_path = self.data_dir / info["path"]

        if info.get("is_dir"):
            # 文件夹：检查是否为空
            prefix = info["path"] + "/"
            has_children = any(
                child_info["path"].startswith(prefix)
                for child_info in self._index.values()
                if child_info.get("path")
            )
            if has_children:
                return False, "文件夹不为空，请先删除子项", None
            if abs_path.exists():
                abs_path.rmdir()
        else:
            # 文件：删除磁盘上的文件
            if abs_path.exists():
                abs_path.unlink()

        del self._index[file_id]
        self._save_index()
        return True, f"已删除 '{info['name']}'", info

    def get_file_info(self, file_id: str) -> dict | None:
        """获取文件信息。"""
        info = self._index.get(file_id)
        if info:
            return {**info, "id": file_id}
        return None


def secure_name(filename: str) -> str:
    """安全处理文件名，移除路径分隔符和危险字符。"""
    if not filename:
        return "unnamed"
    # 取最后的文件名部分
    name = filename.replace("\\", "/").split("/")[-1]
    # 移除空字节
    name = name.replace("\x00", "")
    # 替换危险字符
    for char in ('..', '/', '\\'):
        name = name.replace(char, "_")
    return name or "unnamed"
