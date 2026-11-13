#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HighSpeedDownloader —— aiohttp 异步分片 + requests/threading 同步兜底。

特点
----
* 先 HEAD 探测 ``Content-Length`` / ``Accept-Ranges``；HEAD 不可用时用
  ``GET Range: bytes=0-0`` 复核。
* 支持 Range 时用 aiohttp 异步分片下载；并发连接数可大于分片数，
  多余名额会把分片再切成子段并发下载，起手/收尾阶段都能吃满带宽
  （一个分片一条连接），由 ``asyncio.Semaphore`` 严格控制。
* 单个分片异步重试仍失败时，单独把该分片交给一个 requests + threading
  线程兜底，不影响其它分片。
* 服务端忽略 Range 时，整体降级为单连接同步下载（无 Range 无法真正分片）。
* 分片各自落成独立临时文件，启动时按大小跳过已完成分片，支持断点续传。
* 全部完成后按偏移顺序合并，``os.replace`` 原子替换目标文件，再清理临时分片。
* 右上角“设置”可修改新建任务的默认保存目录（默认系统“下载”目录），
  配置持久化在 ``%APPDATA%\\HighSpeedDownloader\\config.json``。
* 分片数、并发连接、重试次数、分片大小与默认保存目录在关闭窗口时写入同一份配置，
  下次启动原样恢复；分片数旁常驻“最高 N 片”（硬上限 256，且每片不小于 0.5 MB）。
* 默认按系统证书库校验 HTTPS（与浏览器一致），可修复公司代理/杀毒软件/加速器
  替换证书导致的 ``CERTIFICATE_VERIFY_FAILED``；设置里可切回 certifi。
* tkinter 图形界面是本工具唯一入口；不启动任何 Web 服务、不监听端口。

运行环境：Windows + Python 3.9+（开发验证于 Python 3.14）。
依赖安装：``pip install aiohttp requests``。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import re
import shutil
import ssl
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
import requests
from requests.adapters import HTTPAdapter

# --------------------------------------------------------------------------- #
# 常量配置
# --------------------------------------------------------------------------- #

APP_TITLE = "HighSpeedDownloader · aiohttp + requests"
APP_VERSION = "1.0.1"

# 常规 Chrome 浏览器 UA，避免部分 CDN 直接拒绝脚本 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 单片最小值 0.5 MB：分片数再多，也不会切出比这更小的片，避免 inode/请求爆炸
MIN_CHUNK_SIZE = 512 * 1024

DEFAULT_SHARDS = 16       # 默认目标分片数（断点/合并粒度）
DEFAULT_CONCURRENCY = 32  # 默认并发连接数（可以大于分片数：多余连接把分片再切子段）
DEFAULT_RETRY = 3         # 默认单分片额外重试次数（总尝试 = retry + 1）

# 并发连接数硬上限：再多会同时压垮源站与本机端口表
MAX_CONCURRENCY = 256

# 同一分片最多切成几条子连接并发下载（尾部只剩少量分片时用来吃满带宽）
MAX_SUB_CONNECTIONS = 8

# 分片数硬上限：并发连接数等于分片数，再多会同时压垮源站与本机端口表，
# 实际可用上限还会被「最小分片 0.5 MB」进一步压低（见 max_shards_for_size）。
MAX_SHARDS = 256

CONNECT_TIMEOUT = 10.0    # 建立连接超时（秒）
READ_TIMEOUT = 30.0       # 单次读取空闲超时（秒）

PROGRESS_INTERVAL = 0.20  # 进度采样间隔（秒）
SPEED_WINDOW = 2.0        # 瞬时速度滑动窗口（秒）

COPY_BUFFER = 4 * 1024 * 1024   # 合并分片时的拷贝块大小
WRITE_CHUNK = 1024 * 1024       # 下载写盘块大小
PIECE_SIZE = 8 * 1024 * 1024    # 单条连接一次最多负责的字节数，用完换新连接

META_VERSION = 1

# 用户偏好配置文件：记录“默认保存目录”、分片数、重试次数、分片大小与上次保存路径，
# 放在 %APPDATA%\HighSpeedDownloader\config.json
CONFIG_DIR_NAME = "HighSpeedDownloader"
CONFIG_FILE_NAME = "config.json"


# --------------------------------------------------------------------------- #
# 异常定义
# --------------------------------------------------------------------------- #

class DownloadError(Exception):
    """可预期的下载失败，消息直接展示给用户。"""


class DownloadCancelled(Exception):
    """用户主动停止。"""


class RangeIgnored(Exception):
    """服务端忽略 Range，返回 200 全量内容。"""


class RangeMismatch(DownloadError):
    """服务端返回的 Content-Range 与请求区间不一致。"""


class ShardFailed(DownloadError):
    """单个分片重试耗尽。"""

    def __init__(self, index: int, cause: BaseException | None) -> None:
        self.index = index
        self.cause = cause
        super().__init__(f"分片 {index} 重试耗尽：{cause}")


# --------------------------------------------------------------------------- #
# 小工具函数
# --------------------------------------------------------------------------- #

def format_bytes(num: float) -> str:
    """把字节数格式化成人类可读字符串。"""
    if num <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_speed(num: float) -> str:
    """把字节/秒格式化成人类可读速度。"""
    if num <= 0:
        return "0 B/s"
    return f"{format_bytes(num)}/s"


def format_eta(seconds: float) -> str:
    """把剩余秒数格式化成 mm:ss / hh:mm:ss。"""
    if seconds <= 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_int(value: Optional[str]) -> Optional[int]:
    """安全解析响应头里的整数。"""
    if value is None:
        return None
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def parse_content_range(header: Optional[str]) -> Optional[Tuple[int, int, Optional[int]]]:
    """解析 ``Content-Range: bytes 0-99/1234``，返回 (start, end, total)。"""
    if not header:
        return None
    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", header.strip(), re.IGNORECASE)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    return start, end, total


def sanitize_filename(name: str) -> str:
    """把任意文件名清洗成 Windows 可用形式。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unquote(name)).strip(" .")
    return name or "download.bin"


def filename_from_content_disposition(header: Optional[str]) -> Optional[str]:
    """从 Content-Disposition 提取文件名（支持 filename*=UTF-8''...）。"""
    if not header:
        return None
    match = re.search(r"filename\*\s*=\s*([^;]+)", header, re.IGNORECASE)
    if match:
        value = match.group(1).strip().strip('"')
        if "''" in value:  # RFC 5987：charset'lang'encoded
            value = value.split("''", 1)[1]
        try:
            return sanitize_filename(unquote(value))
        except Exception:  # noqa: BLE001 - 解析失败就退回普通 filename
            pass
    match = re.search(r'filename\s*=\s*"([^"]+)"', header, re.IGNORECASE)
    if not match:
        match = re.search(r"filename\s*=\s*([^;]+)", header, re.IGNORECASE)
    if match:
        return sanitize_filename(match.group(1).strip())
    return None


def default_output_name(url: str, suggested: Optional[str] = None) -> str:
    """从 URL（优先用服务端建议的文件名）推导一个安全的默认文件名。"""
    if suggested:
        return sanitize_filename(suggested)
    name = unquote(Path(urlparse(url).path).name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download.bin"


def system_downloads_dir() -> Path:
    """返回系统“下载”目录。

    优先用 Windows 已知文件夹（即使用户把“下载”重定向到 D 盘也能拿到正确路径），
    失败时退回 ``~/Downloads``，再失败退回当前目录。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
            folder_id = _GUID(
                0x374DE290, 0x123F, 0x4565,
                (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
            )
            path_ptr = ctypes.c_wchar_p()
            result = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)
            )
            if result == 0 and path_ptr.value:
                path = Path(path_ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                if path.is_dir():
                    return path
        except Exception:
            pass

    candidate = Path.home() / "Downloads"
    return candidate if candidate.is_dir() else Path.cwd()


def config_path() -> Path:
    """用户配置文件路径：Windows 用 %APPDATA%，其他系统用 ~/.config。"""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_settings() -> dict:
    """读取用户设置；文件不存在或损坏时返回空字典，不影响启动。"""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict) -> None:
    """原子写入用户设置，避免写一半损坏配置。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


# --------------------------------------------------------------------------- #
# HTTPS 证书：默认采用「系统证书库」的信任范围
# --------------------------------------------------------------------------- #

class SystemTrustAdapter(HTTPAdapter):
    """让 requests 使用指定 SSLContext（通常是系统证书库）建立连接。

    requests 默认只认内置 certifi 根证书，遇到公司代理、杀毒软件或加速器
    （如本机的 SteamTools）动态安装的根证书时，会报
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``。
    这里把 ``ssl.create_default_context()``（Windows 下会加载 ROOT/CA 证书库）
    注入 urllib3 的连接池，使信任范围与浏览器一致，且无需在磁盘上落地证书文件。
    """

    def __init__(self, context: ssl.SSLContext, **kwargs) -> None:
        self._ssl_context = context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs) -> None:  # type: ignore[override]
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):  # type: ignore[override]
        proxy_kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def build_http_session(
    trust_system_ca: bool = True,
    headers: Optional[dict] = None,
    pool_size: int = 10,
) -> requests.Session:
    """创建一个 requests 会话：可复用连接，并按需信任系统证书库。"""
    session = requests.Session()
    if headers:
        session.headers.update(headers)
    if trust_system_ca:
        try:
            context = ssl.create_default_context()
            adapter = SystemTrustAdapter(context, pool_connections=pool_size,
                                         pool_maxsize=pool_size)
            session.mount("https://", adapter)
        except Exception:  # noqa: BLE001 - 系统证书不可用时退回 certifi
            pass
    return session


def split_range(start: int, end: int, parts: int) -> List[Tuple[int, int]]:
    """把闭区间 ``[start, end]`` 均分成不超过 ``parts`` 个子区间。

    用于“并发连接数大于分片数”时，把一个分片再切成多条子连接并发下载。
    """
    total = end - start + 1
    if total <= 0:
        return []
    parts = max(1, min(int(parts), total))
    step = math.ceil(total / parts)
    ranges: List[Tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        ranges.append((cursor, min(end, cursor + step - 1)))
        cursor += step
    return ranges


def default_output_path(
    url: str, directory: Optional[Path] = None, suggested: Optional[str] = None
) -> str:
    """默认保存到指定目录（缺省为系统“下载”目录）；目录不可用时落到当前目录。"""
    target_dir = Path(directory) if directory else system_downloads_dir()
    if not target_dir.is_dir():
        target_dir = Path.cwd()
    return str(target_dir / default_output_name(url, suggested))


def max_shards_for_size(total: int) -> int:
    """按文件大小推算“最高支持的分片数”。

    规则：硬上限 ``MAX_SHARDS``，且每片不小于 ``MIN_CHUNK_SIZE``（0.5 MB）。
    未知大小时（``total <= 0``）只返回硬上限。
    """
    if total <= 0:
        return MAX_SHARDS
    return max(1, min(MAX_SHARDS, math.ceil(total / MIN_CHUNK_SIZE)))


def _as_int(value: object, fallback: int, low: int, high: int) -> int:
    """把配置里的值收敛成合法整数，越界时夹取，非法时用默认值。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return min(max(number, low), high)


