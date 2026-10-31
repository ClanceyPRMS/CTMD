"""模组加载器联动：Fabric（纯 JSON 安装）与 Forge（官方安装器）。

Fabric：
    meta API 直接给出 profile JSON（含 inheritsFrom），把 JSON 和依赖库放好就行，
    不需要跑安装器，也不需要改客户端 jar。

Forge：
    官方安装器是唯一靠谱的方式（1.13+ 要在客户端 jar 上打二进制补丁，
    自己实现 processors 是另一个大工程）。我们用 Java 跑
    `forge-installer.jar --installClient <实例的 .minecraft>`，
    并支持 `--mirror` 走 BMCLAPI，省得下载慢。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import java as java_mod
from . import net
from .config import Config
from .downloader import Downloader, Task
from .fsutil import human_size, long_path, sha1_file
from .manifest import library_jar_path
from .sources import SourceManager

FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_ROOT = "https://maven.minecraftforge.net"
FORGE_MIRROR = "https://bmclapi2.bangbang93.com/maven"
FORGE_GROUP = "/net/minecraftforge/forge"
FORGE_PROMO = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"


class LoaderError(Exception):
    pass


@dataclass
class LoaderResult:
    kind: str
    version_id: str
    mc_version: str
    loader_version: str
    version_dir: Path
    libraries: int = 0
    seconds: float = 0.0
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- Fabric

def list_fabric_loaders(game: str, only_stable: bool = False) -> list:
    """[{version, stable}]，新的在前。"""
    url = f"{FABRIC_META}/versions/loader/{game}"
    data = json.loads(net.fetch_bytes(url, timeout=30).decode("utf-8"))
    out = []
    for item in data:
        loader = item.get("loader") or {}
        ver = loader.get("version")
        if not ver:
            continue
        stable = bool(loader.get("stable"))
        if only_stable and not stable:
            continue
        out.append({"version": ver, "stable": stable})
    return out


def fabric_profile(game: str, loader: str) -> dict:
    url = f"{FABRIC_META}/versions/loader/{game}/{loader}/profile/json"
    data = json.loads(net.fetch_bytes(url, timeout=30).decode("utf-8"))
    if not data.get("id") or not data.get("libraries"):
        raise LoaderError("Fabric 返回的 profile 不完整")
    return data


def install_fabric(cfg: Config, sources: SourceManager, mc_dir: Path,
                   game: str, loader: str,
                   on_log: Optional[Callable[[str], None]] = None,
                   on_progress: Optional[Callable] = None,
                   workers: int = 8) -> LoaderResult:
    profile = fabric_profile(game, loader)
    return _install_profile_loader(cfg, sources, mc_dir, "fabric", game, loader, profile,
                                   on_log=on_log, on_progress=on_progress, workers=workers)


# ---------------------------------------------------------------- Quilt

def _quilt_sort_key(ver: str) -> tuple:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z]+)\.(\d+))?", ver)
    if not m:
        return ((0, 0, 0), 0, 0)
    main = tuple(int(x) for x in m.group(1, 2, 3))
    stable = 1 if not m.group(4) else 0
    return (main, stable, int(m.group(5) or 0))


def list_quilt_loaders(game: str) -> list:
    """Quilt 的 meta 没有 stable 标记：按语义版本排，beta 排在同版本的正式版后面。"""
    data = json.loads(net.fetch_bytes(f"{QUILT_META}/versions/loader/{game}", timeout=30)
                      .decode("utf-8"))
    out = []
    for item in data:
        loader = item.get("loader") or {}
        ver = loader.get("version")
        if ver:
            out.append({"version": ver,
                        "stable": "-" not in ver,
                        "key": _quilt_sort_key(ver)})
    out.sort(key=lambda x: x["key"], reverse=True)
    return out


def quilt_profile(game: str, loader: str) -> dict:
    url = f"{QUILT_META}/versions/loader/{game}/{loader}/profile/json"
    data = json.loads(net.fetch_bytes(url, timeout=30).decode("utf-8"))
    if not data.get("id") or not data.get("libraries"):
        raise LoaderError("Quilt 返回的 profile 不完整")
    return data


def install_quilt(cfg: Config, sources: SourceManager, mc_dir: Path,
                  game: str, loader: str,
                  on_log: Optional[Callable[[str], None]] = None,
                  on_progress: Optional[Callable] = None,
                  workers: int = 8) -> LoaderResult:
    profile = quilt_profile(game, loader)
    return _install_profile_loader(cfg, sources, mc_dir, "quilt", game, loader, profile,
                                   on_log=on_log, on_progress=on_progress, workers=workers)


# ---------------------------------------------------------------- 共用的 JSON 加载器安装

def _install_profile_loader(cfg: Config, sources: SourceManager, mc_dir: Path,
                            kind: str, game: str, loader: str, profile: dict,
                            on_log: Optional[Callable[[str], None]] = None,
                            on_progress: Optional[Callable] = None,
                            workers: int = 8) -> LoaderResult:
    """Fabric / Quilt 通用：写 profile JSON + 下依赖库，不需要打补丁。"""
    t0 = time.monotonic()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    vid = profile["id"]
    parent = Path(mc_dir) / "versions" / game / f"{game}.json"
    if not parent.exists():
        raise LoaderError(f"实例里没有原版 {game}，先装原版再装 {kind.title()}")
    vdir = Path(mc_dir) / "versions" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / f"{vid}.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    tasks = []
    for lib in profile.get("libraries", []):
        name = lib.get("name")
        if not name:
            continue
        rel = library_jar_path(name)
        base = lib.get("url") or "https://maven.fabricmc.net/"
        if not base.endswith("/"):
            base += "/"
        tasks.append(Task(
            dest=cfg.store("libraries", *rel.split("/")),
            size=int(lib.get("size") or 0),
            sha1=str(lib.get("sha1") or ""),
            urls=sources.candidates(base + rel),
            kind="library", label=rel.rsplit("/", 1)[-1],
        ))

    log(f"{kind.title()} {loader} · 需要 {len(tasks)} 个依赖库")
    dl = Downloader(workers=workers, timeout=float(cfg.get("timeout", 30)),
                    retries=int(cfg.get("retries", 3)),
                    verify_existing=True, on_progress=on_progress)
    stats = dl.run(tasks)
    if stats.failed:
        raise LoaderError(f"{len(stats.failed)} 个依赖库下载失败：{stats.failed[0][1]}")
    log(f"依赖库完成：{stats.downloaded} 个新下载，{stats.skipped} 个已存在")

    problems = _check_version_libraries(cfg, vdir, vid)
    if problems:
        raise LoaderError("产物不完整：" + "；".join(problems[:3]))
    log("产物校验通过")

    return LoaderResult(
        kind=kind, version_id=vid, mc_version=game, loader_version=loader,
        version_dir=vdir, libraries=len(tasks),
        seconds=time.monotonic() - t0,
        notes=[f"{kind.title()} 用 inheritsFrom 继承原版，不用打补丁"],
    )


# ---------------------------------------------------------------- Forge

def list_forge_versions(game: str, limit: int = 200) -> list:
    """从 Forge 的 maven-metadata.xml 里挑出某个 MC 版本的 Forge 版本（新→旧）。"""
    xml = net.fetch_bytes(f"{FORGE_ROOT}{FORGE_GROUP}/maven-metadata.xml",
                          timeout=40).decode("utf-8", "replace")
    versions = re.findall(r"<version>([^<]+)</version>", xml)
    prefix = f"{game}-"
    out = [v[len(prefix):] for v in versions if v.startswith(prefix)]
    return out[::-1][:limit]


def forge_promotions() -> dict:
    """{ '1.20.1-latest': '47.4.28', ... }，用来标“推荐版本”。"""
    try:
        data = json.loads(net.fetch_bytes(FORGE_PROMO, timeout=30).decode("utf-8"))
        return data.get("promos") or {}
    except Exception:
        return {}


def forge_installer_url(game: str, forge: str, mirror: bool = False) -> str:
    fname = f"forge-{game}-{forge}-installer.jar"
    base = FORGE_MIRROR if mirror else FORGE_ROOT
    return f"{base}{FORGE_GROUP}/{game}-{forge}/{fname}"


# ---------------------------------------------------------------- NeoForge

NEOFORGE_ROOT = "https://maven.neoforged.net/releases"
NEOFORGE_GROUP = "/net/neoforged/neoforge"


def neoforge_prefix(game: str) -> str:
    """26.3 -> '26.3.'；1.21.5 -> '21.5.'（NeoForge 从 1.20.2 才开始有）"""
    parts = game.split(".")
    if parts[0] == "1" and len(parts) >= 3:
        return ".".join(parts[1:3]) + "."
    return game + "."


def _nf_sort_key(ver: str) -> tuple:
    main = ver.split("+")[0]
    stable = 0 if any(w in main.lower() for w in ("alpha", "beta", "snapshot", "pre")) else 1
    return (stable, [int(x) for x in re.findall(r"\d+", main)])


def list_neoforge_versions(game: str, limit: int = 100) -> list:
    """镜像优先（maven.neoforged.net 在不少网络下直接连不上）。"""
    xml = None
    last = None
    for base in (FORGE_MIRROR, NEOFORGE_ROOT):
        try:
            xml = net.fetch_bytes(f"{base}{NEOFORGE_GROUP}/maven-metadata.xml",
                                  timeout=40).decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001
            last = e
    if not xml:
        raise LoaderError(f"拿不到 NeoForge 版本列表：{last}")
    versions = re.findall(r"<version>([^<]+)</version>", xml)
    prefix = neoforge_prefix(game)
    out = [v for v in versions if v.startswith(prefix)]
    out.sort(key=_nf_sort_key, reverse=True)
    return out[:limit]


def install_neoforge(cfg: Config, sources: SourceManager, mc_dir: Path,
                     game: str, ver: str,
                     required_java: int = 8,
                     java_exe: str = "",
                     use_mirror: bool = True,
                     on_log: Optional[Callable[[str], None]] = None,
                     timeout: float = 2400) -> LoaderResult:
    return install_forge(
        cfg, sources, mc_dir, game, ver,
        required_java=required_java, java_exe=java_exe, use_mirror=use_mirror,
        on_log=on_log, timeout=timeout,
        kind="neoforge",
        official_url=f"{NEOFORGE_ROOT}{NEOFORGE_GROUP}/{ver}/neoforge-{ver}-installer.jar",
        mirror_url=f"{FORGE_MIRROR}{NEOFORGE_GROUP}/{ver}/neoforge-{ver}-installer.jar",
        mirror_base=FORGE_MIRROR,
        guess_id=f"neoforge-{ver}",
    )


def _pick_java(required_major: int) -> dict:
    j = java_mod.find_for(required_major)
    if not j:
        raise LoaderError(
            f"Forge 安装器需要 Java {required_major}，本机没找到。"
            f"可以先在 PCL2 / HMCL 里装一个 Java {required_major}，或用 --java 指定。")
    return j


def _check_version_libraries(cfg: Config, vdir: Path, vid: str) -> list:
    """校验加载器版本里列出的库是否齐全、哈希是否正确。"""
    problems = []
    vjson_path = Path(vdir) / f"{vid}.json"
    try:
        vj = json.loads(vjson_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"读不了 {vjson_path.name}：{e}"]
    for lib in vj.get("libraries", []):
        art = (lib.get("downloads") or {}).get("artifact") or {}
        path, sha = art.get("path"), art.get("sha1")
        if not path and lib.get("name"):
            # Fabric / Quilt 的 profile 只给 maven 坐标 + 顶层 sha1
            try:
                path = library_jar_path(lib["name"])
            except Exception:
                continue
            sha = lib.get("sha1") or ""
        if not path:
            continue
        f = cfg.store("libraries", *path.split("/"))
        if not f.exists():
            problems.append(f"缺少 {path}")
            continue
        if sha and sha1_file(f).lower() != str(sha).lower():
            problems.append(f"哈希不符 {path}")
    return problems


def install_forge(cfg: Config, sources: SourceManager, mc_dir: Path,
                  game: str, forge: str,
                  required_java: int = 8,
                  java_exe: str = "",
                  use_mirror: bool = False,
                  on_log: Optional[Callable[[str], None]] = None,
                  timeout: float = 1800,
                  kind: str = "forge",
                  official_url: str = "",
                  mirror_url: str = "",
                  mirror_base: str = FORGE_MIRROR,
                  guess_id: str = "") -> LoaderResult:
    """跑官方安装器装 Forge / NeoForge（两者同一套安装器）。"""
    t0 = time.monotonic()

    official_url = official_url or forge_installer_url(game, forge, False)
    mirror_url = mirror_url or forge_installer_url(game, forge, True)
    guess_id = guess_id or f"{game}-forge-{forge}"

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    mc_dir = Path(mc_dir)
    parent = mc_dir / "versions" / game / f"{game}.json"
    if not parent.exists():
        raise LoaderError(f"实例里没有原版 {game}，先装原版再装 {kind.title()}")

    cache_dir = cfg.store("cache", "installers")
    cache_dir.mkdir(parents=True, exist_ok=True)
    installer = cache_dir / f"{kind}-{game}-{forge}-installer.jar"

    # 1) 安装器本体（镜像优先，失败回官方）
    candidates = []
    if use_mirror:
        candidates.append((mirror_url, "bmclapi"))
    candidates.append((official_url, "official"))
    if not use_mirror:
        candidates.append((mirror_url, "bmclapi"))

    if not installer.exists() or installer.stat().st_size < 100_000:
        last = None
        for url, key in candidates:
            try:
                log(f"下载 Forge 安装器（{key}）…")
                n, _u, _k = net.download_file([(url, key)], installer,
                                              timeout=120, retries=2, verify=False)
                log(f"安装器就绪：{human_size(n)}")
                break
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  这个源不行：{e}")
        else:
            raise LoaderError(
                f"{kind.title()} 安装器下载失败：{last}\n"
                "（官方 maven.neoforged.net 在不少网络直连不通；镜像 BMCLAPI 也会限流，"
                "可以稍后重试，或换一条网络试试）")
    else:
        log(f"使用缓存的安装器：{installer.name}")

    # 2) Java（安装器要控制台输出，优先 java.exe 而不是 javaw.exe）
    if java_exe:
        j = {"exe": java_exe}
    else:
        picked = _pick_java(required_java)
        j = {"exe": picked.get("java") or picked.get("exe") or ""}
    log(f"使用 Java：{j['exe']}")

    # 3) 跑安装器
    before = {p.name for p in (mc_dir / "versions").iterdir() if p.is_dir()} \
        if (mc_dir / "versions").exists() else set()
    cmd = [j["exe"], "-jar", str(installer), "--installClient", str(mc_dir)]
    if use_mirror:
        cmd += ["--mirror", mirror_base]
        log("走 BMCLAPI 镜像下载依赖")
    log("运行 Forge 安装器（可能要几分钟，打补丁阶段输出很多）…")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=str(cache_dir),   # 安装器会把 <jar>.log 写在当前目录，关到缓存里别污染项目根
    )
    tail = []
    patched = 0
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 200:
            tail.pop(0)
        stripped = line.strip()
        # 打补丁阶段会刷上万行，折叠成计数
        if stripped.startswith("Patching "):
            patched += 1
            if patched % 500 == 0:
                log(f"  已打补丁 {patched} 个类…")
            continue
        # 堆栈行不刷屏
        if stripped.startswith("at ") or stripped.startswith("... ") or "SocketTimeoutException" in stripped:
            continue
        if "maven.creeperhost.net" in stripped:
            log("  镜像超时，正在换官方源重试…")
            continue
        log("  " + stripped[:180])
        if time.monotonic() > deadline:
            proc.kill()
            raise LoaderError("Forge 安装器超时")
    code = proc.wait()
    if patched:
        log(f"  打补丁完成，共 {patched} 个类")
    if code != 0:
        raise LoaderError("Forge 安装器退出码 %d：%s" % (code, "\n".join(tail[-6:])))

    after = {p.name for p in (mc_dir / "versions").iterdir() if p.is_dir()}
    new = sorted(after - before)
    vid = ""
    for name in new:
        if (mc_dir / "versions" / name / f"{name}.json").exists():
            vid = name
            break
    if not vid:
        if (mc_dir / "versions" / guess_id / f"{guess_id}.json").exists():
            vid = guess_id
        else:
            raise LoaderError("安装器跑完了，但没找到生成的版本 JSON")

    # 4) 校验产物：安装器偶尔会"退出码 0 但补丁没打上"，不校验就会留下一个假版本
    problems = _check_version_libraries(cfg, mc_dir / "versions" / vid, vid)
    if problems:
        raise LoaderError("安装器跑完了但产物不完整："
                          + "；".join(problems[:3])
                          + "（重跑一次通常就好，已下载的文件会跳过）")
    log("产物校验通过")
    log(f"完成：{vid}")
    return LoaderResult(
        kind=kind, version_id=vid, mc_version=game, loader_version=forge,
        version_dir=mc_dir / "versions" / vid,
        seconds=time.monotonic() - t0,
        notes=[f"安装器日志最后一行：{tail[-1][:120]}" if tail else ""],
    )


# ---------------------------------------------------------------- 统一入口

def instance_mc_dir(cfg: Config, name: str) -> Path:
    from . import instance as inst_mod
    return inst_mod.instance_dir(cfg, name) / ".minecraft"


def record_loader(cfg: Config, name: str, result: LoaderResult) -> None:
    """把装好的加载器写进实例的 ctmd.json，方便界面显示。"""
    from . import instance as inst_mod
    meta_path = inst_mod.instance_dir(cfg, name) / "ctmd.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    loaders = meta.setdefault("loaders", [])
    loaders = [x for x in loaders if x.get("version_id") != result.version_id]
    loaders.append({
        "kind": result.kind,
        "loader_version": result.loader_version,
        "version_id": result.version_id,
        "mc_version": result.mc_version,
        "installed": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    meta["loaders"] = loaders
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
