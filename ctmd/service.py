"""把「选版本 → 下载 → 组装实例」串成一条流程，CLI 和 GUI 共用。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import __version__, instance as inst_mod, java, manifest as man_mod
from .config import Config
from .downloader import Downloader, Progress, Stats
from .instance import InstanceInfo
from .manifest import Plan, build_plan, get_manifest, get_version_json, version_type_label
from .sources import SOURCES, SourceManager


@dataclass
class InstallResult:
    info: Optional[InstanceInfo] = None
    plan: Optional[Plan] = None
    stats: Optional[Stats] = None
    elapsed: float = 0.0
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.info) and not self.cancelled and not (self.stats and self.stats.failed)


class Installer:
    """一次安装 = 一次完整流程，可被线程安全地取消。"""

    def __init__(self, cfg: Config, source: str = "auto", workers: int = 8,
                 verify_existing: bool = True, include_assets: bool = True):
        self.cfg = cfg
        self.sources = SourceManager(preferred=source,
                                     timeout=float(cfg.get("timeout", 30)))
        self.workers = workers
        self.verify_existing = verify_existing
        self.include_assets = include_assets
        self.cancel = threading.Event()

    # ------------------------------------------------------------ 源
    def prepare_sources(self, measure: bool = True) -> None:
        self.sources.preferred = str(self.cfg.get("source", "auto"))
        if measure and self.sources.preferred == "auto":
            self.sources.measure(timeout=8)
        self.sources.apply_preference()

    def source_label(self, key: str = "") -> str:
        if key:
            src = SOURCES.get(key)
            return src.label if src else key
        keys = [k for k in self.sources.ranking]
        return SOURCES[keys[0]].label if keys else "官方源"

    # ------------------------------------------------------------ 版本
    def fetch_versions(self, force: bool = False) -> dict:
        return get_manifest(self.cfg, self.sources, force=force)

    def fetch_version(self, version_id: str) -> dict:
        return get_version_json(self.cfg, self.sources, version_id)

    # ------------------------------------------------------------ 安装
    def install(self, version_id: str, name: str = "", share: Optional[bool] = None,
                on_progress: Optional[Callable[[Progress], None]] = None,
                on_log: Optional[Callable[[str], None]] = None,
                on_file: Optional[Callable] = None) -> InstallResult:
        t0 = time.monotonic()

        def log(msg: str) -> None:
            if on_log:
                on_log(msg)

        if share is None:
            share = bool(self.cfg.get("share_assets", True))

        result = InstallResult()
        self.cancel.clear()

        # 每次安装都按配置重排源：显式选择的源置顶，另一个作备用
        self.prepare_sources(measure=(str(self.cfg.get("source", "auto")) == "auto"
                                      and not self.sources.latency))

        log(f"CTMD {__version__} · 下载源：{self.sources.describe()}")
        vjson = self.fetch_version(version_id)
        vid = vjson.get("id") or version_id
        jvm = int((vjson.get("javaVersion") or {}).get("majorVersion") or 8)
        log(f"版本 {vid}（{version_type_label(vjson.get('type', ''))}）需要 Java {jvm}")
        if not java.find_for(jvm):
            log(f"提示：系统里没找到 Java {jvm}，启动器里可能需要先装 Java")

        log("正在解析文件清单…")
        plan = build_plan(self.cfg, self.sources, vjson, include_assets=self.include_assets)
        result.plan = plan
        from .fsutil import human_size
        log(f"待处理 {len(plan.tasks)} 个文件，共 {human_size(plan.total_bytes)}"
            f"（依赖库 {plan.libraries_count} 个，资源 {plan.assets_count} 个）")
        for note in plan.notes:
            log(f"注意：{note}")

        dl = Downloader(workers=self.workers,
                        timeout=float(self.cfg.get("timeout", 30)),
                        retries=int(self.cfg.get("retries", 3)),
                        verify_existing=self.verify_existing,
                        cancel=self.cancel,
                        on_progress=on_progress,
                        on_file=on_file)
        stats = dl.run(plan.tasks)
        result.stats = stats
        result.cancelled = self.cancel.is_set()
        if result.cancelled:
            log("已取消")
            result.elapsed = time.monotonic() - t0
            return result

        if stats.failed:
            log(f"{len(stats.failed)} 个文件下载失败，例如：{stats.failed[0][1]}")
        log(f"下载完成：{stats.downloaded} 个新文件，{stats.skipped} 个已存在，"
            f"用时 {stats.seconds:.1f}s")

        if stats.failed:
            log("下载不完整，暂不组装实例。可以重试（已完成的文件会自动跳过）。")
            result.elapsed = time.monotonic() - t0
            return result

        log("正在组装实例目录…")
        target_name = name.strip() if name.strip() else vid
        if not (self.cfg.instances_dir / inst_mod.sanitize_name(target_name)).exists():
            target_name = inst_mod.unique_name(self.cfg, target_name)
        info = inst_mod.create_instance(self.cfg, target_name, plan,
                                        source_label=self.source_label(),
                                        share=share, log=log)
        result.info = info
        result.elapsed = time.monotonic() - t0
        j = java.find_for(plan.java_major)
        if j:
            log(f"可用 Java：{j['path']}")
        log(f"完成：{info.name} → {info.mc_dir}")
        return result


def run_verify(cfg: Config, name: str) -> dict:
    return inst_mod.verify_instance(cfg, name)


# ---------------------------------------------------------------- 模组加载器

def _find_instance(cfg: Config, name: str):
    for i in inst_mod.list_instances(cfg):
        if i.name == name:
            return i
    return None


def loader_versions(kind: str, mc_version: str) -> list:
    """给界面/CLI 用的加载器版本列表：[{version, tag}]（新版本在前）。"""
    from . import loaders as ld
    kind = kind.lower()
    if kind == "fabric":
        items = ld.list_fabric_loaders(mc_version)
        return [{"version": x["version"], "tag": "稳定" if x["stable"] else ""} for x in items]
    if kind == "quilt":
        items = ld.list_quilt_loaders(mc_version)
        out = []
        for i, x in enumerate(items):
            tag = "稳定" if x["stable"] and "-" not in x["version"] else ""
            if i == 0 and tag:
                tag = "最新"
            out.append({"version": x["version"], "tag": tag})
        return out
    if kind == "neoforge":
        versions = ld.list_neoforge_versions(mc_version)
        return [{"version": v, "tag": "最新" if i == 0 else ""}
                for i, v in enumerate(versions)]
    if kind == "forge":
        versions = ld.list_forge_versions(mc_version)
        promos = ld.forge_promotions()
        latest = promos.get(f"{mc_version}-latest")
        recommended = promos.get(f"{mc_version}-recommended")
        out = []
        for v in versions:
            tag = ""
            if v == recommended:
                tag = "推荐"
            elif v == latest:
                tag = "最新"
            out.append({"version": v, "tag": tag})
        return out
    raise ValueError(f"不认识的加载器：{kind}")


def install_loader(cfg: Config, instance_name: str, kind: str, version: str = "",
                   on_log=None, on_progress=None, workers: int = 8,
                   java_exe: str = "", mirror=None):
    """把 Fabric / Forge 装进已有实例，返回 LoaderResult。"""
    from . import loaders as ld

    info = _find_instance(cfg, instance_name)
    if not info:
        raise ld.LoaderError(f"没有实例：{instance_name}")
    mc_version = info.version
    if not mc_version:
        raise ld.LoaderError(f"实例 {instance_name} 里没有版本信息")

    sm = SourceManager(preferred=str(cfg.get("source", "auto")))
    sm.apply_preference()
    if mirror is None:
        mirror = bool(sm.ranking) and sm.ranking[0] == "bmclapi"

    kind = kind.lower()
    required = 8
    try:
        import json as _json
        vj = _json.loads((info.mc_dir / "versions" / mc_version / f"{mc_version}.json")
                         .read_text(encoding="utf-8"))
        required = int((vj.get("javaVersion") or {}).get("majorVersion") or 8)
    except Exception:
        pass

    if kind == "fabric":
        if not version:
            vers = ld.list_fabric_loaders(mc_version)
            stable = [v for v in vers if v["stable"]]
            if not vers:
                raise ld.LoaderError(f"Fabric 还没支持 {mc_version}")
            version = (stable or vers)[0]["version"]
        res = ld.install_fabric(cfg, sm, info.mc_dir, mc_version, version,
                                on_log=on_log, on_progress=on_progress, workers=workers)
    elif kind == "quilt":
        if not version:
            vers = ld.list_quilt_loaders(mc_version)
            stable = [v for v in vers if v["stable"]]
            if not vers:
                raise ld.LoaderError(f"Quilt 还没支持 {mc_version}")
            version = (stable or vers)[0]["version"]
        res = ld.install_quilt(cfg, sm, info.mc_dir, mc_version, version,
                               on_log=on_log, on_progress=on_progress, workers=workers)
    elif kind == "neoforge":
        if not version:
            vers = ld.list_neoforge_versions(mc_version)
            if not vers:
                raise ld.LoaderError(f"NeoForge 还没有 {mc_version} 的版本（它从 1.20.2 开始）")
            version = vers[0]
        res = ld.install_neoforge(cfg, sm, info.mc_dir, mc_version, version,
                                  required_java=required, java_exe=java_exe,
                                  use_mirror=True,   # maven.neoforged.net 在不少网络下直连不通
                                  on_log=on_log)
    elif kind == "forge":
        if not version:
            vers = ld.list_forge_versions(mc_version)
            if not vers:
                raise ld.LoaderError(f"Forge 还没有 {mc_version} 的版本")
            version = vers[0]
        res = ld.install_forge(cfg, sm, info.mc_dir, mc_version, version,
                               required_java=required, java_exe=java_exe,
                               use_mirror=mirror, on_log=on_log)
    else:
        raise ld.LoaderError(f"暂不支持这种加载器：{kind}")

    ld.record_loader(cfg, instance_name, res)
    return res


def doctor(cfg: Config) -> str:
    """连通性 / 环境体检。"""
    sm = SourceManager(preferred="auto")
    lat = sm.measure(timeout=8)
    lines = ["== 下载源测速 =="]
    for key, sec in sorted(lat.items(), key=lambda kv: kv[1]):
        label = SOURCES[key].label if key in SOURCES else key
        lines.append(f"  {label:24} {sec*1000:6.0f} ms" + ("（不可用）" if sec >= 90 else ""))
    lines.append("")
    lines.append("== Java ==")
    javas = java.scan(force=True)
    if not javas:
        lines.append("  未检测到 Java")
    for j in javas:
        lines.append(f"  Java {j['major']:>2}  {j['vendor']:<10} {j['path']}")
    lines.append("")
    lines.append("== 目录 ==")
    lines.append(f"  实例目录：{cfg.instances_dir}")
    lines.append(f"  共享仓库：{cfg.store_dir}")
    return "\n".join(lines)
