"""并发下载引擎：跳过已有文件、失败换源、实时进度与速度。"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import net
from .fsutil import long_path, sha1_file


@dataclass
class Task:
    dest: Path
    size: int = 0
    sha1: str = ""
    urls: list = field(default_factory=list)   # [(url, source_key), ...]
    kind: str = "file"                          # client | library | asset | index | log
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or Path(self.dest).name


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    files_done: int = 0
    files_total: int = 0
    speed: float = 0.0          # B/s
    eta: float = 0.0            # 秒
    current: str = ""

    @property
    def percent(self) -> float:
        if self.total > 0:
            return min(100.0, self.done * 100.0 / self.total)
        if self.files_total:
            return min(100.0, self.files_done * 100.0 / self.files_total)
        return 0.0


@dataclass
class Stats:
    total_bytes: int = 0
    done_bytes: int = 0
    files_total: int = 0
    files_done: int = 0
    skipped: int = 0
    downloaded: int = 0
    failed: list = field(default_factory=list)   # [(Task, str)]
    seconds: float = 0.0
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled


class Downloader:
    def __init__(
        self,
        workers: int = 8,
        timeout: float = 30,
        retries: int = 3,
        verify_existing: bool = True,
        cancel: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[Progress], None]] = None,
        on_file: Optional[Callable[[Task, str, str], None]] = None,
    ):
        self.workers = max(1, int(workers))
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.verify_existing = verify_existing
        self.cancel = cancel or threading.Event()
        self.on_progress = on_progress
        self.on_file = on_file

        self._lock = threading.Lock()
        self._done = 0
        self._files_done = 0
        self._total = 0
        self._files_total = 0
        self._samples: deque = deque(maxlen=64)
        self._last_emit = 0.0
        self._current = ""
        self._counters = {"skipped": 0, "downloaded": 0}
        self._failed: list = []

    # ------------------------------------------------------------ 内部
    def _report_bytes(self, delta: int) -> None:
        with self._lock:
            self._done += delta
        self._emit()

    def _emit(self, force: bool = False) -> None:
        if not self.on_progress:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < 0.15:
            return
        with self._lock:
            self._last_emit = now
            done, total = self._done, self._total
            files_done, files_total = self._files_done, self._files_total
            self._samples.append((now, done))
            speed = 0.0
            if len(self._samples) >= 2:
                t0, b0 = self._samples[0]
                if now - t0 > 0.3:
                    speed = max(0.0, (done - b0) / (now - t0))
            eta = (total - done) / speed if speed > 1 and total > done else 0.0
            current = self._current
        self.on_progress(Progress(done=done, total=total, files_done=files_done,
                                  files_total=files_total, speed=speed, eta=eta,
                                  current=current))

    def _is_ready(self, task: Task) -> bool:
        dest = Path(task.dest)
        try:
            if not dest.exists():
                return False
            actual = dest.stat().st_size
        except OSError:
            return False
        if task.size and actual != int(task.size):
            return False
        if self.verify_existing and task.sha1:
            try:
                return sha1_file(dest).lower() == task.sha1.lower()
            except OSError:
                return False
        return True

    def _one(self, task: Task) -> tuple:
        with self._lock:
            self._current = task.name
        if self._is_ready(task):
            size = task.size or (Path(task.dest).stat().st_size if Path(task.dest).exists() else 0)
            with self._lock:
                self._files_done += 1
                self._counters["skipped"] += 1
            if size:
                self._report_bytes(size)
            if self.on_file:
                self.on_file(task, "skip", "已存在")
            self._emit()
            return ("skip", 0, "")

        n, url, key = net.download_file(
            task.urls, Path(task.dest),
            expected_size=task.size,
            expected_sha1=task.sha1,
            timeout=self.timeout,
            retries=self.retries,
            cancel=self.cancel,
            on_bytes=self._report_bytes,
            verify=bool(task.sha1),
        )
        with self._lock:
            self._files_done += 1
            self._counters["downloaded"] += 1
            if not task.size and n:
                self._done += n
                self._total += n
        if self.on_file:
            self.on_file(task, "ok", key)
        self._emit()
        return ("ok", n, key)

    # ------------------------------------------------------------ 入口
    def run(self, tasks: list) -> Stats:
        stats = Stats()
        tasks = [t for t in tasks if t and t.urls]
        self._total = sum(int(t.size or 0) for t in tasks)
        self._files_total = len(tasks)
        self._samples.append((time.monotonic(), 0))
        t_start = time.monotonic()
        stats.files_total = len(tasks)
        stats.total_bytes = self._total

        if not tasks:
            stats.seconds = 0.0
            return stats

        try:
            with ThreadPoolExecutor(max_workers=self.workers,
                                    thread_name_prefix="ctmd-dl") as pool:
                futures = {pool.submit(self._one, t): t for t in tasks}
                for fut in as_completed(futures):
                    if self.cancel.is_set():
                        break
                    task = futures[fut]
                    try:
                        fut.result()
                    except net.Cancelled:
                        self.cancel.set()
                        break
                    except Exception as e:  # noqa: BLE001
                        msg = str(e) or type(e).__name__
                        self._failed.append((task, msg))
                        with self._lock:
                            self._files_done += 1
                        if self.on_file:
                            self.on_file(task, "error", msg)
                    self._emit()
        finally:
            stats.cancelled = self.cancel.is_set()
            stats.seconds = time.monotonic() - t_start
            with self._lock:
                stats.done_bytes = self._done
                stats.files_done = self._files_done
                stats.skipped = self._counters["skipped"]
                stats.downloaded = self._counters["downloaded"]
                stats.failed = list(self._failed)
        self._emit(force=True)
        return stats
