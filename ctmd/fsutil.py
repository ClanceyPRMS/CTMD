"""文件系统小工具（Windows 友好）。

包含：
- 长路径处理（\\\\?\\ 前缀）
- 硬链接 / 目录联接（junction）
- 安全删除（绝不顺着 junction 删到共享仓库里）
- SHA1、体积统计
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

__all__ = [
    "long_path", "human_size", "sha1_file", "is_reparse_point", "make_junction",
    "link_or_copy", "ensure_shared_dir", "safe_rmtree", "dir_size", "write_text",
]


def long_path(p: "str | Path") -> str:
    """返回适合 Windows API 的路径字符串（超长时加 \\\\?\\ 前缀）。"""
    s = os.fspath(p)
    if not IS_WINDOWS:
        return s
    s = os.path.abspath(s)
    if len(s) < 240:
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC" + s[1:]
    return "\\\\?\\" + s


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha1_file(path: "str | Path", chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(long_path(path), "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def is_reparse_point(path: "str | Path") -> bool:
    """判断是否 junction / 符号链接（Python 3.11 没有 os.path.isjunction）。"""
    p = os.fspath(path)
    if not os.path.exists(p) and not os.path.islink(p):
        return False
    if hasattr(os.path, "isjunction"):
        try:
            if os.path.isjunction(p):  # type: ignore[attr-defined]
                return True
        except OSError:
            pass
    if os.path.islink(p):
        return True
    if not IS_WINDOWS:
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(long_path(p)))
        if attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def make_junction(link: Path, target: Path) -> bool:
    """创建目录联接（不需要管理员权限）。成功返回 True。"""
    link = Path(link)
    target = Path(target)
    if is_reparse_point(link):
        return True
    link.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if link.exists():
        if any(link.iterdir()):
            # 已有实体目录且有内容：不覆盖，交给调用方决定
            return False
        try:
            link.rmdir()
        except OSError:
            return False
    if not IS_WINDOWS:
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            return False
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, errors="replace",
        )
        if r.returncode == 0 and is_reparse_point(link):
            return True
    except Exception:
        pass
    return False


def _hardlink_tree(src: Path, dst: Path) -> None:
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out_dir = dst if rel == "." else dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            s = Path(root) / name
            d = out_dir / name
            if d.exists():
                continue
            try:
                os.link(long_path(s), long_path(d))
            except OSError:
                shutil.copy2(long_path(s), long_path(d))


def link_or_copy(src: Path, dst: Path) -> str:
    """优先硬链接（同盘零占用），失败则复制。返回 'link' / 'copy'。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if os.path.samefile(src, dst):
                return "link"
        except OSError:
            pass
        try:
            dst.unlink()
        except OSError:
            shutil.copy2(long_path(src), long_path(dst))
            return "copy"
    try:
        os.link(long_path(src), long_path(dst))
        return "link"
    except OSError:
        shutil.copy2(long_path(src), long_path(dst))
        return "copy"


def ensure_shared_dir(src: Path, dst: Path, copy_mode: bool = False) -> str:
    """让 dst 指向 src：junction → 硬链接树 → 复制。返回实际方式。"""
    src = Path(src); dst = Path(dst)
    src.mkdir(parents=True, exist_ok=True)
    if copy_mode:
        if dst.exists() and is_reparse_point(dst):
            os.rmdir(dst)
        shutil.copytree(long_path(src), long_path(dst), dirs_exist_ok=True, copy_function=shutil.copy2)
        return "copy"
    if is_reparse_point(dst):
        return "junction"
    if make_junction(dst, src):
        return "junction"
    try:
        _hardlink_tree(src, dst)
        return "hardlink"
    except Exception:
        shutil.copytree(long_path(src), long_path(dst), dirs_exist_ok=True, copy_function=shutil.copy2)
        return "copy"


def safe_rmtree(path: Path) -> None:
    """删除目录，但绝不跟随 junction / 符号链接进入共享仓库。

    注意：不能用 os.walk —— 在 Windows 上 junction 会被当成普通目录
    递归进去，从而删掉链接指向的共享仓库内容（真实踩过的坑）。
    这里手动 scandir，遇到 reparse point 只用 rmdir 摘掉链接本身。
    """
    path = Path(path)
    if is_reparse_point(path):
        _remove_link(path)
        return
    if not path.exists():
        return
    try:
        entries = list(os.scandir(long_path(path)))
    except OSError:
        return
    for entry in entries:
        p = Path(entry.path)
        try:
            if is_reparse_point(p):
                _remove_link(p)
            elif entry.is_dir(follow_symlinks=False):
                safe_rmtree(p)
            else:
                os.unlink(long_path(p))
        except FileNotFoundError:
            pass
        except OSError:
            pass
    try:
        os.rmdir(long_path(path))
    except OSError:
        pass


def _remove_link(p: Path) -> None:
    """只摘掉链接本身，不触碰目标。"""
    try:
        os.rmdir(long_path(p))       # 目录联接 / 目录符号链接
    except OSError:
        try:
            os.unlink(long_path(p))  # 文件符号链接
        except OSError:
            pass


def dir_size(path: Path) -> int:
    """目录大小（不进入 junction，避免把共享仓库算两遍）。"""
    total = 0
    path = Path(path)
    if is_reparse_point(path):
        return 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not is_reparse_point(Path(root) / d)]
        for name in files:
            try:
                total += os.path.getsize(long_path(Path(root) / name))
            except OSError:
                pass
    return total


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(long_path(path), "w", encoding=encoding, newline="\n") as f:
        f.write(text)