def _as_text(value: object, fallback: str = "") -> str:
    """把配置里的值收敛成去空白的字符串。"""
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def enable_dpi_awareness() -> None:
    """让 Windows 高分屏下的 tkinter 界面保持清晰。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProbeResult:
    """探测结果。"""

    final_url: str
    status_code: int
    total_size: Optional[int]
    supports_range: bool
    reason: str
    # 服务端在 Content-Disposition 里声明的文件名（可选）
    filename: Optional[str] = None


@dataclass(frozen=True)
class Shard:
    """一个分片对应的字节区间 [start, end]（闭区间）。"""

    index: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


@dataclass
class DownloadPlan:
    """本次下载的完整规划。"""

    url: str
    output: Path
    total_size: int
    chunk_size: int
    shards: List[Shard]


@dataclass
class TaskMeta:
    """``<output>.meta.json`` 的内容，用于校验断点是否属于同一任务。"""

    url: str
    output: str
    total_size: int
    chunk_size: int
    shard_count: int
    version: int = META_VERSION

    def matches(self, url: str, output: Path, total: int, chunk: int, count: int) -> bool:
        return (
            self.version == META_VERSION
            and self.url == url
            and self.output == str(output)
            and self.total_size == total
            and self.chunk_size == chunk
            and self.shard_count == count
        )


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #

def _browser_headers(user_agent: str, extra: Optional[dict] = None) -> dict:
    """构造统一请求头：浏览器 UA + 禁止压缩（保证 Range 按原始字节对齐）。"""
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    if extra:
        headers.update(extra)
    return headers


def probe_url(
    url: str, user_agent: str = DEFAULT_UA, trust_system_ca: bool = True
) -> ProbeResult:
    """探测目标链接。

    先发 HEAD；若 HEAD 被拒绝（405/501）、缺少长度或没有 Accept-Ranges，
    再用 ``GET Range: bytes=0-0`` 复核一次。只有实测到 206 才认定支持分片。
    ``trust_system_ca`` 为真时按系统证书库校验 TLS（与浏览器一致）。
    """
    headers = _browser_headers(user_agent)
    total: Optional[int] = None
    accept_ranges = ""
    status = 0
    final_url = url
    suggested: Optional[str] = None

    with build_http_session(trust_system_ca, headers) as session:
        # 第一步：HEAD
        try:
            head = session.head(url, headers=headers, allow_redirects=True,
                                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            status = head.status_code
            final_url = head.url or url
            total = parse_int(head.headers.get("Content-Length"))
            accept_ranges = (head.headers.get("Accept-Ranges") or "").strip().lower()
            suggested = filename_from_content_disposition(head.headers.get("Content-Disposition"))
            head.close()
        except requests.RequestException:
            status = 0

        # HEAD 不可用 / 信息不足时，用 0-0 字节范围请求复核
        need_probe = status not in (200, 206) or total is None or accept_ranges != "bytes"
        if need_probe:
            resp = None
            try:
                resp = session.get(
                    url,
                    headers=_browser_headers(user_agent, {"Range": "bytes=0-0"}),
                    allow_redirects=True,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
                final_url = resp.url or final_url
                suggested = filename_from_content_disposition(
                    resp.headers.get("Content-Disposition")
                ) or suggested
                if resp.status_code == 206:
                    parsed = parse_content_range(resp.headers.get("Content-Range"))
                    if parsed and parsed[2] is not None:
                        total = parsed[2]
                    return ProbeResult(
                        final_url=final_url,
                        status_code=206,
                        total_size=total,
                        supports_range=True,
                        reason="GET Range: bytes=0-0 返回 206，支持分片",
                        filename=suggested,
                    )
                if resp.status_code == 200:
                    total = parse_int(resp.headers.get("Content-Length")) or total
                    return ProbeResult(
                        final_url=final_url,
                        status_code=200,
                        total_size=total,
                        supports_range=False,
                        reason="Range 请求返回 200，服务端忽略 Range",
                        filename=suggested,
                    )
                status = resp.status_code
            except requests.RequestException as exc:
                return ProbeResult(
                    final_url=final_url,
                    status_code=status,
                    total_size=total,
                    supports_range=False,
                    reason=f"探测失败：{exc}",
                    filename=suggested,
                )
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

    supports = accept_ranges == "bytes" and total is not None
    return ProbeResult(
        final_url=final_url,
        status_code=status,
        total_size=total,
        supports_range=supports,
        reason="HEAD 声明 Accept-Ranges: bytes" if supports else "HEAD 未声明支持 Range",
        filename=suggested,
    )


# --------------------------------------------------------------------------- #
# 下载引擎（与界面完全解耦，可单独在线程里运行）
# --------------------------------------------------------------------------- #

class DownloadEngine:
    """核心下载引擎。

    生命周期：``start()`` 启动后台线程 → ``run()`` 内部完成探测、分片、异步
    下载、同步兜底、合并；``cancel()`` 请求停止并保留断点。
    """

    def __init__(
        self,
        url: str,
        output: str,
        concurrency: Optional[int] = None,
        shards: int = DEFAULT_SHARDS,
        chunk_size: Optional[int] = None,
        retry: int = DEFAULT_RETRY,
        user_agent: str = DEFAULT_UA,
        trust_system_ca: bool = True,
    ) -> None:
        if not url.lower().startswith(("http://", "https://")):
            raise DownloadError("下载链接必须以 http:// 或 https:// 开头")
        if shards < 1:
            raise DownloadError("分片数必须 >= 1")
        if retry < 0:
            raise DownloadError("重试次数不能为负数")

        self.url = url
        self.output = Path(output).expanduser().absolute()
        self.shards = int(shards)
        # 并发连接数：默认 32，允许大于分片数——多出来的连接会把分片再切成
        # 子段并发下载，保证起手和收尾阶段都能吃满带宽。
        if concurrency is None:
            self.concurrency = min(DEFAULT_CONCURRENCY, MAX_CONCURRENCY)
        else:
            self.concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY))
        self.chunk_size = int(chunk_size) if chunk_size else None
        self.retry = int(retry)
        self.user_agent = user_agent or DEFAULT_UA
        # 是否按系统证书库校验 HTTPS（与浏览器一致；关闭则只用 certifi）
        self.trust_system_ca = bool(trust_system_ca)

        # 回调：kind ∈ {"info", "degrade", "error", "done", "stopped"}
        self.on_event: Callable[[str, str], None] = lambda kind, text: None

        # 运行状态
        self._stop = threading.Event()
        self._range_ignored = threading.Event()
        self._report_stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # 进度计数
        self._done_lock = threading.Lock()
        self._done = 0
        self._total = 0
        self._speed = 0.0
        self._speed_lock = threading.Lock()

        # 同步兜底阶段的错误
        self._error_lock = threading.Lock()
        self._errors: List[Tuple[int, BaseException]] = []

        # 每个兜底线程独享一个 requests.Session（线程局部复用）
        self._tls = threading.local()

        # 本次任务的规划 / 是否已走整体单连接模式
        self._plan: Optional[DownloadPlan] = None
        self._whole_mode = False
        self._last_probe: Optional[ProbeResult] = None

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """在后台线程启动下载，立即返回。"""
        if self._thread and self._thread.is_alive():
            raise DownloadError("任务正在运行中")
        self._thread = threading.Thread(target=self.run, name="download-engine", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """请求停止；已完成分片与临时文件会保留。"""
        self._stop.set()

    def is_running(self) -> bool:
        return self._running or bool(self._thread and self._thread.is_alive())

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def snapshot(self) -> Tuple[int, int, float]:
        """返回 (已下载字节, 总字节, 瞬时速度)，供界面轮询。"""
        with self._done_lock:
            done = self._done
        with self._speed_lock:
            speed = self._speed
        return done, self._total, speed

    @property
    def last_probe(self) -> Optional[ProbeResult]:
        """最近一次探测结果；界面据此按文件大小推算最高分片数。"""
        return self._last_probe

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    def _format_error(self, exc: BaseException) -> str:
        """给证书类错误补一条可操作的提示。"""
        text = f"下载失败：{exc}"
        lowered = str(exc).lower()
        if "certificate" in lowered or "ssl" in lowered:
            text += (
                "\n提示：证书校验失败。若本机装有公司代理、杀毒软件或加速器"
                "（它们会替换 HTTPS 证书），请在右上角“设置”里勾选"
                "“信任 Windows 证书库”后重试。"
            )
        return text
    def run(self) -> None:
        """下载主流程（运行在后台线程中）。"""
        self._running = True
        reporter = threading.Thread(target=self._report_loop, name="speed-report", daemon=True)
        reporter.start()
        try:
            self._emit("info", "正在探测目标链接 ...")
            probe = probe_url(self.url, self.user_agent, self.trust_system_ca)
            self._last_probe = probe
            self._emit(
                "info",
                f"探测完成：HTTP {probe.status_code}，总大小 {format_bytes(probe.total_size or 0)}，"
                f"Range {'支持' if probe.supports_range else '不支持'}（{probe.reason}）",
            )

            # 拿不到总长度：无法分片，只能整体流式下载
            if not probe.total_size:
                self._emit("degrade", "无法获取文件总大小，改为单连接整体下载（不支持断点续传）")
                self._download_whole_sync(probe)
                self._emit("done", f"下载完成：{self.output}")
                return

            plan = self._build_plan(probe)
            plan = self._adopt_existing_layout(plan)
            self._plan = plan
            self._prepare_parts(plan)
            self._total = plan.total_size
            pending = [shard for shard in plan.shards if not self._is_part_complete(shard)]
            self._init_progress(plan, pending)
            sub = self._sub_connections(max(1, len(pending)))
            self._emit(
                "info",
                f"共 {len(plan.shards)} 个分片，并发连接 {self.concurrency} 条"
                f"（每片最多 {sub} 条子连接，总量不超过上限）",
            )

            if pending:
                failed = self._run_async_stage(pending)
                if self._stop.is_set():
                    raise DownloadCancelled()
                if failed:
                    self._run_sync_stage(failed)
                if self._stop.is_set():
                    raise DownloadCancelled()

            # 整体单连接模式已经直接产出目标文件，无需分片合并
            if self._whole_mode:
                self._emit("done", f"下载完成：{self.output}")
                return

            self._ensure_all_complete(plan)
            self._merge(plan)
            self._emit("done", f"下载完成：{self.output}（{format_bytes(plan.total_size)}）")
        except DownloadCancelled:
            self._emit("stopped", "已停止，临时分片已保留，下次可续传")
        except Exception as exc:  # noqa: BLE001 - 兜底展示给界面
            self._emit("error", self._format_error(exc))
        finally:
            self._running = False
            self._report_stop.set()
            reporter.join(timeout=2.0)

    # ------------------------------------------------------------------ #
    # 规划与断点
    # ------------------------------------------------------------------ #

    def _build_plan(self, probe: ProbeResult) -> DownloadPlan:
        """按「分片数优先」计算分片大小，并生成所有分片区间。"""
        total = int(probe.total_size or 0)
        if self.chunk_size:
            chunk = max(MIN_CHUNK_SIZE, self.chunk_size)
        else:
            chunk = max(MIN_CHUNK_SIZE, math.ceil(total / max(1, self.shards)))
        count = max(1, math.ceil(total / chunk))
        # 均分一下，避免最后一片过小
        chunk = math.ceil(total / count)

        shards: List[Shard] = []
        for index in range(count):
            start = index * chunk
            end = min(total, start + chunk) - 1
            shards.append(Shard(index=index, start=start, end=end))
        return DownloadPlan(url=self.url, output=self.output, total_size=total,
                            chunk_size=chunk, shards=shards)

    @property
    def parts_dir(self) -> Path:
        """临时文件目录：``<输出文件>.parts/``，避免把下载目录弄乱。"""
        return self.output.with_name(self.output.name + ".parts")

    @property
    def _meta_path(self) -> Path:
        return self.parts_dir / "meta.json"

    def _part_path(self, index: int) -> Path:
        return self.parts_dir / f"part.{index}"

    def _migrate_legacy_parts(self) -> None:
        """把旧版本散落在输出目录里的临时文件挪进 ``.parts`` 子目录。"""
        legacy_meta = self.output.with_name(self.output.name + ".meta.json")
        if legacy_meta.exists() and not self._meta_path.exists():
            shutil.move(str(legacy_meta), str(self._meta_path))
        for old in self.output.parent.glob(self.output.name + ".part.*"):
            suffix = old.name.rsplit(".", 1)[-1]
            if not suffix.isdigit():
                continue
            target = self._part_path(int(suffix))
            if not target.exists():
                shutil.move(str(old), str(target))

    def _adopt_existing_layout(self, plan: DownloadPlan) -> DownloadPlan:
        """续传时优先沿用上次任务的分片布局。

        用户改了分片数/分片大小再来续传是常事（界面还会记住上次的数值），
        如果直接按新参数重算，旧分片就对不上偏移，只能报错让人手动删除。
        只要下载地址、输出路径和文件总大小一致，就沿用 meta 里的布局继续下，
        避免“改了个参数就断点全废”。
        """
        if not self._meta_path.exists():
            inferred = self._infer_layout_from_parts(plan)
            if inferred is not None:
                return inferred
            if self._has_plain_parts():
                # 有分片却没有元数据，且反推不出唯一布局：宁可停下，也不要拼出错文件
                raise DownloadError(
                    "临时目录里有分片文件，但缺少 meta.json，无法确定上次的分片布局。\n"
                    f"继续下载可能拼出错误的文件，已停止。\n"
                    f"请手动删除临时目录 {self.parts_dir.name} 后重新下载。"
                )
            return plan
        try:
            raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
            meta = TaskMeta(**{k: raw.get(k) for k in
                               ("url", "output", "total_size", "chunk_size", "shard_count", "version")})
        except (OSError, ValueError, TypeError, AttributeError):
            # 元数据损坏交给 _prepare_parts 统一报错
            return plan

        if meta.url != self.url or Path(meta.output) != self.output:
            return plan
        if not meta.total_size or meta.total_size != plan.total_size:
            return plan
        if not meta.chunk_size or meta.chunk_size <= 0 or not meta.shard_count:
            return plan

        same_layout = (
            meta.chunk_size == plan.chunk_size and meta.shard_count == len(plan.shards)
        )
        if same_layout:
            return plan

        # 用旧布局重建分片，偏移必须和当初完全一致
        total = meta.total_size
        count = int(meta.shard_count)
        if count != math.ceil(total / meta.chunk_size):
            return plan
        return self._rebuild_plan(
            plan,
            meta.chunk_size,
            count,
            f"检测到未完成的旧任务，沿用其分片布局：{count} 个分片 × "
            f"{format_bytes(meta.chunk_size)}，继续断点续传",
        )

    def _infer_layout_from_parts(self, plan: DownloadPlan) -> Optional[DownloadPlan]:
        """没有元数据时，用残留分片的实际大小反推上次的分片布局。

        正常情况下 meta.json 会一直在；但用户手动删过、或磁盘清理动过目录后，
        只剩一堆 ``part.N``。这时如果按新参数硬算偏移，合并出来的文件必然是错的，
        所以只在“编号从 0 连续、除最后一片外大小完全一致”这种无歧义的情况下
        沿用旧布局；反推不出来时返回 None，由调用方决定是报错还是照常开始。
        """
        pieces: List[Tuple[int, Path]] = []
        try:
            for path in self.parts_dir.glob("part.*"):
                suffix = path.name[len("part."):]
                if suffix.isdigit():
                    pieces.append((int(suffix), path))
        except OSError:
            return None
        if not pieces:
            return None
        pieces.sort()
        if [index for index, _ in pieces] != list(range(len(pieces))):
            return None

        try:
            sizes = [path.stat().st_size for _, path in pieces]
        except OSError:
            return None
        chunk = sizes[0]
        total = plan.total_size
        if chunk <= 0 or chunk > total:
            return None
        # 除最后一片外必须都已下满，且最后一片不超过分片大小
        if any(size != chunk for size in sizes[:-1]) or sizes[-1] > chunk:
            return None
        if math.ceil(total / chunk) != len(pieces):
            return None
        return self._rebuild_plan(
            plan,
            chunk,
            len(pieces),
            f"临时元数据缺失，按残留分片反推布局：{len(pieces)} 个分片 × "
            f"{format_bytes(chunk)}，继续断点续传",
        )

    def _has_plain_parts(self) -> bool:
        """临时目录里是否存在 ``part.N`` 形式的分片（不含子段临时文件）。"""
        try:
            for path in self.parts_dir.glob("part.*"):
                if path.name[len("part."):].isdigit():
                    return True
        except OSError:
            return False
        return False

    def _rebuild_plan(
        self,
        plan: DownloadPlan,
        chunk: int,
        count: int,
        message: str,
    ) -> DownloadPlan:
        """按给定的分片大小/数量重建分片表（偏移与旧任务保持一致）。"""
        total = plan.total_size
        shards: List[Shard] = []
        for index in range(count):
            start = index * chunk
            end = min(total, start + chunk) - 1
            shards.append(Shard(index=index, start=start, end=end))
        self.shards = count
        self.chunk_size = chunk
        self._emit("info", message)
        return DownloadPlan(url=self.url, output=self.output, total_size=total,
                            chunk_size=chunk, shards=shards)

    def _prepare_parts(self, plan: DownloadPlan) -> None:
        """校验/创建任务元数据，确保断点属于同一任务。"""
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_parts()
        if self._meta_path.exists():
            try:
                raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
                meta = TaskMeta(**{k: raw.get(k) for k in
                                   ("url", "output", "total_size", "chunk_size", "shard_count", "version")})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise DownloadError(f"临时元数据损坏：{self._meta_path}（{exc}）") from exc
            if not meta.matches(self.url, self.output, plan.total_size,
                                plan.chunk_size, len(plan.shards)):
                # 布局差异已经在 _adopt_existing_layout 里消化掉了，走到这里都是硬冲突
                if meta.url != self.url or Path(meta.output) != self.output:
                    raise DownloadError(
                        "临时文件属于另一个下载任务（下载地址或保存路径不同）。\n"
                        f"元数据：{self._meta_path}\n"
                        f"请换一个输出文件名，或手动删除临时目录 {self.parts_dir.name} 后重试。"
                    )
                if meta.total_size != plan.total_size:
                    raise DownloadError(
                        "服务器上的文件已经变化，无法继续上次的断点：\n"
                        f"上次总大小 {format_bytes(meta.total_size)}，"
                        f"现在 {format_bytes(plan.total_size)}。\n"
                        f"请手动删除临时目录 {self.parts_dir.name} 后重新下载。"
                    )
                raise DownloadError(
                    "检测到与当前任务不一致的临时文件，为避免拼错文件已停止。\n"
                    f"元数据：{self._meta_path}\n"
                    f"请换一个输出文件名，或手动删除临时目录 {self.parts_dir.name} 后重试。"
                )
            return

        # 没有元数据但残留分片：只接受大小不超过预期的情况，否则视为异常
        for shard in plan.shards:
            part = self._part_path(shard.index)
            if part.exists() and part.stat().st_size > shard.size:
                raise DownloadError(
                    f"临时分片 {part.name} 大小异常（{part.stat().st_size} > {shard.size}），"
                    "请更换输出路径或手动清理后重试。"
                )

        meta = TaskMeta(url=self.url, output=str(self.output), total_size=plan.total_size,
                        chunk_size=plan.chunk_size, shard_count=len(plan.shards))
        tmp = self._meta_path.with_suffix(self._meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(meta.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._meta_path)

    def _init_progress(self, plan: DownloadPlan, pending: List[Shard]) -> None:
        """把已存在的分片字节数计入进度，并提示断点续传。"""
        done = 0
        for shard in plan.shards:
            part = self._part_path(shard.index)
            if part.exists():
                done += min(part.stat().st_size, shard.size)
        with self._done_lock:
            self._done = done
        if done and pending:
            finished = len(plan.shards) - len(pending)
            if finished:
                self._emit(
                    "info",
                    f"发现断点：{finished}/{len(plan.shards)} 个分片已完成，"
                    f"继续剩余 {len(pending)} 个分片",
                )
            else:
                self._emit(
                    "info",
                    f"发现断点：已有 {format_bytes(done)} 临时数据，"
                    f"继续剩余 {len(pending)} 个分片",
                )
        elif done and not pending:
            self._emit("info", "所有分片均已存在，直接合并")

    def _is_part_complete(self, shard: Shard) -> bool:
        part = self._part_path(shard.index)
        try:
            return part.exists() and part.stat().st_size == shard.size
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    # 异步阶段
    # ------------------------------------------------------------------ #

    def _run_async_stage(self, pending: List[Shard]) -> List[Shard]:
        """用 aiohttp 并发下载分片，返回仍然失败、需要同步兜底的分片。"""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._async_main(pending))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                asyncio.set_event_loop(None)

    async def _async_main(self, pending: List[Shard]) -> List[Shard]:
        # 信任系统证书时用默认上下文（Windows 下含系统根证书）；
        # 关闭时退回 certifi 的根证书包
        ssl_arg: object = True
        if not self.trust_system_ca:
            ssl_arg = ssl.create_default_context(cafile=requests.certs.where())
        connector = aiohttp.TCPConnector(
            limit=self.concurrency + 4,
            limit_per_host=self.concurrency + 4,
            ttl_dns_cache=300,
            ssl=ssl_arg,
        )
        timeout = aiohttp.ClientTimeout(
            total=None, connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT
        )
        headers = _browser_headers(self.user_agent)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers,
            raise_for_status=False,
        ) as session:
            # 先用一个 0 字节范围请求确认服务端是否真的按 Range 返回 206
            supported = await self._probe_async(session)
            if supported is False:
                self._range_ignored.set()
                return [shard for shard in pending if not self._is_part_complete(shard)]

            semaphore = asyncio.Semaphore(self.concurrency)
            sub = self._sub_connections(len(pending))
            tasks = [
                asyncio.create_task(
                    self._download_async_shard(session, semaphore, shard, sub)
                )
                for shard in pending
            ]
            watcher = asyncio.create_task(self._stop_watcher(tasks))
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                watcher.cancel()

        if self._stop.is_set():
            raise DownloadCancelled()

        failed: List[Shard] = []
        for shard, result in zip(pending, results):
            if result is None:
                continue
            if isinstance(result, RangeIgnored):
                self._range_ignored.set()
                continue
            if isinstance(result, DownloadCancelled):
                continue
            failed.append(shard)

        if self._range_ignored.is_set():
            return [shard for shard in pending if not self._is_part_complete(shard)]
        return failed

    async def _probe_async(self, session: aiohttp.ClientSession) -> Optional[bool]:
        """异步复核 Range：True=206，False=200，None=无法判断。

        CDN 偶尔会抽风返回一个 200（过载节点/缓存穿透），这里重试两次，
        避免一次偶发响应就把整个任务判定成“不支持 Range”。
        """
        for attempt in range(3):
            try:
                async with session.get(self.url, headers={"Range": "bytes=0-0"}) as resp:
                    if resp.status == 206:
                        return True
                    if resp.status == 200:
                        if attempt < 2:
                            await asyncio.sleep(0.6)
                            continue
                        return False
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(0.6)
                    continue
                return None
        return None

    def _looks_like_full_body(self, headers: Mapping[str, str]) -> bool:
        """判断一个 200 响应是不是“整份文件”（即服务端真的忽略了 Range）。

        只有长度对得上总大小（或者没有长度）时才认定忽略 Range；否则更可能是
        CDN 节点偶发返回的错误 200，交给重试处理。
        """
        total = self._last_probe.total_size if self._last_probe else None
        length = headers.get("Content-Length")
        if not total:
            return True
        if not length or not str(length).isdigit():
            return True
        return int(length) >= total

    async def _stop_watcher(self, tasks: List["asyncio.Task"]) -> None:
        """轮询停止标记，一旦用户取消就尽快取消所有分片任务。"""
        while not self._stop.is_set():
            await asyncio.sleep(PROGRESS_INTERVAL)
        for task in tasks:
            task.cancel()

    def _sub_connections(self, active: int) -> int:
        """按“当前还有几个分片在排队”换算每个分片可以开几条子连接。

        记录里的并发数只是总量上限，真正的连接由 ``asyncio.Semaphore`` 控制；
        这里把名额平均分给在跑的分片，尾部只剩一两个分片时它们能拿到更多
        子连接，避免收尾阶段只剩一两条连接低速爬行。
        """
        if active <= 0:
            return 1
        per_shard = math.ceil(self.concurrency / active)
        return max(1, min(MAX_SUB_CONNECTIONS, per_shard))

    async def _download_async_shard(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        shard: Shard,
        sub: int = 1,
    ) -> None:
        """下载单个分片：连接名额够时再切成 ``sub`` 个子段并发下载。

        子段先各写一个临时文件，全部成功后再按顺序拼成 ``part.N``——这样
        “part 文件大小 == 分片大小”这一断点判定依然成立，中断后仍能续传。
        """
        part = self._part_path(shard.index)
        size = self._part_size(part)
        if size == shard.size:
            return  # 已经下完（可能由上一轮遗留）
        if size > shard.size:
            raise DownloadError(f"分片 {shard.index} 临时文件大小异常")
        if size > 0:
            # 已有断点数据：退回单连接按偏移续传，避免把已下载的字节重下
            sub = 1

        if sub <= 1:
            await self._download_async_sequential(session, semaphore, shard)
            return

        ranges = split_range(shard.start, shard.end, sub)
        # 临时文件名里带上区间，换个并发数重启时不会把旧文件错当成新区间的一半
        temps = [
            part.with_name(f"{part.name}.{index}_{start}_{end}")
            for index, (start, end) in enumerate(ranges)
        ]
        try:
            results = await asyncio.gather(
                *(
                    self._fetch_subrange(session, semaphore, shard, start, end, temp)
                    for (start, end), temp in zip(ranges, temps)
                ),
                return_exceptions=True,
            )
            errors = [item for item in results if isinstance(item, BaseException)]
            for error in errors:
                if isinstance(error, (RangeIgnored, DownloadCancelled)):
                    raise error
                if isinstance(error, asyncio.CancelledError):
                    raise DownloadCancelled()
            if errors:
                first = errors[0]
                if isinstance(first, ShardFailed):
                    raise first
                raise ShardFailed(shard.index, first)
            self._join_subparts(shard, temps, part)
        finally:
            for temp in temps:
                try:
                    temp.unlink()
                except OSError:
                    pass

    def _join_subparts(self, shard: Shard, temps: List[Path], part: Path) -> None:
        """把子段临时文件按顺序拼成分片文件，并校验总长度。

        每拼完一个子段就删掉它，避免磁盘上同时存在“半成品分片 + 剩余子段”
        导致进度统计瞬间翻倍。
        """
        with open(part, "wb") as target:
            for temp in temps:
                with open(temp, "rb") as source:
                    shutil.copyfileobj(source, target, COPY_BUFFER)
                try:
                    temp.unlink()
                except OSError:
                    pass
        actual = self._part_size(part)
        if actual != shard.size:
            raise ShardFailed(
                shard.index,
                DownloadError(f"子段拼接后长度不符：{actual} != {shard.size}"),
            )

    async def _fetch_subrange(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        shard: Shard,
        start: int,
        end: int,
        temp: Path,
    ) -> None:
        """下载分片内的一个子区间。

        这里刻意不“一条连接下到底”，而是把子区间再切成 ``PIECE_SIZE`` 大小
        的小块：每块用完就换一条新连接。很多 CDN 会让单条长连接越下越慢
        （尤其收尾阶段），换新连接能重新拿到一个快的出口；同时也天然支持
        断点续传（临时文件已有多少字节，就从哪里接着要）。
        """
        last_error: Optional[BaseException] = None

        while True:
            self._check_stop()
            if self._range_ignored.is_set():
                raise RangeIgnored()

            # 以临时文件的实际大小为准计算断点，失败重试时能接着下
            pos = start + self._temp_size(temp)
            if pos > end + 1:
                raise DownloadError(f"子段临时文件超出范围：{pos} > {end + 1}")
            if pos == end + 1:
                return

            piece_end = min(pos + PIECE_SIZE - 1, end)
            last_error = None
            progressed = False

            for attempt in range(1, self.retry + 2):  # 总尝试次数 = retry + 1
                self._check_stop()
                try:
                    async with semaphore:
                        async with session.get(
                            self.url, headers={"Range": f"bytes={pos}-{piece_end}"}
                        ) as response:
                            if response.status == 206:
                                self._verify_content_range(
                                    response.headers.get("Content-Range"), pos
                                )
                                written = await self._write_async_stream(
                                    response, temp, limit=piece_end - pos + 1
                                )
                                progressed = written > 0
                                break
                            if response.status == 200:
                                if self._looks_like_full_body(response.headers):
                                    self._range_ignored.set()
                                    raise RangeIgnored()
                                raise DownloadError("HTTP 200（Range 未生效，稍后重试）")
                            raise DownloadError(f"HTTP {response.status}")
                except (RangeIgnored, DownloadCancelled):
                    raise
                except (DownloadError, asyncio.TimeoutError, aiohttp.ClientError,
                        ConnectionError, OSError) as exc:
                    last_error = exc
                    if attempt <= self.retry:
                        await asyncio.sleep(min(2 ** (attempt - 1), 5))
            else:
                raise ShardFailed(shard.index, last_error)

            if not progressed:
                # 拿到了 206 却一个字节都没写进来，避免死循环
                raise ShardFailed(shard.index, DownloadError("子段连接无数据返回"))

    async def _download_async_sequential(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        shard: Shard,
    ) -> None:
        """单连接顺序下载一个分片（支持按已有字节续传）。

        与子段下载同样的思路：每次只请求 ``PIECE_SIZE`` 那么多字节，下完这一块
        就关掉连接、换一条新的继续，避免一条长连接越走越慢把收尾拖死。
        """
        part = self._part_path(shard.index)
        last_error: Optional[BaseException] = None

        while True:
            self._check_stop()
            if self._range_ignored.is_set():
                raise RangeIgnored()

            current = self._part_size(part)
            if current > shard.size:
                raise DownloadError(f"分片 {shard.index} 临时文件大小异常")
            if current == shard.size:
                return  # 已经下完（可能由上一轮遗留）

            start = shard.start + current
            piece_end = min(start + PIECE_SIZE - 1, shard.end)
            last_error = None
            progressed = False

            for attempt in range(1, self.retry + 2):  # 总尝试次数 = retry + 1
                self._check_stop()
                try:
                    # 信号量只包住真正的网络请求，退避等待不占连接名额
                    async with semaphore:
                        async with session.get(
                            self.url, headers={"Range": f"bytes={start}-{piece_end}"}
                        ) as response:
                            if response.status == 206:
                                self._verify_content_range(
                                    response.headers.get("Content-Range"), start
                                )
                                written = await self._write_async_stream(
                                    response, part, limit=piece_end - start + 1
                                )
                                progressed = written > 0
                                break
                            if response.status == 200:
                                # 只有真的是“整份文件”才整体降级；否则当作偶发错误重试
                                if self._looks_like_full_body(response.headers):
                                    self._range_ignored.set()
                                    raise RangeIgnored()
                                raise DownloadError("HTTP 200（Range 未生效，稍后重试）")
                            if response.status == 416:
                                if self._part_size(part) == shard.size:
                                    return
                                raise DownloadError("HTTP 416，远程文件可能已变更，请重新下载")
                            raise DownloadError(f"HTTP {response.status}")
                except (RangeIgnored, DownloadCancelled):
                    raise
                except (DownloadError, asyncio.TimeoutError, aiohttp.ClientError,
                        ConnectionError, OSError) as exc:
                    last_error = exc
                    if attempt <= self.retry:
                        await asyncio.sleep(min(2 ** (attempt - 1), 5))
            else:
                raise ShardFailed(shard.index, last_error)

            if not progressed:
                raise ShardFailed(shard.index, DownloadError("分片连接无数据返回"))

    async def _write_async_stream(
        self,
        response: aiohttp.ClientResponse,
        part: Path,
        limit: Optional[int] = None,
    ) -> int:
        """把响应体追加写入临时文件，最多写 ``limit`` 个字节，返回实际写入量。"""
        mode = "ab" if part.exists() and part.stat().st_size > 0 else "wb"
        written = 0
        # 写盘是本地小块操作，放在事件循环里执行影响很小，故不额外开线程
        with open(part, mode) as file_obj:
            async for chunk in response.content.iter_chunked(WRITE_CHUNK):
                self._check_stop()
                if self._range_ignored.is_set():
                    raise RangeIgnored()
                if limit is not None and written + len(chunk) > limit:
                    chunk = chunk[: limit - written]
                file_obj.write(chunk)
                written += len(chunk)
                self._add_progress(len(chunk))
                if limit is not None and written >= limit:
                    break
        return written

    def _verify_content_range(self, header: Optional[str], expected_start: int) -> None:
        """校验 206 响应的起始偏移，防止服务端返回错位数据。"""
        parsed = parse_content_range(header)
        if parsed is None or parsed[0] != expected_start:
            raise RangeMismatch(
                f"Content-Range 校验失败：期望起始 {expected_start}，实际 {header!r}"
            )

    # ------------------------------------------------------------------ #
    # 同步兜底阶段
    # ------------------------------------------------------------------ #

    def _run_sync_stage(self, failed: List[Shard]) -> None:
        """异步失败后的同步兜底。

        若服务端根本不支持 Range，则整体降级为单连接同步下载；否则每个失败
        分片单独开一个线程，用 requests 重新下载。
        """
        if self._range_ignored.is_set() or not self._probe_sync_range():
            self._emit(
                "degrade",
                "服务端不支持 Range 分片，整体降级为 requests + threading 单连接同步下载",
            )
            self._download_whole_sync(self._last_probe)
            return

        self._emit("degrade", f"{len(failed)} 个分片转 requests + threading 同步线程兜底")
        with self._error_lock:
            self._errors.clear()

        threads: List[threading.Thread] = []
        for shard in failed:
            self._emit(
                "degrade",
                f"分片 {shard.index} 异步重试 {self.retry} 次仍失败，切换同步线程兜底",
            )
            thread = threading.Thread(
                target=self._sync_shard_guarded,
                args=(shard,),
                name=f"fallback-{shard.index}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        if self._stop.is_set():
            raise DownloadCancelled()
        with self._error_lock:
            errors = list(self._errors)
        if errors:
            first = errors[0][1]
            raise DownloadError(
                f"{len(errors)} 个分片同步兜底仍然失败（首个：分片 {errors[0][0]}：{first}）"
            )

    def _probe_sync_range(self) -> bool:
        """同步侧再确认一次 Range 是否可用。"""
        try:
            with build_http_session(self.trust_system_ca) as session:
                resp = session.get(
                    self.url,
                    headers=_browser_headers(self.user_agent, {"Range": "bytes=0-0"}),
                    allow_redirects=True,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
                try:
                    return resp.status_code == 206 and bool(resp.headers.get("Content-Range"))
                finally:
                    resp.close()
        except requests.RequestException:
            return False

    def _thread_session(self) -> requests.Session:
        """每个兜底线程复用自己的 requests.Session，避免重复握手。"""
        session = getattr(self._tls, "session", None)
        if session is None:
            session = build_http_session(
                self.trust_system_ca, _browser_headers(self.user_agent)
            )
            self._tls.session = session
        return session

    def _sync_shard_guarded(self, shard: Shard) -> None:
        """线程入口：捕获异常并记录，避免线程静默退出。"""
        try:
            self._download_sync_shard(shard)
        except DownloadCancelled:
            pass
        except Exception as exc:  # noqa: BLE001 - 统一汇总后抛出
            with self._error_lock:
                self._errors.append((shard.index, exc))

    def _download_sync_shard(self, shard: Shard) -> None:
        """用 requests 同步下载一个分片（整片重做/续写临时文件）。"""
        session = self._thread_session()
        part = self._part_path(shard.index)
        last_error: Optional[BaseException] = None

        for attempt in range(1, self.retry + 2):
            self._check_stop()
            current = self._part_size(part)
            if current > shard.size:
                raise DownloadError(f"分片 {shard.index} 临时文件大小异常")
            if current == shard.size:
                return

            start = shard.start + current
            try:
                response = session.get(
                    self.url,
                    headers={"Range": f"bytes={start}-{shard.end}"},
                    stream=True,
                    allow_redirects=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
                try:
                    if response.status_code == 206:
                        self._verify_content_range(response.headers.get("Content-Range"), start)
                        mode = "ab" if current > 0 else "wb"
                        with open(part, mode) as file_obj:
                            for chunk in response.iter_content(WRITE_CHUNK):
                                self._check_stop()
                                if not chunk:
                                    continue
                                file_obj.write(chunk)
                                self._add_progress(len(chunk))
                        return
                    if response.status_code == 200:
                        self._range_ignored.set()
                        raise RangeIgnored()
                    if response.status_code == 416:
                        if self._part_size(part) == shard.size:
                            return
                        raise DownloadError("HTTP 416，远程文件可能已变更")
                    raise DownloadError(f"HTTP {response.status_code}")
                finally:
                    response.close()
            except RangeIgnored:
                raise
            except DownloadCancelled:
                raise
            except (DownloadError, requests.RequestException, OSError) as exc:
                last_error = exc
                if attempt <= self.retry:
                    self._stop.wait(min(2 ** (attempt - 1), 5))

        raise ShardFailed(shard.index, last_error)

    # ------------------------------------------------------------------ #
    # 不分片 / 整体下载
    # ------------------------------------------------------------------ #

    def _download_whole_sync(self, probe_result: Optional[ProbeResult]) -> None:
        """无法分片时，用一条连接整体流式下载到临时文件再替换。"""
        temp = self.parts_dir / "single.part"
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self._reset_progress()
        if probe_result and probe_result.total_size:
            self._total = probe_result.total_size

        headers = _browser_headers(self.user_agent)
        try:
            with build_http_session(self.trust_system_ca, headers) as session:
                response = session.get(
                    self.url, headers=headers, stream=True, allow_redirects=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
                try:
                    if response.status_code not in (200, 206):
                        raise DownloadError(f"HTTP {response.status_code}")
                    total = parse_int(response.headers.get("Content-Length")) or 0
                    if total:
                        self._total = total
                    with open(temp, "wb") as file_obj:
                        for chunk in response.iter_content(WRITE_CHUNK):
                            self._check_stop()
                            if not chunk:
                                continue
                            file_obj.write(chunk)
                            self._add_progress(len(chunk))
                finally:
                    response.close()
        except DownloadCancelled:
            raise
        except (DownloadError, requests.RequestException, OSError) as exc:
            raise DownloadError(f"整体下载失败：{exc}") from exc

        os.replace(temp, self.output)
        self._whole_mode = True
        self._cleanup_parts(self._plan)

    # ------------------------------------------------------------------ #
    # 合并
    # ------------------------------------------------------------------ #

    def _ensure_all_complete(self, plan: DownloadPlan) -> None:
        missing = [shard.index for shard in plan.shards if not self._is_part_complete(shard)]
        if missing:
            preview = ", ".join(str(i) for i in missing[:10])
            raise DownloadError(f"仍有 {len(missing)} 个分片未完成（{preview} ...）")

    def _merge(self, plan: DownloadPlan) -> None:
        """按偏移顺序合并分片，原子替换目标文件，再清理临时文件。"""
        self._emit("info", "正在合并分片 ...")
        merging = self.parts_dir / "merging"
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(merging, "wb") as output_file:
                for shard in plan.shards:
                    with open(self._part_path(shard.index), "rb") as part_file:
                        shutil.copyfileobj(part_file, output_file, COPY_BUFFER)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(merging, self.output)
        finally:
            try:
                if merging.exists():
                    merging.unlink()
            except OSError:
                pass

        self._cleanup_parts(plan)

    def _cleanup_parts(self, plan: Optional[DownloadPlan]) -> None:
        """删除本次任务的分片与元数据；失败只提示，不影响下载结果。"""
        if plan is not None:
            for shard in plan.shards:
                try:
                    self._part_path(shard.index).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    self._emit("info", f"临时分片 {shard.index} 清理失败：{exc}")
        try:
            self._meta_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._emit("info", f"元数据清理失败：{exc}")
        # 顺带清掉本目录里我们自己留下的其它临时文件（整体模式的 single.part、
        # 合并中的 merging、子段 part.N.k）。只按固定命名匹配，不碰任何无关文件。
        if self.parts_dir.is_dir():
            for stray in self.parts_dir.iterdir():
                if not stray.is_file():
                    continue
                name = stray.name
                if name in {"merging", "single.part", "meta.json"} or name.startswith("part."):
                    try:
                        stray.unlink()
                    except OSError as exc:
                        self._emit("info", f"临时文件 {name} 清理失败：{exc}")
        # 分片与元数据都清干净后，顺手删掉空的 .parts 目录（非空说明还有残留，保留）
        try:
            self.parts_dir.rmdir()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 进度与事件
    # ------------------------------------------------------------------ #

    def _emit(self, kind: str, text: str) -> None:
        try:
            self.on_event(kind, text)
        except Exception:
            pass

    def _add_progress(self, count: int) -> None:
        with self._done_lock:
            self._done += count

    def _reset_progress(self) -> None:
        with self._done_lock:
            self._done = 0

    def _disk_done(self) -> Optional[int]:
        """以临时目录里的实际字节数为准统计进度。

        重试、同步兜底、子段拼接都可能把同一段数据写两次；如果只按写入量累加，
        进度会出现“18 MB / 16 MB”这种超额。这里直接读磁盘，天然自洽。
        读不到目录时返回 None，调用方回退到累加值。
        """
        total = 0
        try:
            for entry in self.parts_dir.iterdir():
                name = entry.name
                if name == "single.part" or name.startswith("part."):
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            return None
        return total

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise DownloadCancelled()

    def _part_size(self, part: Path) -> int:
        try:
            return part.stat().st_size
        except OSError:
            return 0

    def _temp_size(self, temp: Path) -> int:
        """子段临时文件已写入的字节数（用于续传与“换连接”时的偏移计算）。"""
        try:
            return temp.stat().st_size
        except OSError:
            return 0

    def _report_loop(self) -> None:
        """每 0.2 秒采样一次，用 2 秒滑动窗口计算瞬时速度。"""
        samples: deque = deque()
        while not self._report_stop.wait(PROGRESS_INTERVAL):
            now = time.monotonic()
            disk = self._disk_done()
            if disk is None:
                with self._done_lock:
                    current = self._done
            else:
                current = disk
                with self._done_lock:
                    self._done = disk
            samples.append((now, current))
            while len(samples) > 2 and now - samples[0][0] > SPEED_WINDOW:
                samples.popleft()
            if len(samples) >= 2:
                delta_t = samples[-1][0] - samples[0][0]
                delta_b = samples[-1][1] - samples[0][1]
                speed = delta_b / delta_t if delta_t > 0 else 0.0
                with self._speed_lock:
                    self._speed = max(0.0, speed)


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #

class DownloadApp:
    """tkinter 图形界面：唯一对外入口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.engine: Optional[DownloadEngine] = None
        self.events: "queue.Queue[Tuple[str, str]]" = queue.Queue()

        # 读取用户设置：默认保存目录（缺省即系统“下载”目录）
        self.settings = load_settings()
        configured = self.settings.get("download_dir")
        candidate = (
            Path(configured).expanduser()
            if isinstance(configured, str) and configured.strip()
            else system_downloads_dir()
        )
        self.download_dir = candidate if candidate.is_dir() else system_downloads_dir()

        # 记住的界面参数（关闭窗口时写回配置文件）；非法值按默认处理
        self.initial_shards = _as_int(self.settings.get("shards"), DEFAULT_SHARDS, 1, 100000)
        self.initial_concurrency = _as_int(
            self.settings.get("concurrency"), DEFAULT_CONCURRENCY, 1, MAX_CONCURRENCY
        )
        self.initial_retry = _as_int(self.settings.get("retry"), DEFAULT_RETRY, 0, 10)
        self.initial_chunk_mb = self._valid_chunk_mb(self.settings.get("chunk_mb"))
        # 不记忆具体文件名：启动时“保存到”为空，URL 解析后再按目录 + 真实文件名填充
        self._auto_output = ""
        self._resolve_token = 0

        # 分片数上限：先按硬上限展示，探测到文件大小后再按 0.5 MB 最小分片收紧
        self._max_shards = MAX_SHARDS
        self._probe_size_applied = 0

        # 证书：默认信任系统证书库（与浏览器一致），可修复公司代理/杀软替换证书导致的
        # CERTIFICATE_VERIFY_FAILED；关闭则退回 requests/aiohttp 自带的 certifi
        self.trust_system_ca = bool(self.settings.get("trust_system_ca", True))

        self._configure_window()
        self._build_widgets()
        self._poll_events()

    @staticmethod
    def _valid_chunk_mb(value: object) -> str:
        """配置里的“分片大小 MB”只保留正数，非法值按未填写处理。"""
        text = _as_text(value)
        if not text:
            return ""
        try:
            return text if float(text) > 0 else ""
        except ValueError:
            return ""

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #

    def _configure_window(self) -> None:
        self.root.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.root.geometry("880x740")
        self.root.minsize(800, 660)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.root.option_add("*Font", "{Microsoft YaHei UI} 10")
        style.configure("TButton", padding=(10, 4))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Warn.TLabel", foreground="#B26A00")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Percent.TLabel", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Status.TLabel", foreground="#444444")

    def _build_widgets(self) -> None:
        # 状态栏先占住窗口底部，避免被可扩展的主区域挤掉
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(
            self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(10, 4)
        ).pack(side="bottom", fill="x")

        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        ttk.Label(main, text="HighSpeedDownloader", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        # 头部右侧：设置入口（默认保存目录等偏好）
        ttk.Button(main, text="设置", width=8, command=self._open_settings).grid(
            row=0, column=3, sticky="e"
        )
        ttk.Label(
            main,
            text="aiohttp 异步分片 · requests/threading 同步兜底 · 断点续传",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 14))

        # 下载链接
        ttk.Label(main, text="下载链接").grid(row=2, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(main, textvariable=self.url_var)
        url_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(main, text="粘贴", width=7, command=self._paste_url).grid(
            row=2, column=3, padx=(8, 0), pady=4
        )

        # 保存路径：启动时留空，URL 解析后自动填“保存目录 + 真实文件名”
        ttk.Label(main, text="保存到").grid(row=3, column=0, sticky="w", pady=4)
        self.output_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.output_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Button(main, text="浏览...", width=7, command=self._browse_output).grid(
            row=3, column=3, padx=(8, 0), pady=4
        )

        # 参数区
        params = ttk.LabelFrame(main, text="下载参数", padding=12)
        params.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(14, 4))
        # 最后一列只做占位吸收多余宽度，避免控件被拉变形
        params.columnconfigure(7, weight=1)

        self.shards_var = tk.IntVar(value=self.initial_shards)
        self.concurrency_var = tk.IntVar(value=self.initial_concurrency)
        self.retry_var = tk.IntVar(value=self.initial_retry)
        self.chunk_mb_var = tk.StringVar(value=self.initial_chunk_mb)

        ttk.Label(params, text="分片数").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(params, from_=1, to=1024, width=6, textvariable=self.shards_var).grid(
            row=0, column=1, sticky="w", padx=(4, 6)
        )
        # 常驻小字：最高支持分片数（探测到总大小后按 0.5 MB 最小分片收紧）
        self.shard_hint_var = tk.StringVar()
        self.shard_hint_label = ttk.Label(
            params, textvariable=self.shard_hint_var, style="Hint.TLabel"
        )
        self.shard_hint_label.grid(row=0, column=2, sticky="w", padx=(0, 18))

        ttk.Label(params, text="并发连接").grid(row=0, column=3, sticky="w")
        ttk.Spinbox(params, from_=1, to=MAX_CONCURRENCY, width=6,
                    textvariable=self.concurrency_var).grid(
            row=0, column=4, sticky="w", padx=(4, 18)
        )
        ttk.Label(params, text="重试次数").grid(row=0, column=5, sticky="w")
        ttk.Spinbox(params, from_=0, to=10, width=5, textvariable=self.retry_var).grid(
            row=0, column=6, sticky="w", padx=(4, 0)
        )

        # “分片大小”单独一行、用一个子 Frame 承载，避免撑宽上面的列造成控件错位
        chunk_row = ttk.Frame(params)
        chunk_row.grid(row=1, column=0, columnspan=8, sticky="w", pady=(8, 0))
        ttk.Label(chunk_row, text="分片大小 MB（可选）").pack(side="left")
        ttk.Entry(chunk_row, width=8, textvariable=self.chunk_mb_var).pack(
            side="left", padx=(4, 0)
        )
        ttk.Label(
            params,
            text="提示：并发连接可以大于分片数，多余连接会把分片再切成子段，"
                 "起手和收尾也能跑满带宽",
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=8, sticky="w", pady=(8, 0))

        self.shards_var.trace_add("write", lambda *_args: self._refresh_shard_hint())
        self._refresh_shard_hint()

        # 按钮
        buttons = ttk.Frame(main)
        buttons.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(14, 10))
        self.start_button = ttk.Button(buttons, text="开始下载", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            buttons, text="停止", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(10, 0))

        # 进度区
        progress_frame = ttk.LabelFrame(main, text="进度", padding=12)
        progress_frame.grid(row=6, column=0, columnspan=4, sticky="ew")
        progress_frame.columnconfigure(0, minsize=118)
        progress_frame.columnconfigure(1, weight=1)

        self.percent_var = tk.StringVar(value="0.0%")
        ttk.Label(progress_frame, textvariable=self.percent_var, style="Percent.TLabel").grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 16)
        )
        self.progress_bar = ttk.Progressbar(progress_frame, maximum=100.0)
        self.progress_bar.grid(row=0, column=1, sticky="ew", pady=(6, 4))
        self.size_var = tk.StringVar(value="0 B / 0 B")
        ttk.Label(progress_frame, textvariable=self.size_var).grid(
            row=1, column=1, sticky="w"
        )
        info_row = ttk.Frame(progress_frame)
        info_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.speed_var = tk.StringVar(value="速度 0 B/s")
        self.eta_var = tk.StringVar(value="剩余 --:--")
        ttk.Label(info_row, textvariable=self.speed_var, style="Status.TLabel").pack(side="left")
        ttk.Label(info_row, textvariable=self.eta_var, style="Status.TLabel").pack(
            side="left", padx=(24, 0)
        )

        # 事件区（只展示降级/完成/失败，不写日志文件）
        events_frame = ttk.LabelFrame(main, text="事件", padding=(10, 8))
        events_frame.grid(row=7, column=0, columnspan=4, sticky="nsew", pady=(12, 0))
        main.rowconfigure(7, weight=1)
        events_frame.columnconfigure(0, weight=1)
        events_frame.rowconfigure(0, weight=1)

        self.event_text = tk.Text(
            events_frame, height=7, wrap="word", state="disabled",
            borderwidth=1, relief="solid", padx=8, pady=6,
        )
        self.event_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(events_frame, orient="vertical", command=self.event_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.event_text.configure(yscrollcommand=scrollbar.set)
        self.event_text.tag_configure("info", foreground="#444444")
        self.event_text.tag_configure("degrade", foreground="#B26A00")
        self.event_text.tag_configure("error", foreground="#B00020")
        self.event_text.tag_configure("done", foreground="#0B6E4F")

        # URL 失焦时自动补默认文件名
        url_entry.bind("<FocusOut>", self._fill_default_output)
        # URL 改动后延迟探测一次，用重定向后的真实文件名刷新“保存到”
        self.url_var.trace_add("write", lambda *_args: self._schedule_resolve())
        url_entry.bind("<Return>", lambda _event: self._start())

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #

    def _paste_url(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        self.url_var.set(text.strip())
        self._fill_default_output()

    def _fill_default_output(self, _event: Optional[tk.Event] = None) -> None:
        """按“保存目录 + URL 里的文件名”填充；用户手工改过就不覆盖。"""
        url = self.url_var.get().strip()
        if not url:
            return
        current = self.output_var.get().strip()
        if current and current != self._auto_output:
            return
        self._set_auto_output(default_output_path(url, self.download_dir))

    def _set_auto_output(self, path: str) -> None:
        """记录“自动填充值”，用于判断用户是否手工改过文件名。"""
        self._auto_output = path
        self.output_var.set(path)

    # ---- 解析真实文件名（跟随跳转 / Content-Disposition）--------------- #

    def _schedule_resolve(self) -> None:
        """URL 变化后延迟解析，避免每敲一个字都发请求。"""
        self._resolve_token += 1
        token = self._resolve_token
        self.root.after(600, lambda: self._start_resolve(token))

    def _start_resolve(self, token: int) -> None:
        if token != self._resolve_token:
            return
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            return
        if self.engine and self.engine.is_running():
            return
        threading.Thread(
            target=self._resolve_worker, args=(url, token), daemon=True
        ).start()

    def _resolve_worker(self, url: str, token: int) -> None:
        """后台探测一次，只为拿到跳转后的真实文件名（失败静默）。"""
        try:
            probe = probe_url(url, DEFAULT_UA, self.trust_system_ca)
        except Exception:  # noqa: BLE001 - 解析失败就保留 URL 推导名
            return
        try:
            self.root.after(0, lambda: self._apply_resolved(url, probe, token))
        except tk.TclError:
            pass

    def _apply_resolved(self, url: str, probe: ProbeResult, token: int) -> None:
        if token != self._resolve_token:
            return
        if self.engine and self.engine.is_running():
            return
        name = probe.filename or default_output_name(probe.final_url or url)
        current = self.output_var.get().strip()
        if current and current != self._auto_output:
            return
        base_dir = Path(current).parent if current else self.download_dir
        target = str(base_dir / name)
        if target == current:
            return
        self._set_auto_output(target)
        self.status_var.set(f"已解析文件名：{name}")

    def _browse_output(self) -> None:
        url = self.url_var.get().strip()
        current = self.output_var.get().strip()
        if current:
            initial_dir = Path(current).parent
            initial_name = Path(current).name
        else:
            initial_dir = self.download_dir
            initial_name = default_output_name(url) if url else "download.bin"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="选择保存位置",
            initialdir=str(initial_dir),
            initialfile=initial_name,
        )
        if path:
            self.output_var.set(path)

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #

    def apply_download_dir(self, raw: str, parent: Optional[tk.Misc] = None) -> bool:
        """校验并持久化“默认保存目录”；成功返回 True。

        设置窗口与自动化测试共用这一入口，避免把校验/落盘逻辑散落在界面代码里。
        """
        anchor = parent or self.root
        text = raw.strip()
        if not text:
            messagebox.showerror("设置", "默认保存目录不能为空", parent=anchor)
            return False

        target = Path(text).expanduser()
        if target.exists() and not target.is_dir():
            messagebox.showerror("设置", "该路径不是目录，请重新选择", parent=anchor)
            return False
        if not target.exists():
            if not messagebox.askyesno(
                "设置", f"目录不存在：\n{target}\n\n是否创建？", parent=anchor
            ):
                return False
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("设置", f"创建目录失败：{exc}", parent=anchor)
                return False

        previous_dir = self.download_dir
        self.download_dir = target
        self.settings["download_dir"] = str(target)
        try:
            save_settings(self.settings)
        except OSError as exc:
            messagebox.showerror("设置", f"保存设置失败：{exc}", parent=anchor)
            self.download_dir = previous_dir
            return False

        # “保存到”为空、或仍是旧默认目录下的自动填充值时，立即按新目录刷新
        current = self.output_var.get().strip()
        if not current or Path(current).parent == previous_dir:
            self.output_var.set("")
            self._fill_default_output()
        self.status_var.set(f"默认保存目录：{target}")
        return True

    def apply_trust_system_ca(self, enabled: bool) -> None:
        """切换 HTTPS 证书信任来源并持久化。

        开启时按系统证书库校验（与浏览器一致），关闭则只用内置 certifi。
        """
        self.trust_system_ca = bool(enabled)
        self.settings["trust_system_ca"] = self.trust_system_ca
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.status_var.set(f"设置保存失败：{exc}")
        else:
            self.status_var.set(
                "已信任系统证书库" if self.trust_system_ca else "已改用内置 certifi 证书"
            )

    def _open_settings(self) -> None:
        """设置窗口：调整“新建任务”的默认保存目录并持久化。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text="默认保存目录", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )

        dir_var = tk.StringVar(value=str(self.download_dir))
        entry = ttk.Entry(body, textvariable=dir_var, width=46)
        entry.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        def _choose() -> None:
            chosen = filedialog.askdirectory(
                parent=dialog, title="选择默认保存目录", initialdir=dir_var.get() or str(self.download_dir)
            )
            if chosen:
                dir_var.set(chosen)

        ttk.Button(body, text="浏览...", width=8, command=_choose).grid(
            row=1, column=1, padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(
            body,
            text="新建任务时“保存到”默认使用该目录，仍可在主界面单独修改文件名或路径。",
            style="Hint.TLabel",
            wraplength=580,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 配置路径展示时把 %APPDATA% 折叠掉，避免路径太长把窗口撑宽
        settings_path = str(config_path())
        appdata = os.environ.get("APPDATA")
        if appdata and settings_path.startswith(appdata):
            settings_path = "%APPDATA%" + settings_path[len(appdata):]
        ttk.Label(
            body,
            text=f"配置文件：{settings_path}",
            style="Hint.TLabel",
            wraplength=580,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 12))

        # ---- HTTPS 证书 ----
        ttk.Label(body, text="HTTPS 证书", style="Section.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w"
        )
        trust_var = tk.BooleanVar(value=self.trust_system_ca)
        ttk.Checkbutton(
            body, text="信任 Windows 证书库（推荐）", variable=trust_var
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(
            body,
            text="公司代理、杀毒软件或加速器会替换 HTTPS 证书；\n"
                 "勾选后与浏览器信任范围一致，取消则只用内置 certifi。",
            style="Hint.TLabel",
            wraplength=660,
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 12))

        actions = ttk.Frame(body)
        actions.grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Button(
            actions, text="恢复系统下载目录", command=lambda: dir_var.set(str(system_downloads_dir()))
        ).pack(side="left")

        def _save() -> None:
            if self.apply_download_dir(dir_var.get(), dialog):
                self.apply_trust_system_ca(bool(trust_var.get()))
                dialog.destroy()

        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="保存", command=_save).pack(side="right", padx=(0, 8))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        entry.focus_set()

    def _refresh_shard_hint(self) -> None:
        """刷新“最高 N 片”提示；超限时改成橙色警告（不夹取，仍由用户决定）。"""
        limit = self._max_shards
        try:
            value = int(self.shards_var.get())
        except (tk.TclError, ValueError):
            value = 0
        if value > limit:
            self.shard_hint_var.set(f"超过上限，最高 {limit} 片")
            self.shard_hint_label.configure(style="Warn.TLabel")
        else:
            self.shard_hint_var.set(f"最高 {limit} 片")
            self.shard_hint_label.configure(style="Hint.TLabel")

    # ------------------------------------------------------------------ #
    # 设置的读取与保存
    # ------------------------------------------------------------------ #

    def _collect_ui_state(self) -> dict:
        """收集需要记忆的界面状态：下载参数 + 保存路径 + 默认目录。"""
        state = dict(self.settings)
        state["download_dir"] = str(self.download_dir)
        try:
            state["shards"] = max(1, int(self.shards_var.get()))
        except (tk.TclError, ValueError):
            pass
        try:
            state["concurrency"] = max(1, min(int(self.concurrency_var.get()), MAX_CONCURRENCY))
        except (tk.TclError, ValueError):
            pass
        try:
            state["retry"] = max(0, int(self.retry_var.get()))
        except (tk.TclError, ValueError):
            pass
        state["chunk_mb"] = self.chunk_mb_var.get().strip()
        return state

    def _save_ui_state(self) -> None:
        """退出时把界面状态写回配置文件；失败只在状态栏提示，不阻塞关闭。"""
        state = self._collect_ui_state()
        try:
            save_settings(state)
        except OSError as exc:
            self.status_var.set(f"设置保存失败：{exc}")
            return
        self.settings = state

    def _start(self) -> None:
        if self.engine and self.engine.is_running():
            return

        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showerror("参数错误", "请填写以 http:// 或 https:// 开头的下载链接")
            return

        output = self.output_var.get().strip() or default_output_path(url, self.download_dir)
        self.output_var.set(output)
        target = Path(output).expanduser()
        if target.exists() and target.is_dir():
            messagebox.showerror("参数错误", "保存路径是一个目录，请指定文件名")
            return
        if target.exists():
            if not messagebox.askyesno(
                "确认覆盖", f"目标文件已存在：\n{target}\n\n继续将覆盖该文件，是否继续？"
            ):
                return

        try:
            shards = int(self.shards_var.get())
            concurrency = int(self.concurrency_var.get())
            retry = int(self.retry_var.get())
            chunk_mb = self.chunk_mb_var.get().strip()
            chunk_size = int(float(chunk_mb) * 1024 * 1024) if chunk_mb else None
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "参数错误", "分片数、并发连接、重试次数必须是整数，分片大小必须是数字"
            )
            return

        if shards < 1 or retry < 0 or concurrency < 1:
            messagebox.showerror(
                "参数错误", "分片数和并发连接必须 >= 1，重试次数必须 >= 0"
            )
            return
        if concurrency > MAX_CONCURRENCY:
            messagebox.showerror("参数错误", f"并发连接不能超过 {MAX_CONCURRENCY}")
            return
        if chunk_size is not None and chunk_size < MIN_CHUNK_SIZE:
            messagebox.showerror("参数错误", "分片大小不能小于 0.5 MB")
            return

        # 新任务：上限先回到硬上限，等探测出总大小后再按 0.5 MB 最小分片收紧
        self._max_shards = MAX_SHARDS
        self._probe_size_applied = 0
        self._refresh_shard_hint()

        # 超过上限不拦截：按用户输入继续，只在事件区给出橙色警告
        if shards > self._max_shards:
            self.events.put((
                "degrade",
                f"分片数 {shards} 超过建议上限 {self._max_shards} 片，"
                "仍按输入继续；若被限速或遇到 429 请调低分片数",
            ))

        try:
            self.engine = DownloadEngine(
                url=url,
                output=str(target),
                shards=shards,
                concurrency=concurrency,
                chunk_size=chunk_size,
                retry=retry,
                trust_system_ca=self.trust_system_ca,
            )
        except DownloadError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.engine.on_event = lambda kind, text: self.events.put((kind, text))
        self._set_running(True)
        self._set_progress(0, 0, 0.0)
        self.status_var.set("正在探测目标链接 ...")
        self.engine.start()

    def _stop(self) -> None:
        if self.engine and self.engine.is_running():
            self.engine.cancel()
            self.status_var.set("正在停止，等待分片收尾 ...")
            self.stop_button.configure(state="disabled")

    def _on_close(self) -> None:
        self._resolve_token += 1  # 取消尚未返回的文件名解析
        if self.engine and self.engine.is_running():
            if not messagebox.askyesno(
                "确认退出", "下载仍在进行，退出会保留断点，下次可续传。确定退出吗？"
            ):
                return
            self.engine.cancel()
            self.engine.wait(timeout=3.0)
        self._save_ui_state()
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # 事件轮询与刷新
    # ------------------------------------------------------------------ #

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, text = self.events.get_nowait()
                self._append_event(kind, text)
                if kind == "done":
                    self.status_var.set("下载完成")
                    self._set_running(False)
                    self.progress_bar.stop()
                elif kind == "error":
                    self.status_var.set("下载失败")
                    self._set_running(False)
                    self.progress_bar.stop()
                    messagebox.showerror("下载失败", text)
                elif kind == "stopped":
                    self.status_var.set("已停止（断点已保留）")
                    self._set_running(False)
                    self.progress_bar.stop()
                elif kind == "degrade":
                    self.status_var.set(text)
                elif kind == "info":
                    self.status_var.set(text)
        except queue.Empty:
            pass

        if self.engine:
            done, total, speed = self.engine.snapshot()
            self._set_progress(done, total, speed)

            # 探测出总大小后，把“最高 N 片”收紧到真实可切分片数
            probe = self.engine.last_probe
            if probe is not None and probe.total_size:
                size = int(probe.total_size)
                if size != self._probe_size_applied:
                    self._probe_size_applied = size
                    limit = max_shards_for_size(size)
                    if limit != self._max_shards:
                        self._max_shards = limit
                        self._refresh_shard_hint()

        self.root.after(120, self._poll_events)

    def _set_progress(self, done: int, total: int, speed: float) -> None:
        if total > 0:
            percent = min(100.0, done / total * 100.0)
            self.percent_var.set(f"{percent:.1f}%")
            self.size_var.set(f"{format_bytes(done)} / {format_bytes(total)}")
            if str(self.progress_bar.cget("mode")) != "determinate":
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
            self.progress_bar["value"] = percent
            remaining = (total - done) / speed if speed > 0 else float("inf")
            self.eta_var.set(f"剩余 {format_eta(remaining)}")
        else:
            self.percent_var.set("--")
            self.size_var.set(f"{format_bytes(done)} / 未知大小")
            if str(self.progress_bar.cget("mode")) != "indeterminate":
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start(12)
            self.eta_var.set("剩余 --:--")
        self.speed_var.set(f"速度 {format_speed(speed)}")

    def _append_event(self, kind: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        tag = kind if kind in ("info", "degrade", "error", "done") else "info"
        self.event_text.configure(state="normal")
        self.event_text.insert("end", f"[{stamp}] {text}\n", tag)
        self.event_text.see("end")
        self.event_text.configure(state="disabled")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> None:
    """启动图形界面。"""
    enable_dpi_awareness()
    root = tk.Tk()
    DownloadApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
