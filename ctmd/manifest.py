"""版本清单、版本 JSON 解析，把"要下载什么"算成一张任务表。"""

from __future__ import annotations

import json
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import net
from .config import Config
from .downloader import Task
from .fsutil import long_path
from .sources import SourceManager


# ---------------------------------------------------------------- 清单缓存

def _cache_manifest_path(cfg: Config) -> Path:
    return cfg.store("cache", "version_manifest_v2.json")


def get_manifest(cfg: Config, sources: SourceManager, force: bool = False) -> dict:
    """拉版本清单，带本地缓存与多源回退。"""
    cache = _cache_manifest_path(cfg)
    ttl = float(cfg.get("keep_manifest_minutes", 10)) * 60
    if not force and cache.exists() and (time.time() - cache.stat().st_mtime) < ttl:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    errors = []
    for url in sources.manifest_urls():
        try:
            data = net.fetch_json(url, timeout=float(cfg.get("timeout", 30)))
            if isinstance(data, dict) and data.get("versions"):
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(cache)
                return data
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url.split('/')[2]}: {e}")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    raise RuntimeError("无法获取版本清单：" + "; ".join(errors))


def find_version_entry(manifest: dict, version_id: str) -> Optional[dict]:
    for v in manifest.get("versions", []):
        if v.get("id") == version_id:
            return v
    lowered = version_id.lower()
    for v in manifest.get("versions", []):
        if str(v.get("id", "")).lower() == lowered:
            return v
    return None


def get_version_json(cfg: Config, sources: SourceManager, version_id: str,
                     manifest: Optional[dict] = None, force: bool = False) -> dict:
    """取某个版本的 JSON（优先缓存）。"""
    cache = cfg.store("cache", "versions", f"{version_id}.json")
    if not force and cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("id"):
                return d
        except Exception:
            pass
    entry = find_version_entry(manifest or get_manifest(cfg, sources), version_id)
    if not entry:
        raise KeyError(f"版本清单里没有 {version_id}")
    last = None
    for url, _key in sources.candidates(entry["url"]):
        try:
            data = net.fetch_json(url, timeout=float(cfg.get("timeout", 30)))
            if not isinstance(data, dict) or "id" not in data:
                raise ValueError("不是有效的版本 JSON")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"下载 {version_id} 的版本 JSON 失败：{last}")


# ---------------------------------------------------------------- 规则匹配

def _os_name() -> str:
    return "windows" if platform.system() == "Windows" else (
        "osx" if platform.system() == "Darwin" else "linux")


def _os_arch() -> str:
    m = platform.machine().lower()
    return "x86" if m in ("x86", "i386", "i686") else "x64"


def rules_allow(rules, features: Optional[dict] = None) -> bool:
    """Mojang 的 rules 判定：从 False 开始，命中规则就取规则的 action。"""
    if not rules:
        return True
    features = features or {}
    allowed = False
    os_name, os_arch = _os_name(), _os_arch()
    os_version = platform.version()
    for rule in rules:
        action = rule.get("action")
        matched = True
        os_ = rule.get("os") or {}
        if "name" in os_ and os_["name"] != os_name:
            matched = False
        if matched and "arch" in os_:
            if os_["arch"] not in (os_arch, "x86" if os_arch == "x86" else "amd64"):
                matched = False
        if matched and "version" in os_:
            try:
                if not re.search(os_["version"], os_version):
                    matched = False
            except re.error:
                matched = False
        if matched and rule.get("features"):
            for k, v in rule["features"].items():
                if bool(features.get(k, False)) != bool(v):
                    matched = False
                    break
        if matched:
            allowed = (action == "allow")
    return allowed


def library_jar_path(name: str) -> str:
    """maven 坐标 -> 相对路径，如 org.lwjgl:lwjgl:2.9.0 -> org/lwjgl/lwjgl/2.9.0/lwjgl-2.9.0.jar"""
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    fn = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + ".jar"
    return "/".join(group.split(".") + [artifact, version, fn])


# ---------------------------------------------------------------- 任务规划

@dataclass
class Plan:
    version_id: str
    version_json: dict
    tasks: list = field(default_factory=list)
    total_bytes: int = 0
    assets_count: int = 0
    libraries_count: int = 0
    jar_dest: Optional[Path] = None
    notes: list = field(default_factory=list)
    asset_index_id: str = ""
    legacy_assets: bool = False

    @property
    def java_major(self) -> int:
        return int((self.version_json.get("javaVersion") or {}).get("majorVersion") or 8)


def _add(tasks: list, sources: SourceManager, url: str, dest: Path,
         size: int = 0, sha1: str = "", kind: str = "file", label: str = "") -> None:
    if not url:
        return
    tasks.append(Task(dest=dest, size=int(size or 0), sha1=str(sha1 or ""),
                      urls=sources.candidates(url), kind=kind, label=label))


