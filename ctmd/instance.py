"""实例管理 —— CTMD 的核心：一个实例 = 一个独立 .minecraft。

隔离策略：
    instances/<实例名>/.minecraft/versions/<版本号>/    ← 只放自己这一个版本
    instances/<实例名>/.minecraft/assets      ─┐
    instances/<实例名>/.minecraft/libraries   ─┴→ 目录联接（junction）指向 store/
    saves / mods / config / resourcepacks … 全部独立

这样每个实例的存档、模组、按键设置互不干扰，
而体积巨大的资源文件和依赖库全盘只存一份。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .config import Config
from .fsutil import (dir_size, ensure_shared_dir, is_reparse_point, link_or_copy,
                     long_path, make_junction, safe_rmtree, write_text)
from .manifest import Plan

BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


# ---------------------------------------------------------------- 名称

def sanitize_name(name: str) -> str:
    name = BAD_CHARS.sub("_", str(name or "").strip().strip("."))
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "新实例"
    if name.split(".")[0].upper() in RESERVED:
        name = f"MC-{name}"
    return name[:48]


def unique_name(cfg: Config, base: str) -> str:
    name = sanitize_name(base)
    if not (cfg.instances_dir / name).exists():
        return name
    for i in range(2, 1000):
        cand = f"{name} ({i})"
        if not (cfg.instances_dir / cand).exists():
            return cand
    return f"{name}-{datetime.now():%H%M%S}"


# ---------------------------------------------------------------- 数据

@dataclass
class InstanceInfo:
    name: str
    path: Path
    version: str = ""
    version_type: str = ""
    java_major: int = 0
    source: str = ""
    created: str = ""
    link_mode: str = ""
    size: int = 0
    version_count: int = 1
    meta: dict = field(default_factory=dict)
    loaders: list = field(default_factory=list)

    @property
    def mc_dir(self) -> Path:
        return self.path / ".minecraft"

    @property
    def size_text(self) -> str:
        from .fsutil import human_size
        return human_size(self.size)


def instance_dir(cfg: Config, name: str) -> Path:
    return cfg.instances_dir / name


def read_meta(path: Path) -> dict:
    p = Path(path) / "ctmd.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def list_versions(mc_dir: Path) -> list:
    vdir = Path(mc_dir) / "versions"
    out = []
    if vdir.exists():
        for child in vdir.iterdir():
            if child.is_dir() and (child / f"{child.name}.json").exists():
                out.append(child.name)
    return sorted(out)


def loader_label(kind: str) -> str:
    return {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge",
            "quilt": "Quilt", "optifine": "OptiFine"}.get(
                str(kind or "").lower(), str(kind or ""))


_LOADER_RE = re.compile(
    r"(?i)(neoforge|forge|fabric|quilt)[-_\s]*v?([0-9][0-9A-Za-z.+_]*)")
_OPTIFINE_TAIL = re.compile(r"(?i)-?_?(optifine|opti).*$")


def loader_kind(vid: str, vj: dict) -> str:
    """判断一个版本的加载器类型。

    既认 CTMD 自己装的（`inheritsFrom` 链），也认 PCL2 的**合并版**——
    PCL 把继承链展开成单个 JSON，没有 `inheritsFrom`，只能靠
