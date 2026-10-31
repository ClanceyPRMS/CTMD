"""下载源：Mojang 官方 + BMCLAPI 镜像，支持自动测速、失败换源。

镜像规则全部按实测结果写死（2026-10 验证）：
    piston-meta.mojang.com/xxx      -> bmclapi2.bangbang93.com/xxx
    piston-data.mojang.com/xxx      -> bmclapi2.bangbang93.com/xxx
    launchermeta.mojang.com/xxx     -> bmclapi2.bangbang93.com/xxx
    launcher.mojang.com/xxx         -> bmclapi2.bangbang93.com/xxx
    libraries.minecraft.net/xxx     -> bmclapi2.bangbang93.com/maven/xxx
    resources.download.minecraft.net/xx/hash -> bmclapi2.bangbang93.com/assets/xx/hash
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import net


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    manifest: str
    rewrites: tuple = ()

    def rewrite(self, url: str) -> str:
        for prefix, replacement in self.rewrites:
            if url.startswith(prefix):
                return replacement + url[len(prefix):]
        return url


OFFICIAL = Source(
    key="official",
    label="Mojang 官方源",
    manifest="https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
)

BMCLAPI = Source(
    key="bmclapi",
    label="BMCLAPI 镜像（国内）",
    manifest="https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
    rewrites=(
        ("https://piston-meta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://piston-data.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://launchermeta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://launcher.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://libraries.minecraft.net/", "https://bmclapi2.bangbang93.com/maven/"),
        ("https://resources.download.minecraft.net/", "https://bmclapi2.bangbang93.com/assets/"),
    ),
)

SOURCES = {s.key: s for s in (OFFICIAL, BMCLAPI)}
DEFAULT_ORDER = ["official", "bmclapi"]

SOURCE_CHOICES = [
    ("auto", "自动（测速选最快）"),
    ("official", "Mojang 官方源"),
    ("bmclapi", "BMCLAPI 镜像（国内）"),
]


@dataclass
class SourceManager:
    """管理下载源顺序：先首选、失败自动换备用源。"""

    preferred: str = "auto"
    timeout: float = 30.0
    ranking: list = field(default_factory=lambda: list(DEFAULT_ORDER))
    latency: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def measure(self, timeout: float = 10.0) -> dict:
        """测速：并发探测各源清单，按耗时排序。返回 {key: 秒}。"""
        results = {}

        def _one(key: str, src: Source) -> None:
            try:
                results[key] = net.probe(src.manifest, timeout=timeout)
            except Exception:
                results[key] = 99.0

        threads = [threading.Thread(target=_one, args=(k, s), daemon=True)
                   for k, s in SOURCES.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 5)
        with self._lock:
            self.latency = dict(results)
            self.ranking = sorted(SOURCES.keys(), key=lambda k: results.get(k, 99.0))
        return self.latency

    def apply_preference(self) -> None:
        """根据 preferred 调整顺序（显式选择时首选置顶，另一个作备用）。"""
        with self._lock:
            if self.preferred in SOURCES:
                rest = [k for k in SOURCES if k != self.preferred]
                self.ranking = [self.preferred] + rest
            elif self.preferred == "auto":
                if not self.latency:
                    self.ranking = list(DEFAULT_ORDER)
                else:
                    self.ranking = sorted(SOURCES, key=lambda k: self.latency.get(k, 99.0))

    def candidates(self, url: str) -> list:
        """给一个官方地址，返回按优先级排好的 [(url, source_key), ...]。"""
        out = []
        seen = set()
        with self._lock:
            order = list(self.ranking)
        for key in order:
            src = SOURCES.get(key)
            if not src:
                continue
            u = src.rewrite(url)
            if u in seen:
                continue
            seen.add(u)
            out.append((u, key))
        if not out:
            out.append((url, "official"))
        return out

    def manifest_urls(self) -> list:
        out = []
        with self._lock:
            order = list(self.ranking)
        for key in order:
            src = SOURCES.get(key)
            if src and src.manifest not in out:
                out.append(src.manifest)
        return out or [OFFICIAL.manifest]

    def describe(self) -> str:
        parts = []
        for key in self.ranking:
            src = SOURCES.get(key)
            if not src:
                continue
            lat = self.latency.get(key)
            parts.append(f"{src.label}{'' if lat is None else f'（{lat*1000:.0f}ms）'}")
        return " → ".join(parts)
