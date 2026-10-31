"""Java 运行时检测：找系统里已装的 Java，按大版本排序。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

_CACHE: dict = {}


def _release_major(release_file: Path) -> int:
    try:
        text = release_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    m = re.search(r'JAVA_VERSION="([^"]+)"', text)
    if not m:
        return 0
    ver = m.group(1)
    if ver.startswith("1."):
        return int(ver.split(".")[1] or 0)
    m2 = re.match(r"(\d+)", ver)
    return int(m2.group(1)) if m2 else 0


def _vendor_from_path(p: str) -> str:
    low = p.lower()
    for key, name in (("adoptium", "Adoptium"), ("temurin", "Adoptium"),
                      ("zulu", "Zulu"), ("microsoft", "Microsoft"),
                      ("amazon", "Corretto"), ("corretto", "Corretto"),
                      ("oracle", "Oracle"), ("java", "Java")):
        if key in low:
            return name
    return "Java"


def _candidate_dirs() -> list:
    dirs = []
    env = os.environ.get("JAVA_HOME")
    if env:
        dirs.append(Path(env))
    roots = [
        Path(r"C:\Program Files\Java"),
        Path(r"C:\Program Files\Eclipse Adoptium"),
        Path(r"C:\Program Files\Microsoft"),
        Path(r"C:\Program Files\Zulu"),
        Path(r"C:\Program Files\Amazon Corretto"),
        Path(r"C:\Program Files\BellSoft"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        Path(os.environ.get("APPDATA", "")) / ".minecraft" / "runtime",
    ]
    for root in roots:
        try:
            if not root.exists():
                continue
            for child in root.iterdir():
                if child.is_dir():
                    dirs.append(child)
                    for sub in ("jre", "bin"):
                        if (child / "jre").is_dir():
                            dirs.append(child / "jre")
        except OSError:
            pass
    # 启动器自带的运行时
    mc_runtime = Path(os.environ.get("APPDATA", "")) / ".minecraft" / "runtime"
    if mc_runtime.exists():
        for child in mc_runtime.iterdir():
            if child.is_dir():
                for sub in child.iterdir():
                    if sub.is_dir():
                        dirs.append(sub)
    return dirs


def _exe_for(home: Path) -> tuple:
    """返回 (java.exe, javaw.exe) 路径，没有的给空串。"""
    j = home / "bin" / "java.exe"
    w = home / "bin" / "javaw.exe"
    return (str(j) if j.exists() else "", str(w) if w.exists() else "")


def _run_version(exe: str) -> int:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True,
                             text=True, errors="replace", timeout=8)
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
        if m:
            return int(m.group(1)) if int(m.group(1)) > 1 else int(m.group(2) or 0)
    except Exception:
        pass
    return 0


def scan(force: bool = False) -> list:
    """返回 [{'path','exe','java','javaw','major','vendor'}]，按 major 降序。

    exe 用于启动游戏（优先 javaw，不弹黑框）；java 用于安装器（需要控制台输出）。
    """
    if _CACHE and not force:
        return _CACHE.get("list", [])

    homes = []
    for name in ("java", "javaw"):
        which = shutil.which(name)
        if which:
            homes.append(Path(which).resolve().parent.parent)
    homes.extend(_candidate_dirs())

    found = {}
    for home in homes:
        java_exe, javaw_exe = _exe_for(home)
        if not (java_exe or javaw_exe):
            continue
        major = _release_major(home / "release") or _run_version(java_exe or javaw_exe)
        if not major:
            continue
        key = str(home).lower()
        found[key] = {
            "path": str(home),
            "exe": javaw_exe or java_exe,
            "java": java_exe,
            "javaw": javaw_exe,
            "major": major,
            "vendor": _vendor_from_path(str(home)),
        }
    result = sorted(found.values(), key=lambda x: (-x["major"], x["path"]))
    _CACHE["list"] = result
    return result


def find_for(major: int) -> dict | None:
    """找能满足指定大版本的最合适 Java（优先完全相等，其次更高版本）。"""
    javas = scan()
    if not javas:
        return None
    exact = [j for j in javas if j["major"] == major]
    if exact:
        return exact[0]
    higher = [j for j in javas if j["major"] > major]
    if higher:
        return sorted(higher, key=lambda j: j["major"])[0]
    return None


def summary_line() -> str:
    """紧凑的一行摘要，界面里用。"""
    javas = scan()
    if not javas:
        return "本机未检测到 Java"
    majors = []
    for j in javas:
        if j["major"] not in majors:
            majors.append(j["major"])
    shown = "·".join(str(m) for m in majors[:5])
    more = "" if len(majors) <= 5 else f"…"
    return f"Java {shown}{more}"