`mainClass` / 库坐标 / 目录名反推。
    """
    low = (vid or "").lower()
    main = str(vj.get("mainClass") or "").lower()
    if vj.get("inheritsFrom"):
        parent = str(vj["inheritsFrom"]).lower()
        if "neoforge" in low:
            return "NeoForge"
        if low.startswith("quilt-loader-"):
            return "Quilt"
        if low.startswith("fabric-loader-"):
            return "Fabric"
        if low.startswith(parent + "-forge-"):
            return "Forge"
    if "neoforge" in low:
        return "NeoForge"
    if "quilt" in low:
        return "Quilt"
    if "fabric" in low or "knot" in main:
        return "Fabric"
    if "forge" in low or "bootstraplauncher" in main or "modlauncher" in main:
        return "Forge"
    for lib in (vj.get("libraries") or [])[:400]:
        n = str((lib or {}).get("name") or "").lower()
        if "net.neoforged" in n:
            return "NeoForge"
        if "net.fabricmc" in n:
            return "Fabric"
        if "org.quiltmc" in n:
            return "Quilt"
        if "minecraftforge" in n:
            return "Forge"
    for lib in (vj.get("libraries") or [])[:400]:
        if "optifine" in str((lib or {}).get("name") or "").lower():
            return "OptiFine"
    if "optifine" in low:
        return "OptiFine"
    return ""


def loader_version(vid: str, vj: dict, kind: str) -> str:
    """加载器版本号：优先从库坐标取（准），再从目录名猜。

    注意别被 `net.minecraftforge:accesstransformers:8.0.4` 这类同组包骗了，
    Forge 本体看 `net.minecraftforge:forge:`，其次 `:fmlloader:`。
    """
    libs = [str((lib or {}).get("name") or "") for lib in (vj.get("libraries") or [])[:400]]

    # CTMD 自己装的加载器：目录名形如 fabric-loader-<ver>-<parent> / <parent>-forge-<ver>
    parent = str(vj.get("inheritsFrom") or "")
    if parent:
        if vid.startswith("fabric-loader-"):
            rest = vid[len("fabric-loader-"):]
            return rest[: -(len(parent) + 1)] if rest.endswith("-" + parent) else rest
        if vid.startswith("quilt-loader-"):
            rest = vid[len("quilt-loader-"):]
            return rest[: -(len(parent) + 1)] if rest.endswith("-" + parent) else rest
        if vid.startswith(parent + "-forge-"):
            return vid[len(parent) + len("-forge-"):]
        if "neoforge" in vid.lower():
            return vid.replace("neoforge-", "").replace("-neoforge-", "-")

    def find(pred) -> str:
        for name in libs:
            if pred(name):
                parts = name.split(":")
                if len(parts) >= 3 and parts[2]:
                    return parts[2]
        return ""

    ver = ""
    if kind == "Fabric":
        ver = find(lambda n: n.startswith("net.fabricmc:fabric-loader:"))
    elif kind == "Quilt":
        ver = find(lambda n: n.startswith("org.quiltmc:quilt-loader:"))
    elif kind == "NeoForge":
        ver = (find(lambda n: n.startswith("net.neoforged:neoforge:"))
               or find(lambda n: n.startswith("net.neoforged:forge:")))
    elif kind == "Forge":
        ver = (find(lambda n: n.startswith("net.minecraftforge:forge:"))
               or find(lambda n: n.startswith("net.minecraftforge:fmlloader:")))
        if ver and "-" in ver:
            ver = ver.split("-")[-1]
    elif kind == "OptiFine":
        ver = (find(lambda n: n.lower().startswith("optifine:"))
               or find(lambda n: "optifine" in n.lower()))
        if ver and "_" in ver:
            ver = ver.split("_", 1)[1]      # 1.18.2_HD_U_H9_pre3 → HD_U_H9_pre3
    if ver:
        return ver

    m = _LOADER_RE.search(vid or "")
    if m:
        ver = _OPTIFINE_TAIL.sub("", m.group(2)).rstrip("-_")
        if ver:
            return ver
    return vid


def detect_loaders(mc_dir: Path) -> list:
    """从 versions/ 里识别装过哪些模组加载器（CTMD 装的、PCL2 装的都算）。"""
    out = []
    for vid in list_versions(mc_dir):
        try:
            vj = json.loads((Path(mc_dir) / "versions" / vid / f"{vid}.json")
                            .read_text(encoding="utf-8"))
        except Exception:
            continue
        kind = loader_kind(vid, vj)
        if not kind:
            continue
        out.append({"kind": kind, "loader_version": loader_version(vid, vj, kind),
                    "version_id": vid})
    return out


def list_instances(cfg: Config) -> list:
    root = cfg.instances_dir
    items = []
    if not root.exists():
        return items
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        mc = child / ".minecraft"
        if not mc.exists():
            continue
        meta = read_meta(child)
        vers = list_versions(mc)
        found = detect_loaders(mc)
        # 以实时扫描为准：扫描不到的一律不认（元数据可能是旧版本写的）
        by_id = {x["version_id"]: dict(x) for x in found}
        for x in (meta.get("loaders") or []):
            _vid = x.get("version_id")
            if _vid in by_id:
                by_id[_vid] = {**x, **by_id[_vid]}
        items.append(InstanceInfo(
            name=child.name,
            path=child,
            version=meta.get("version") or (vers[0] if vers else ""),
            version_type=meta.get("version_type", ""),
            java_major=int(meta.get("java_major") or 0),
            source=meta.get("source", ""),
            created=meta.get("created", ""),
            link_mode=meta.get("link_mode", ""),
            size=dir_size(child),
            version_count=len(vers),
            meta=meta,
            loaders=list(by_id.values()),
        ))
    return items


# ---------------------------------------------------------------- 创建

def _legacy_assets(cfg: Config, index_id: str, mc_dir: Path, map_to_resources: bool) -> None:
    """老版本（1.5 及以前）需要把对象铺成 virtual/legacy 与 resources/ 目录。"""
    idx = cfg.store("assets", "indexes", f"{index_id}.json")
    if not idx.exists():
        return
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except Exception:
        return
    objects = data.get("objects") or {}
    if not objects:
        return
    targets = []
    if data.get("virtual") or data.get("map_to_resources"):
        targets.append(cfg.store("assets", "virtual", "legacy"))
    if map_to_resources:
        targets.append(mc_dir / "resources")
    for base in targets:
        for name, obj in objects.items():
            h = obj.get("hash")
            if not h:
                continue
            src = cfg.store("assets", "objects", h[:2], h)
            dst = base / name
            if dst.exists() or not src.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.link(long_path(src), long_path(dst))
            except OSError:
                try:
                    shutil.copy2(long_path(src), long_path(dst))
                except OSError:
                    pass


def create_instance(cfg: Config, name: str, plan: Plan, source_label: str = "",
                    share: bool = True, log=None) -> InstanceInfo:
    """把已下载好的 store 内容组装成一个可被启动器识别的实例。"""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    name = sanitize_name(name)
    inst = cfg.instances_dir / name
    mc = inst / ".minecraft"
    vid = plan.version_id
    ver_dir = mc / "versions" / vid
    ver_dir.mkdir(parents=True, exist_ok=True)
    mc.mkdir(parents=True, exist_ok=True)

    # 1) 版本 JSON
    vjson_dest = ver_dir / f"{vid}.json"
    vjson_dest.write_text(json.dumps(plan.version_json, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    # 2) 主 jar（硬链接，几乎不占空间）
    if plan.jar_dest and plan.jar_dest.exists():
        mode = link_or_copy(plan.jar_dest, ver_dir / f"{vid}.jar")
        _log(f"主文件：{vid}.jar（{mode}）")

    # 3) 共享 assets / libraries
    link_modes = {}
    if share:
        link_modes["assets"] = ensure_shared_dir(cfg.store("assets"), mc / "assets")
        link_modes["libraries"] = ensure_shared_dir(cfg.store("libraries"), mc / "libraries")
        _log(f"共享资源：assets={link_modes['assets']}，libraries={link_modes['libraries']}")
    else:
        for sub in ("assets", "libraries"):
            if (mc / sub).exists() and is_reparse_point(mc / sub):
                os.rmdir(mc / sub)
            ensure_shared_dir(cfg.store(sub), mc / sub, copy_mode=True)
            link_modes[sub] = "copy"
        _log("资源文件已复制到实例（不共享）")

    # 4) 老版本资源
    if plan.asset_index_id:
        _legacy_assets(cfg, plan.asset_index_id, mc,
                       bool((plan.version_json.get("assetIndex") or {}).get("map_to_resources")))

    # 5) 标准目录骨架
    for d in ("saves", "mods", "resourcepacks", "shaderpacks", "screenshots",
              "logs", "config", "crash-reports", "backups"):
        (mc / d).mkdir(parents=True, exist_ok=True)

    # 6) 给官方启动器留个档案（PCL2 / HMCL 不需要，但无害）
    now_iso = datetime.now().astimezone().isoformat()
    profiles = {
        "profiles": {
            f"CTMD-{name}": {
                "name": f"CTMD · {name}",
                "type": "custom",
                "created": now_iso,
                "lastUsed": now_iso,
                "lastVersionId": vid,
                "gameDir": str(mc),
                "javaArgs": "-Xmx4G -XX:+UseG1GC",
            }
        },
        "settings": {"enableSnapshots": True, "keepLauncherOpen": True},
        "version": 3,
    }
    write_text(mc / "launcher_profiles.json", json.dumps(profiles, ensure_ascii=False, indent=2))

    # 7) 元数据
    meta = {
        "schema": 1,
        "name": name,
        "version": vid,
        "version_type": plan.version_json.get("type", ""),
        "java_major": plan.java_major,
        "source": source_label,
        "created": now_iso,
        "link_mode": ",".join(f"{k}:{v}" for k, v in link_modes.items()),
        "shared": bool(share),
        "store": str(cfg.store_dir),
        "ctmd_version": __version__,
    }
    write_text(inst / "ctmd.json", json.dumps(meta, ensure_ascii=False, indent=2))

    # 8) 使用说明
    write_text(inst / "如何启动.txt", LAUNCH_TXT.format(
        name=name, version=vid, mc=mc, java=plan.java_major))

    info = InstanceInfo(
        name=name, path=inst, version=vid,
        version_type=plan.version_json.get("type", ""),
        java_major=plan.java_major, source=source_label,
        created=now_iso, link_mode=meta["link_mode"],
        size=dir_size(inst), meta=meta,
    )
    return info


LAUNCH_TXT = """CTMD 实例：{name}
版本：{version}    需要 Java {java} 或更高

