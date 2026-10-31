"""网络层：HTTP 长连接复用、流式下载、校验、失败换源。

资源文件动辄几千个小文件，每个都重新握手代价极高（实测单请求 ~750ms，
其中 TCP+TLS 就占 ~400ms），所以这里按线程维护 keep-alive 连接池。
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional, Sequence

from .fsutil import long_path, sha1_file

USER_AGENT = "CTMD/0.1 (Creative Trinity Minecraft Downloader)"
CHUNK = 256 * 1024
RANGE_CHUNK = 16 * 1024      # 实测：单次响应超过 ~20KB 就会被卡死，16KB 留余量
RANGE_WORKERS = 4

# 有些线路（Quilt 的 Cloudflare、BMCLAPI 的 CDN）单连接传 ~24KB 就卡死，
# 但按 Range 分段请求很快。这个集合记录踩过的域名，后续直接走分块。
SLOW_HOSTS: set = set()


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


_slow_file: Optional[Path] = None


def bind_slow_hosts(path: Path) -> None:
    """把"慢线路黑名单"落到磁盘，省得每次启动都重新踩一遍 30 秒超时。"""
    global _slow_file
    _slow_file = Path(path)
    try:
        if _slow_file.exists():
            SLOW_HOSTS.update(json.loads(_slow_file.read_text(encoding="utf-8")))
    except Exception:
        pass
    atexit.register(_save_slow_hosts)


def _save_slow_hosts() -> None:
    if not _slow_file or not SLOW_HOSTS:
        return
    try:
        _slow_file.parent.mkdir(parents=True, exist_ok=True)
        _slow_file.write_text(json.dumps(sorted(SLOW_HOSTS), ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


class Cancelled(Exception):
    """用户主动取消。"""


class DownloadError(Exception):
    """下载失败（所有重试与换源都失败）。"""


class HTTPStatusError(DownloadError):
    """带状态码的 HTTP 错误，方便对 429 / 403 做专门处理。"""

    def __init__(self, code: int, reason: str = ""):
        self.code = code
        super().__init__(f"HTTP {code} {reason}".strip())


_SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- 连接池

class _ConnPool:
    """每线程一份连接，避免共享 socket 的线程安全问题。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def _cache(self) -> dict:
        cache = getattr(self._local, "conns", None)
        if cache is None:
            cache = {}
            self._local.conns = cache
        return cache

    def get(self, scheme: str, host: str, timeout: float):
        key = (scheme, host)
        conn = self._cache().get(key)
        if conn is None:
            if scheme == "https":
                conn = http.client.HTTPSConnection(host, timeout=timeout, context=_SSL_CTX)
            else:
                conn = http.client.HTTPConnection(host, timeout=timeout)
            self._cache()[key] = conn
        return conn

    def drop(self, scheme: str, host: str) -> None:
        conn = self._cache().pop((scheme, host), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close_all(self) -> None:
        for conn in list(self._cache().values()):
            try:
                conn.close()
            except Exception:
                pass
        self._cache().clear()


POOL = _ConnPool()


# ---------------------------------------------------------------- 请求

def open_url(url: str, timeout: float = 30, headers: Optional[dict] = None):
    """一次性请求（不复用连接），用于流式响应或特殊场景。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=_SSL_CTX),
        urllib.request.HTTPRedirectHandler(),
    )
    return opener.open(req, timeout=timeout)


def _request(url: str, timeout: float, headers: Optional[dict] = None):
    """用连接池发 GET，自动跟随重定向。返回 (conn, resp, 最终 url)。"""
    resp = None
    for _ in range(6):
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme or "https"
        host = parts.netloc
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        hdrs = {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            **(headers or {}),
        }
        for attempt in range(2):
            conn = POOL.get(scheme, host, timeout)
            try:
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                break
            except (http.client.HTTPException, OSError):
                POOL.drop(scheme, host)
                if attempt == 1:
                    raise
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            try:
                resp.read()
            except Exception:
                POOL.drop(scheme, host)
            if not loc:
                raise DownloadError("重定向缺少 Location")
            url = urllib.parse.urljoin(url, loc)
            continue
        if resp.status >= 400:
            POOL.drop(scheme, host)
            raise HTTPStatusError(resp.status, resp.reason or "")
        return conn, resp, url
    raise DownloadError("重定向次数过多")


def fetch_json(url: str, timeout: float = 30) -> dict:
    parts = urllib.parse.urlsplit(url)
    conn, resp, _ = _request(url, timeout)
    try:
        data = resp.read()
    finally:
        POOL.drop(parts.scheme, parts.netloc)
    return json.loads(data.decode("utf-8"))


def fetch_bytes(url: str, timeout: float = 30) -> bytes:
    parts = urllib.parse.urlsplit(url)
    host = host_of(url)
    if host in SLOW_HOSTS:
        total = _probe_total_size(url, timeout)
        if total:
            return _ranged_collect(url, total, timeout)
    conn, resp, _ = _request(url, timeout)
    try:
        return resp.read()
    except Exception:
        POOL.drop(parts.scheme, parts.netloc)
        total = _probe_total_size(url, timeout)
        if not total:
            raise
        data = _ranged_collect(url, total, timeout)
        SLOW_HOSTS.add(host)
        return data
    finally:
        POOL.drop(parts.scheme, parts.netloc)


def probe(url: str, timeout: float = 12) -> float:
    """返回耗时（秒）；失败抛异常。用于测速选源。"""
    t0 = time.monotonic()
    conn, resp, _ = _request(url, timeout, headers={"Range": "bytes=0-1023"})
    try:
        resp.read(1024)
    finally:
        POOL.close_all()
    return time.monotonic() - t0


def _stream(url: str, tmp: Path, timeout: float,
            cancel: Optional[object], on_bytes: Optional[Callable[[int], None]],
            headers: Optional[dict] = None) -> int:
    """流式写入 tmp，返回字节数。中途取消会抛出 Cancelled。"""
    conn, resp, final_url = _request(url, timeout, headers)
    parts = urllib.parse.urlsplit(final_url)
    n = 0
    ok = False
    try:
        with open(long_path(tmp), "wb") as f:
            while True:
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    raise Cancelled()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
        ok = True
        return n
    finally:
        if not ok:
            POOL.drop(parts.scheme, parts.netloc)


def _range_get(url: str, pos: int, end: int, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Range": f"bytes={pos}-{end}",
        "Accept-Encoding": "identity",
        "Connection": "close",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _ranged_collect(url: str, total: int, timeout: float,
                    chunk: int = RANGE_CHUNK, workers: int = RANGE_WORKERS) -> bytes:
    """把 URL 分块抓进内存（用于清单这类小文件）。"""
    buf = bytearray(total)
    lock = threading.Lock()
    last_err = [None]

    def _fetch(pos: int) -> None:
        end = min(pos + chunk - 1, total - 1)
        data = None
        for attempt in range(4):
            try:
                data = _range_get(url, pos, end, timeout)
                break
            except urllib.error.HTTPError as e:
                last_err[0] = e
                time.sleep(6 if e.code == 429 else 1.0 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last_err[0] = e
                time.sleep(0.4 * (attempt + 1))
        if not data:
            raise DownloadError(f"分块读取失败（偏移 {pos}）：{last_err[0]}")
        with lock:
            buf[pos:pos + len(data)] = data

    positions = list(range(0, total, chunk))
    with ThreadPoolExecutor(max_workers=max(1, workers),
                            thread_name_prefix="ctmd-range") as pool:
        list(pool.map(_fetch, positions))
    return bytes(buf)


def _ranged_stream(url: str, tmp: Path, total: int, timeout: float,
                   cancel: Optional[object],
                   on_bytes: Optional[Callable[[int], None]],
                   chunk: int = RANGE_CHUNK,
                   workers: int = RANGE_WORKERS) -> int:
    """按 Range 分段、并发下载到文件，每段一条新连接。

    对付"单次响应只能跑 ~20KB"的线路（Quilt 的 Cloudflare、BMCLAPI 的 CDN）。
    """
    lock = threading.Lock()
    written = [0]
    last_err = [None]

    def _fetch(pos: int) -> None:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            return
        end = min(pos + chunk - 1, total - 1)
        data = None
        for attempt in range(4):
            try:
                data = _range_get(url, pos, end, timeout)
                break
            except urllib.error.HTTPError as e:
                last_err[0] = e
                time.sleep(6 if e.code == 429 else 1.0 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last_err[0] = e
                time.sleep(0.4 * (attempt + 1))
        if not data:
            raise DownloadError(f"分块下载失败（偏移 {pos}）：{last_err[0]}")
        with lock:
            fh.seek(pos)
            fh.write(data)
            fh.flush()
            written[0] += len(data)
            if on_bytes:
                on_bytes(len(data))

    with open(long_path(tmp), "wb") as f:
        f.truncate(total)
    fh = open(long_path(tmp), "r+b")
    try:
        positions = list(range(0, total, chunk))
        with ThreadPoolExecutor(max_workers=max(1, workers),
                                thread_name_prefix="ctmd-range") as pool:
            list(pool.map(_fetch, positions))
    finally:
        fh.close()
    if cancel is not None and getattr(cancel, "is_set", lambda: False)():
        raise Cancelled()
    if written[0] != total:
        raise DownloadError(f"分块不完整（{written[0]}/{total}）")
    return written[0]


def _probe_total_size(url: str, timeout: float) -> int:
    """探测文件总长：Range 探测 → Content-Length → HEAD，429 会退避重试。"""
    last_err = None
    for attempt in range(3):
        try:
            conn, resp, _ = _request(url, timeout, headers={"Range": "bytes=0-0"})
            cr = resp.getheader("Content-Range") or ""
            cl = resp.getheader("Content-Length") or ""
            try:
                resp.read()
            except Exception:
                pass
            if "/" in cr:
                return int(cr.rsplit("/", 1)[1])
            if resp.status == 200 and cl.isdigit():
                return int(cl)   # 服务器不认 Range，直接给了全长
        except HTTPStatusError as e:
            last_err = e
            if e.code in (429, 503):
                time.sleep(6 * (attempt + 1))
                continue
        except Exception as e:  # noqa: BLE001
            last_err = e
        # HEAD 兜底
        try:
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit():
                    return int(cl)
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.0 * (attempt + 1))
    if last_err is not None:
        raise DownloadError(f"探测文件大小失败：{last_err}")
    return 0


def _verify_and_install(tmp: Path, dest: Path, n: int, expected_size: int,
                        expected_sha1: str, verify: bool) -> None:
    if expected_size and n != int(expected_size):
        raise DownloadError(f"大小不符（{n} != {expected_size}）")
    if verify and expected_sha1:
        got = sha1_file(tmp)
        if got.lower() != str(expected_sha1).lower():
            raise DownloadError(f"SHA1 不符（{got[:8]}… != {expected_sha1[:8]}…）")
    os.replace(long_path(tmp), long_path(dest))


def download_file(
    candidates: Sequence[tuple],
    dest: Path,
    expected_size: int = 0,
    expected_sha1: str = "",
    timeout: float = 30,
    retries: int = 3,
    cancel: Optional[object] = None,
    on_bytes: Optional[Callable[[int], None]] = None,
    verify: bool = True,
) -> tuple:
    """按候选地址顺序下载，失败自动换下一个源。

    candidates: [(url, source_key), ...]
    返回 (bytes, url_used, source_key)
    """
    if not candidates:
        raise DownloadError(f"{Path(dest).name}: 没有可用下载地址")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    attempts = max(retries, len(candidates))
    last_err: Optional[BaseException] = None

    for i in range(attempts):
        url, key = candidates[i % len(candidates)]
        host = host_of(url)
        counter = [0]

        def _cb(delta: int, _c=counter):
            _c[0] += delta
            if on_bytes:
                on_bytes(delta)

        def _run(ranged: bool) -> int:
            if ranged:
                total = int(expected_size or 0)
                if not total:
                    total = _probe_total_size(url, timeout)
                if not total:
                    raise DownloadError("拿不到文件大小，没法分块（可能被限流了）")
                return _ranged_stream(url, tmp, total, timeout, cancel, _cb)
            return _stream(url, tmp, timeout, cancel, _cb)

        use_ranged = host in SLOW_HOSTS
        try:
            n = _run(use_ranged)
            _verify_and_install(tmp, dest, n, expected_size, expected_sha1, verify)
            return n, url, key
        except Cancelled:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        except Exception as e:  # noqa: BLE001 - 换源/换模式重试
            last_err = e
            try:
                tmp.unlink()
            except OSError:
                pass
            if on_bytes and counter[0]:
                on_bytes(-counter[0])

            # 慢线路兜底：先试分块，成功就记住这个域名（size 未知也能试，分块会自己探测总长）
            if not use_ranged:
                counter[0] = 0
                try:
                    n = _run(True)
                    _verify_and_install(tmp, dest, n, expected_size, expected_sha1, verify)
                    SLOW_HOSTS.add(host)
                    return n, url, key
                except Cancelled:
                    raise
                except Exception as e2:  # noqa: BLE001
                    last_err = e2
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    if on_bytes and counter[0]:
                        on_bytes(-counter[0])

            if i < attempts - 1:
                time.sleep(min(1.2 * (i + 1), 4.0))
    raise DownloadError(f"{dest.name}: {last_err}")
