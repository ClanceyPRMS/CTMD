"""配置读写（config.json 放在程序根目录）。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULTS = {
    "instances_dir": "instances",   # 实例根目录（相对程序目录或绝对路径）
    "store_dir": "store",           # 共享仓库（libraries / assets / 版本本体）
    "source": "auto",               # auto | official | bmclapi
    "workers": 8,                   # 并发下载数
    "timeout": 30,                  # 单请求超时（秒）
    "retries": 3,                   # 失败重试次数
    "verify_existing": True,        # 已有文件是否校验 SHA1
    "download_assets": True,        # 是否下载资源文件（声音、语言等）
    "share_assets": True,           # 实例间共享 assets/libraries（junction）
    "keep_manifest_minutes": 10,    # 版本清单缓存时间
    "last_version": "",             # 上次选择的版本
}


class Config:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "config.json"
        self._lock = threading.RLock()
        self.data = dict(DEFAULTS)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> dict:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            self.data[k] = v
                except Exception:
                    pass
            return self.data

    def save(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # ---------- 访问 ----------
    def get(self, key: str, default=None):
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
        self.save()

    def update(self, **kwargs) -> None:
        with self._lock:
            self.data.update(kwargs)
        self.save()

    # ---------- 路径 ----------
    def _abs(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def instances_dir(self) -> Path:
        return self._abs(str(self.get("instances_dir")))

    @property
    def store_dir(self) -> Path:
        return self._abs(str(self.get("store_dir")))

    def ensure_dirs(self) -> None:
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("versions", "libraries", "assets/indexes", "assets/objects",
                    "assets/log_configs", "assets/virtual", "cache"):
            (self.store_dir / sub).mkdir(parents=True, exist_ok=True)

    def store(self, *parts: str) -> Path:
        return self.store_dir.joinpath(*parts)