def build_plan(cfg: Config, sources: SourceManager, vjson: dict,
               include_assets: bool = True, local_mc: Optional[Path] = None) -> Plan:
    """把版本 JSON 展开成完整下载任务表。

    local_mc 给定时，资源索引优先从 `<local_mc>/assets/indexes/` 读
    （PCL2 导入的实例没有 store 索引，这样校验时不必联网）。
    """
    vid = vjson.get("id") or "unknown"
    plan = Plan(version_id=vid, version_json=vjson)
    store = cfg.store_dir
    downloads = vjson.get("downloads") or {}

    # 1) 客户端主文件
    client = downloads.get("client") or {}
    if client.get("url"):
        dest = store / "versions" / vid / f"{vid}.jar"
        _add(plan.tasks, sources, client["url"], dest,
             client.get("size", 0), client.get("sha1", ""), "client", f"{vid}.jar")
        plan.jar_dest = dest
        plan.total_bytes += int(client.get("size") or 0)
    else:
        plan.notes.append("该版本没有客户端 jar（可能只是服务端/父版本）")

    # 2) 依赖库
    for lib in vjson.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        dl = lib.get("downloads") or {}
        artifact = dl.get("artifact") or {}
        if artifact.get("url"):
            _add(plan.tasks, sources, artifact["url"],
                 store / "libraries" / artifact["path"],
                 artifact.get("size", 0), artifact.get("sha1", ""), "library",
                 artifact["path"].rsplit("/", 1)[-1])
            plan.libraries_count += 1
            plan.total_bytes += int(artifact.get("size") or 0)
        elif lib.get("name") and not dl:
            # 老版本（1.5 及更早）只有 maven 坐标，连 downloads 都没有
            rel = library_jar_path(lib["name"])
            base = lib.get("url") or "https://libraries.minecraft.net/"
            if not base.endswith("/"):
                base += "/"
            _add(plan.tasks, sources, base + rel, store / "libraries" / rel,
                 0, "", "library", rel.rsplit("/", 1)[-1])
            plan.libraries_count += 1
        # 老式 natives 分类器（例如 lwjgl-platform-2.9.0-natives-windows.jar）
        natives = lib.get("natives") or {}
        ckey = natives.get("windows") if isinstance(natives, dict) else None
        if ckey:
            c = (dl.get("classifiers") or {}).get(ckey) or {}
            if c.get("url"):
                _add(plan.tasks, sources, c["url"], store / "libraries" / c["path"],
                     c.get("size", 0), c.get("sha1", ""), "library",
                     c["path"].rsplit("/", 1)[-1])
                plan.total_bytes += int(c.get("size") or 0)

    # 3) 资源索引（需要先下下来才能枚举对象）
    ai = vjson.get("assetIndex") or {}
    if ai.get("url"):
        idx_id = str(ai.get("id") or vjson.get("assets") or vid)
        idx_dest = store / "assets" / "indexes" / f"{idx_id}.json"
        _add(plan.tasks, sources, ai["url"], idx_dest,
             ai.get("size", 0), ai.get("sha1", ""), "index", f"{idx_id}.json")
        plan.asset_index_id = idx_id
        plan.total_bytes += int(ai.get("size") or 0)

        raw = None
        last = None
        idx_local = None
        if local_mc:
            cand = Path(local_mc) / "assets" / "indexes" / f"{idx_id}.json"
            if cand.exists():
                idx_local = cand
        if idx_local is not None:
            try:
                raw = json.loads(idx_local.read_text(encoding="utf-8"))
            except Exception:
                raw = None
        if raw is None and idx_dest.exists():
            try:
                raw = json.loads(idx_dest.read_text(encoding="utf-8"))
            except Exception:
                raw = None
        if raw is None:
            for url, _k in sources.candidates(ai["url"]):
                try:
                    raw = json.loads(net.fetch_bytes(url, timeout=float(cfg.get("timeout", 30))).decode("utf-8"))
                    idx_dest.parent.mkdir(parents=True, exist_ok=True)
                    idx_dest.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
        if raw is None:
            plan.notes.append(f"资源索引下载失败：{last}")
        else:
            objects = raw.get("objects") or {}
            plan.legacy_assets = bool(raw.get("virtual") or raw.get("map_to_resources"))
            if include_assets:
                for name, obj in objects.items():
                    h = obj.get("hash") or ""
                    if not h:
                        continue
                    dest = store / "assets" / "objects" / h[:2] / h
                    _add(plan.tasks, sources,
                         f"https://resources.download.minecraft.net/{h[:2]}/{h}",
                         dest, obj.get("size", 0), h, "asset", name)
                    plan.assets_count += 1
                    plan.total_bytes += int(obj.get("size") or 0)
    elif vjson.get("assets"):
        plan.notes.append("这个版本的资源索引缺失，可能无法播放声音")

    # 4) 日志配置（崩溃日志用的 log4j 配置）
    log = ((vjson.get("logging") or {}).get("client") or {}).get("file") or {}
    if log.get("url") and log.get("id"):
        _add(plan.tasks, sources, log["url"],
             store / "assets" / "log_configs" / str(log["id"]),
             log.get("size", 0), log.get("sha1", ""), "log", str(log["id"]))
        plan.total_bytes += int(log.get("size") or 0)

    # 5) 去重：同一目标文件只下一次（老版本 JSON 里常出现重复的 natives 条目）
    seen = {}
    for t in plan.tasks:
        key = str(t.dest).lower()
        if key in seen:
            continue
        seen[key] = t
    if len(seen) != len(plan.tasks):
        plan.tasks = list(seen.values())

    return plan


def version_type_label(t: str) -> str:
    return {
        "release": "正式版",
        "snapshot": "快照",
        "old_beta": "远古 Beta",
        "old_alpha": "远古 Alpha",
    }.get(t, t or "未知")