游戏目录（.minecraft）：
{mc}

—— 怎么用启动器打开 ——

【PCL2】
1. 打开 PCL2 →「设置」→「启动器」→「游戏目录」
2. 点「添加」，选择上面这个 .minecraft 文件夹
3. 回到主界面，版本列表里会出现 {version}，直接启动

【HMCL】
1. 打开 HMCL →「设置」→「游戏目录」
2. 点「添加游戏目录」，选择上面这个 .minecraft 文件夹
3. 返回主界面选择版本启动

【官方启动器】
已生成 launcher_profiles.json，用正版账号登录后
在「安装」里即可看到「CTMD · {name}」档案。

—— 为什么不会串档 ——
这个实例有自己独立的 .minecraft，只有 assets 和 libraries
是指向共享仓库的链接（省空间），存档、模组、设置都在本实例内。
"""


# ---------------------------------------------------------------- 校验 / 删除

def _resolve_local(mc: Path, dest, store_dir: Path) -> Path:
    """把计划里的 store 路径映射到实例内的对应位置。

    计划表一律按 store 布局生成，但实例的 assets/libraries 可能是指向
    别处（比如原 PCL 目录）的 junction。核对时应该看实例里「实际能看到的
    那个文件」，否则会把完好的导入实例报成一堆缺失。
    """
    d = Path(dest)
    try:
        rel = d.relative_to(store_dir)
    except ValueError:
        return d
    cand = Path(mc) / rel
    return cand if cand.exists() else d


def _index_usable(path: Path, mc: Path) -> bool:
    """资源索引和官方声明不同时，判断它还能不能用。

    典型场景：PCL / 其它启动器会「更新语言文件」并重写 assets/indexes/<id>.json，
    这时文件的 size/sha1 必然与 Mojang 声明不符，但索引指向的对象本地都在，
    游戏完全正常。这种情况不该报「损坏」。
    """
    try:
        idx = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return False
    objs = idx.get("objects") or {}
    if not objs:
        return False
    base = Path(mc) / "assets" / "objects"
    if not base.is_dir():
        return False
    for obj in objs.values():
        h = str((obj or {}).get("hash") or "")
        if not h or not (base / h[:2] / h).exists():
            return False
    return True


def verify_instance(cfg: Config, name: str) -> dict:
    """逐文件核对实例完整性（jar、依赖库、资源对象）。

    按实例内实际可见的路径核对，兼容两种布局：
      - CTMD 原生：assets/libraries 是 junction → store
      - PCL 导入：assets/libraries 指向原目录，或直接是实体目录
    """
    from . import sources as sources_mod
    from .manifest import build_plan

    inst = cfg.instances_dir / name
    mc = inst / ".minecraft"
    result = {"name": name, "checked": 0, "missing": [], "corrupt": [], "notes": [], "ok": True}
    vers = list_versions(mc)
    if not vers:
        result["ok"] = False
        result["missing"].append("versions/ 下没有版本")
        return result

    sm = sources_mod.SourceManager()
    sm.apply_preference()
    from .fsutil import sha1_file

    for vid in vers:
        vjson = json.loads((mc / "versions" / vid / f"{vid}.json").read_text(encoding="utf-8"))
        plan = build_plan(cfg, sm, vjson, include_assets=True, local_mc=mc)
        # 加载器版本靠 inheritsFrom 继承原版，自己没有 jar；合并版自己带 jar
        parent = vjson.get("inheritsFrom")
        jar = mc / "versions" / vid / f"{vid}.jar"
        if not jar.exists():
            if parent:
                parent_json = mc / "versions" / parent / f"{parent}.json"
                parent_jar = mc / "versions" / parent / f"{parent}.jar"
                if not parent_json.exists() or not parent_jar.exists():
                    result["missing"].append(f"父版本 {parent} 不完整（{vid} 要它）")
            else:
                result["missing"].append(f"versions/{vid}/{vid}.jar")
        for task in plan.tasks:
            dest = _resolve_local(mc, task.dest, cfg.store_dir)
            result["checked"] += 1
            if not dest.exists():
                try:
                    label = str(dest.relative_to(mc))
                except ValueError:
                    label = str(dest)
                result["missing"].append(label)
                continue
            if task.size and dest.stat().st_size != int(task.size):
                if task.kind == "index" and _index_usable(dest, mc):
                    result["notes"].append(
                        f"{dest.name} 与官方声明不同（本地启动器更新过索引），内容可用")
                    continue
                result["corrupt"].append(f"{dest.name}（大小不符）")
                continue
            if task.sha1 and dest.stat().st_size < 2 * 1024 * 1024:
                if sha1_file(dest).lower() != task.sha1.lower():
                    if task.kind == "index" and _index_usable(dest, mc):
                        result["notes"].append(
                            f"{dest.name} 与官方声明不同（本地启动器更新过索引），内容可用")
                        continue
                    result["corrupt"].append(f"{dest.name}（哈希不符）")
    result["ok"] = not result["missing"] and not result["corrupt"]
    return result


def delete_instance(cfg: Config, name: str) -> None:
    """删除实例。junction 只摘链接，绝不会波及共享仓库。"""
    target = cfg.instances_dir / name
    if target.exists() or is_reparse_point(target):
        safe_rmtree(target)


def instance_total_versions(mc: Path) -> list:
    return list_versions(mc)


def store_usage(cfg: Config) -> dict:
    return {
        "assets": dir_size(cfg.store("assets")),
        "libraries": dir_size(cfg.store("libraries")),
        "versions": dir_size(cfg.store("versions")),
    }


def prune_store_versions(cfg: Config) -> list:
    """清掉没有任何实例引用的版本本体（store/versions/<id>）。

    实例里的 jar 是硬链接，删掉仓库这一份不会影响已有实例。
    """
    used = set()
    for inst in list_instances(cfg):
        used.update(list_versions(inst.mc_dir))
    removed = []
    vdir = cfg.store("versions")
    if not vdir.exists():
        return removed
    for child in sorted(vdir.iterdir()):
        if not child.is_dir() or child.name in used:
            continue
        size = dir_size(child)
        safe_rmtree(child)
        removed.append((child.name, size))
    return removed


# ---------------------------------------------------------------- 从 PCL 目录导入
#
# PCL2 开「版本隔离」时，每个版本的游戏目录（gameDir）就是
#     <PCL>\.minecraft\versions\<id>\
# 而 CTMD 的实例目录也是 gameDir：
#     instances\<名>\.minecraft\
# 两者一一对应，所以导入 = 把版本目录里的「游戏数据」上提一层，
# 再把 <id>.json / <id>.jar 放回标准位置 versions\<id>\。

PCL_KEEP_NAMES = {"PCL"}          # PCL 的版本级设置，留在原地
PCL_SKIP_SUFFIX = "-natives"      # 启动器会自己重新解压


@dataclass
class ImportResult:
    name: str = ""
    version: str = ""
    ok: bool = True
    entries: int = 0
    size: int = 0
    share: str = ""
    stats: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def _entry_move(src: Path, dst: Path, mode: str, stats: dict) -> None:
    """把一个文件/目录放到 dst（目录递归合并，已存在的跳过）。"""
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        try:
            children = list(os.scandir(long_path(src)))
        except OSError:
            return
        for e in children:
            _entry_move(Path(e.path), dst / e.name, mode, stats)
        return
    if dst.exists():
        stats["skip"] = stats.get("skip", 0) + 1
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if mode == "move":
            os.replace(long_path(src), long_path(dst))
            stats["move"] = stats.get("move", 0) + 1
            return
        if mode == "link":
            os.link(long_path(src), long_path(dst))
            stats["link"] = stats.get("link", 0) + 1
            return
    except OSError:
        pass
    shutil.copy2(long_path(src), long_path(dst))
    stats["copy"] = stats.get("copy", 0) + 1


def _link_shared(cfg: Config, mc: Path, pcl_mc: Path, share: str) -> dict:
    """给实例接上 assets / libraries。"""
    modes = {}
    for sub in ("assets", "libraries"):
        dst = mc / sub
        src = pcl_mc / sub
        if share == "copy":
            ensure_shared_dir(cfg.store(sub), dst, copy_mode=True)
            modes[sub] = "copy"
            continue
        if share == "local" and src.exists():
            if is_reparse_point(dst):
                os.rmdir(dst)
            if make_junction(dst, src):
                modes[sub] = "local"
                continue
        modes[sub] = ensure_shared_dir(cfg.store(sub), dst)
    return modes


def import_pcl_version(cfg: Config, pcl_mc: Path, vid: str, name: str = "",
                       mode: str = "link", share: str = "local",
                       log=None) -> ImportResult:
    """把一个 PCL 版本目录导入成 CTMD 实例（不动源目录，除非 mode='move'）。"""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    pcl_mc = Path(pcl_mc)
    src_ver = pcl_mc / "versions" / vid
    vjson_path = src_ver / f"{vid}.json"
    if not vjson_path.exists():
        raise FileNotFoundError(f"{src_ver} 里没有 {vid}.json")

    base = sanitize_name(name or vid)
    inst = cfg.instances_dir / base
    if inst.exists() and any(inst.iterdir()):
        base = unique_name(cfg, base)
        inst = cfg.instances_dir / base
    mc = inst / ".minecraft"
    mc.mkdir(parents=True, exist_ok=True)

    vj = json.loads(vjson_path.read_text(encoding="utf-8"))
    res = ImportResult(name=base, version=vid)
    stats: dict = {}

    # 1) 版本本体 → versions/<id>/
    vdir = mc / "versions" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    for fn in (f"{vid}.json", f"{vid}.jar"):
        s = src_ver / fn
        if s.exists():
            _entry_move(s, vdir / fn, mode, stats)

    # 2) 游戏数据（saves/mods/config/options.txt/…) → 上提到 .minecraft 根
    for e in sorted(os.scandir(long_path(src_ver)), key=lambda x: x.name.lower()):
        nm = e.name
        if nm in (f"{vid}.json", f"{vid}.jar") or nm in PCL_KEEP_NAMES:
            continue
        if nm.endswith(PCL_SKIP_SUFFIX) and Path(e.path).is_dir():
            res.notes.append(f"留在原地的解压库：{nm}")
            continue
        _entry_move(Path(e.path), mc / nm, mode, stats)
        res.entries += 1

    # 3) 共享目录
    modes = _link_shared(cfg, mc, pcl_mc, share)
    res.share = ",".join(f"{k}:{v}" for k, v in modes.items())
    _log(f"共享目录：{res.share}")

    # 4) 标准骨架 + 官方启动器档案
    for d in ("saves", "mods", "resourcepacks", "shaderpacks", "screenshots",
              "logs", "config", "crash-reports", "backups"):
        (mc / d).mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now().astimezone().isoformat()
    profiles = {
        "profiles": {f"CTMD-{base}": {
            "name": f"CTMD · {base}", "type": "custom",
            "created": now_iso, "lastUsed": now_iso, "lastVersionId": vid,
            "gameDir": str(mc), "javaArgs": "-Xmx4G -XX:+UseG1GC"}},
        "settings": {"enableSnapshots": True, "keepLauncherOpen": True},
        "version": 3,
    }
    write_text(mc / "launcher_profiles.json",
               json.dumps(profiles, ensure_ascii=False, indent=2))

    # 5) 元数据
    loaders = [x for x in detect_loaders(mc) if x.get("version_id") == vid]
    meta = {
        "schema": 1, "name": base, "version": vid,
        "version_type": vj.get("type", ""),
        "java_major": int((vj.get("javaVersion") or {}).get("majorVersion") or 8),
        "source": "PCL 导入", "created": now_iso,
        "link_mode": res.share, "shared": share != "copy",
        "imported_from": str(pcl_mc), "import_mode": mode,
        "pcl_isolated": True, "loaders": loaders,
    }
    write_text(inst / "ctmd.json", json.dumps(meta, ensure_ascii=False, indent=2))
    write_text(inst / "如何启动.txt", LAUNCH_TXT.format(
        name=base, version=vid, mc=mc, java=meta["java_major"]))

    res.stats = stats
    res.size = dir_size(inst)
    res.ok = True
    return res


def import_pcl(cfg: Config, pcl_mc: Path, versions=None, mode: str = "link",
               share: str = "local", log=None) -> list:
    """批量导入一个 PCL 游戏目录里的所有版本。"""
    pcl_mc = Path(pcl_mc)
    vids = list(versions) if versions else list_versions(pcl_mc)
    out = []
    for vid in vids:
        try:
            r = import_pcl_version(cfg, pcl_mc, vid, mode=mode, share=share, log=log)
            if log:
                log(f"✓ {vid} → 实例「{r.name}」（{r.entries} 项，"
                    f"{len(r.stats)} 类操作 {r.stats}）")
            out.append(r)
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"✗ {vid}：{e}")
            out.append(ImportResult(version=vid, ok=False, notes=[str(e)]))
    return out


def _repair_target(mc: Path, dest, store_dir: Path) -> Path:
    """补文件写到「实例里能看见」的位置，而不是死写 store。

    实例的 assets/libraries 可能是指向原 PCL 目录的 junction，
    只写 store 的话实例里仍然看不到。
    """
    d = Path(dest)
    try:
        rel = d.relative_to(store_dir)
    except ValueError:
        return d
    cand = Path(mc) / rel
    try:
        cand.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return d
    return cand


def repair_instance(cfg: Config, name: str, workers: int = 8,
                    include_assets: bool = True, dry_run: bool = False,
                    log=None, on_progress=None) -> dict:
    """按校验结果补齐缺失/大小不符的文件（PCL 目录常缺 log_config、旧音效）。"""
    import threading

    from . import sources as sources_mod
    from .downloader import Downloader, Task as DT
    from .manifest import build_plan

    def _log(msg: str) -> None:
        if log:
            log(msg)

    inst = cfg.instances_dir / name
    mc = inst / ".minecraft"
    if not mc.is_dir():
        raise FileNotFoundError(f"没有这个实例：{inst}")

    sm = sources_mod.SourceManager()
    sm.apply_preference()

    todo = []
    seen = set()
    for vid in list_versions(mc):
        vjson = json.loads((mc / "versions" / vid / f"{vid}.json").read_text(encoding="utf-8"))
        plan = build_plan(cfg, sm, vjson, include_assets=include_assets, local_mc=mc)
        for t in plan.tasks:
            cur = _resolve_local(mc, t.dest, cfg.store_dir)
            bad = not cur.exists()
            if not bad and t.size and cur.stat().st_size != int(t.size):
                bad = True
            if not bad:
                continue
            target = _repair_target(mc, t.dest, cfg.store_dir)
            key = str(target).lower()
            if key in seen:
                continue
            seen.add(key)
            todo.append(DT(dest=target, size=t.size, sha1=t.sha1, urls=t.urls,
                           kind=t.kind, label=t.label))

    if dry_run:
        return {"count": len(todo), "tasks": todo, "stats": None, "ok": False}
    if not todo:
        return {"count": 0, "tasks": [], "stats": None, "ok": True}

    _log(f"需要补 {len(todo)} 个文件")
    dl = Downloader(workers=workers,
                    timeout=float(cfg.get("timeout", 30)),
                    retries=int(cfg.get("retries", 3)),
                    verify_existing=True,
                    cancel=threading.Event(),
                    on_progress=on_progress)
    stats = dl.run(todo)
    return {"count": len(todo), "tasks": todo, "stats": stats,
            "ok": not stats.failed}
