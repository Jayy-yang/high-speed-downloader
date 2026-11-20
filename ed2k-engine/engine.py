"""HighSpeedDownloader eD2K 引擎（独立进程，GPL-3.0）。

主程序通过 **stdin / stdout 的按行 JSON** 与本进程通信，不共享内存、不互相 import：

    主程序 → 引擎：{"id":1,"cmd":"start","link":"ed2k://|file|...","out_dir":"D:\\Downloads"}
    引擎 → 主程序：{"event":"progress","verified":1024,"size":4096,"peers":2,"sources":30}

设计要点：

* 一个引擎进程同时只跑一个 eD2K 下载任务（eD2K 的共享/上传状态是全局的，多任务并发意义不大）；
* 下载完成后可按设置继续做种（``share``），做种期间进程保持连接；
* 所有日志写 stderr，stdout 只输出协议行，避免污染通道；
* 收到 ``shutdown`` 或 stdin 关闭时优雅退出：取消下载 → 关闭客户端 → 退出。

用法（一般由主程序拉起，不建议手工调用）::

    python engine.py --version
    echo {"cmd":"start",...} | python engine.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Optional

ENGINE_VERSION = "1.1.1"

# vendor 目录里是上游 ed2k_client 的原样副本（仅打了 Windows 文件锁补丁）
_VENDOR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from ed2k_client import Client, ClientConfig, DownloadSettings, FileLink, Source, hash_file  # noqa: E402
from ed2k_client.download import ContentPolicy  # noqa: E402
from ed2k_client.sources import client_id_from_host  # noqa: E402

LOGGER = logging.getLogger("ed2k-engine")

# stdout 写入锁：进度回调与命令处理可能在不同线程/协程里同时写
_WRITE_LOCK = threading.Lock()


def emit(payload: dict[str, Any]) -> None:
    """向 stdout 输出一行 JSON 事件。"""
    line = json.dumps(payload, ensure_ascii=False)
    with _WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def emit_error(message: str, *, fatal: bool = False, detail: Optional[str] = None) -> None:
    """输出错误事件；``fatal`` 表示引擎无法继续工作。"""
    payload: dict[str, Any] = {"event": "error", "message": message, "fatal": fatal}
    if detail:
        payload["detail"] = detail
    emit(payload)


def _parse_source(spec: Any) -> Optional[Source]:
    """把 ``{"host": "127.0.0.1", "port": 4662}`` 转成协议里的 ``Source``。

    主要用于联调：让客户端跳过服务器/Kad，直接连指定 peer。
    """
    try:
        client_id = client_id_from_host(str(spec["host"]))
        port = int(spec["port"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < port < 65536:
        return None
    return Source(client_id=client_id, port=port)


class Engine:
    """把 ``ed2k_client.Client`` 包装成 JSON 行协议。"""

    def __init__(self) -> None:
        self.client: Optional[Client] = None
        self._client_entered = False
        self.download_task: Optional[asyncio.Task[Any]] = None
        self.monitor_task: Optional[asyncio.Task[Any]] = None
        self.current_link: Optional[FileLink] = None
        self.seed_enabled = True
        self.use_kad = True
        self.shutting_down = False

    # ------------------------------------------------------------------ 客户端

    async def ensure_client(self, spec: dict[str, Any]) -> Client:
        """按 ``start`` 命令里的参数建立（或复用）eD2K 客户端。"""
        if self.client is not None and self._client_entered:
            return self.client

        state_dir = Path(spec["state_dir"])
        out_dir = Path(spec["out_dir"])
        state_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        max_peers = max(1, min(int(spec.get("max_peers") or 8), 64))
        offline = bool(spec.get("offline"))
        config = ClientConfig(
            state_dir=state_dir,
            download_dir=out_dir,
            nickname=str(spec.get("nickname") or "HighSpeedDownloader"),
            tcp_port=int(spec.get("tcp_port") or 4662),
            udp_port=int(spec.get("udp_port") or 4672),
            download=DownloadSettings(max_peers=max_peers, content_policy=ContentPolicy.ANY),
        )
        if offline:
            # 联调模式：不拉服务器列表、不拉 Kad 节点、不拉 IP 过滤表，只做本地直连
            config = ClientConfig(
                state_dir=state_dir,
                download_dir=out_dir,
                nickname=config.nickname,
                tcp_port=config.tcp_port,
                udp_port=config.udp_port,
                servers=(),
                server_met_url="",
                nodes_dat_url="",
                ip_filter_url="",
                allow_private_peers=True,  # 联调要连 127.0.0.1
                download=config.download,
            )
        client = Client(config)
        await client.__aenter__()
        self._client_entered = True
        self.client = client

        if offline:
            # 联调模式：只和直连源通信，不登录服务器、不拉任何列表
            emit({"event": "net", "servers": 0, "low_id": False, "kad": 0, "port": None, "shared": 0})
            self.monitor_task = asyncio.create_task(self._monitor(client))
            return client

        emit({"event": "log", "message": "正在登录 eD2K 服务器…"})
        status = await client.connect()
        emit(
            {
                "event": "net",
                "servers": len(status.servers),
                "low_id": any(item.low_id for item in status.servers),
                "kad": status.kad_contacts,
                "port": client.listening_port,
                "shared": status.shared_files,
            }
        )
        if not status.servers:
            emit_error("没有登录上任何 eD2K 服务器，将只能靠 Kad 找源", fatal=False)

        if self.use_kad:
            try:
                contacts = await client.connect_kad()
                emit({"event": "log", "message": f"Kad 已连接，联系人 {contacts} 个"})
            except Exception as exc:  # Kad 失败不应阻断下载
                LOGGER.warning("Kad 连接失败: %s", exc)
                emit_error(f"Kad 连接失败：{exc}", fatal=False)

        self.monitor_task = asyncio.create_task(self._monitor(client))
        return client

    def _net_payload(self) -> dict[str, Any]:
        """组装一条网络状态事件；未连接或处于离线联调模式时给零值。"""
        client = self.client
        port = client.listening_port if client is not None else None
        status = None
        if client is not None:
            try:
                status = client.status()
            except Exception:  # 离线模式下从未登录，status() 不可用
                status = None
        return {
            "event": "net",
            "servers": len(status.servers) if status else 0,
            "low_id": any(item.low_id for item in status.servers) if status else False,
            "kad": status.kad_contacts if status else 0,
            "port": port,
            "shared": status.shared_files if status else 0,
        }

    async def _monitor(self, client: Client) -> None:
        """每 3 秒推送一次连接状态。"""
        while not self.shutting_down:
            await asyncio.sleep(3.0)
            emit(self._net_payload())

    # ------------------------------------------------------------------ 命令

    async def cmd_start(self, spec: dict[str, Any]) -> None:
        """开始一个 eD2K 下载。"""
        if self.download_task is not None and not self.download_task.done():
            emit_error("已有 eD2K 任务在运行，同一时间只支持一个", fatal=False)
            return

        raw_link = str(spec.get("link") or "").strip()
        try:
            link = FileLink.parse(raw_link)
        except Exception as exc:
            emit_error(f"无法解析 eD2K 链接：{exc}", fatal=False)
            return

        self.use_kad = bool(spec.get("use_kad", True))
        self.seed_enabled = bool(spec.get("seed", True))
        self.current_link = link
        emit(
            {
                "event": "task",
                "name": link.name,
                "size": link.size,
                "hash": link.file_hash.hex,
                "has_aich": link.aich_root is not None,
            }
        )
        self.download_task = asyncio.create_task(self._run_download(spec, link))

    async def _run_download(self, spec: dict[str, Any], link: FileLink) -> None:
        """执行下载并推送进度/结果。"""
        try:
            client = await self.ensure_client(spec)
        except Exception as exc:
            LOGGER.exception("建立客户端失败")
            emit_error(f"无法连接 eD2K 网络：{exc}", fatal=False, detail=traceback.format_exc(limit=3))
            return

        settings = DownloadSettings(
            max_peers=max(1, min(int(spec.get("max_peers") or 8), 64)),
            content_policy=ContentPolicy.ANY,
        )
        # 指定源（仅联调/内网直连用）：[{"host":"127.0.0.1","port":4662}]
        extra_sources = [item for item in map(_parse_source, spec.get("sources") or []) if item]
        if extra_sources:
            emit({"event": "log", "message": f"已注入 {len(extra_sources)} 个直连源"})
        last_percent = -1

        def on_progress(progress: Any) -> None:
            nonlocal last_percent
            percent = int(progress.verified_bytes * 100 / progress.size) if progress.size else 0
            # 百分比没变时不刷屏，但每 30 次仍然推一次，让速度统计有数据
            if percent == last_percent:
                return
            last_percent = percent
            emit(
                {
                    "event": "progress",
                    "name": progress.name,
                    "size": progress.size,
                    "verified": progress.verified_bytes,
                    "peers": progress.active_peers,
                    "sources": progress.known_sources,
                }
            )

        emit({"event": "log", "message": "正在向服务器与 Kad 请求源…"})
        try:
            result = await client.download(
                link, progress=on_progress, settings=settings, extra_sources=extra_sources
            )
        except asyncio.CancelledError:
            emit({"event": "cancelled"})
            raise
        except PermissionError as exc:
            # 隔离区目录权限问题最常见：旧版本或别的账号建过 .incoming，ACL 把自己也挡在外面
            LOGGER.exception("隔离区目录不可写")
            emit_error(
                f"无法写入 eD2K 隔离区目录：{exc}\n"
                f"请删除下载目录下的 .incoming 文件夹（或换一个下载目录）后重试",
                fatal=False,
                detail=traceback.format_exc(limit=3),
            )
            return
        except Exception as exc:
            LOGGER.exception("下载失败")
            emit_error(f"eD2K 下载失败：{exc}", fatal=False, detail=traceback.format_exc(limit=3))
            return

        emit(
            {
                "event": "done",
                "path": str(result.path),
                "size": result.path.stat().st_size if result.path.exists() else link.size,
                "inspection": getattr(result.inspection, "verdict", str(result.inspection)),
            }
        )
        if self.seed_enabled:
            await self._share(result.path)

    async def _share(self, path: Path) -> None:
        """把下载好的文件加入共享，继续做种。"""
        if self.client is None:
            return
        try:
            shared = await self.client.share(path)
        except Exception as exc:
            LOGGER.warning("做种失败: %s", exc)
            emit_error(f"文件已下载，但加入共享失败：{exc}", fatal=False)
            return
        emit({"event": "seed", "files": len(shared), "path": str(path)})

    async def cmd_cancel(self) -> None:
        """取消当前下载（保留分片进度）。"""
        task = self.download_task
        if task is None or task.done():
            emit({"event": "log", "message": "当前没有可取消的 eD2K 任务"})
            return
        task.cancel()

    async def cmd_status(self) -> None:
        """按需回推一次状态。"""
        if self.client is None:
            emit({"event": "net", "servers": 0, "low_id": False, "kad": 0, "port": None, "shared": 0})
            return
        status = self.client.status()
        emit(
            {
                "event": "net",
                "servers": len(status.servers),
                "low_id": any(item.low_id for item in status.servers),
                "kad": status.kad_contacts,
                "port": self.client.listening_port,
                "shared": status.shared_files,
            }
        )

    async def cmd_share(self, spec: dict[str, Any]) -> None:
        """把指定文件加入共享（用于“下载后继续做种”之外的场景）。"""
        raw = spec.get("path")
        if not raw:
            emit_error("share 需要 path 参数", fatal=False)
            return
        path = Path(str(raw))
        if not path.is_file():
            emit_error(f"文件不存在：{path}", fatal=False)
            return
        if self.client is None:
            # 做种也可能发生在任何下载之前：用同一条指令里的连接参数临时建网
            if spec.get("out_dir") and spec.get("state_dir"):
                try:
                    await self.ensure_client(spec)
                except Exception as exc:
                    emit_error(f"无法连接 eD2K 网络：{exc}", fatal=False)
                    return
            else:
                emit_error("尚未连接 eD2K 网络", fatal=False)
                return
        await self._share(path)

    async def cmd_hash(self, spec: dict[str, Any]) -> None:
        """计算本地文件的 eD2K 哈希（联调与“生成 ed2k 链接”用）。"""
        raw = spec.get("path")
        if not raw:
            emit_error("hash 需要 path 参数", fatal=False)
            return
        path = Path(str(raw))
        if not path.is_file():
            emit_error(f"文件不存在：{path}", fatal=False)
            return
        hashes = await asyncio.to_thread(hash_file, path)
        emit(
            {
                "event": "hash",
                "path": str(path),
                "size": hashes.size,
                "hash": hashes.file_hash.hex,
                "link": f"ed2k://|file|{path.name}|{hashes.size}|{hashes.file_hash.hex}|/",
            }
        )

    async def shutdown(self) -> None:
        """取消任务、断开网络并退出。"""
        self.shutting_down = True
        for task in (self.download_task, self.monitor_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self.download_task, self.monitor_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.client is not None and self._client_entered:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception as exc:  # 关闭失败不影响退出
                LOGGER.warning("关闭客户端失败: %s", exc)
            self._client_entered = False
        emit({"event": "bye"})


async def _read_stdin(queue: "asyncio.Queue[Optional[str]]", loop: asyncio.AbstractEventLoop) -> None:
    """在后台线程里读 stdin，把整行塞进队列（None 表示 EOF）。"""

    def worker() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=worker, name="stdin-reader", daemon=True).start()


async def main_async() -> int:
    """事件循环主入口。"""
    engine = Engine()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    await _read_stdin(queue, loop)

    emit(
        {
            "event": "ready",
            "version": ENGINE_VERSION,
            "python": sys.version.split()[0],
            "platform": sys.platform,
        }
    )

    try:
        while True:
            line = await queue.get()
            if line is None:  # stdin 关闭：主程序退出了
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                emit_error("收到无法解析的指令（不是合法 JSON）", fatal=False)
                continue

            command = message.get("cmd")
            request_id = message.get("id")
            try:
                if command == "start":
                    await engine.cmd_start(message)
                elif command == "cancel":
                    await engine.cmd_cancel()
                elif command == "status":
                    await engine.cmd_status()
                elif command == "share":
                    await engine.cmd_share(message)
                elif command == "hash":
                    await engine.cmd_hash(message)
                elif command == "ping":
                    emit({"id": request_id, "ok": True})
                elif command == "shutdown":
                    break
                else:
                    emit_error(f"未知指令：{command}", fatal=False)
            except Exception as exc:  # 单条指令失败不应终止引擎
                LOGGER.exception("处理指令失败")
                emit_error(f"指令 {command} 执行失败：{exc}", fatal=False, detail=traceback.format_exc(limit=3))
    finally:
        await engine.shutdown()
    return 0


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="HighSpeedDownloader eD2K 引擎")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    parser.add_argument("--debug", action="store_true", help="把调试日志也写到 stderr")
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if arguments.debug else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if arguments.version:
        print(ENGINE_VERSION)
        return 0

    # Windows 控制台默认 GBK，强制 UTF-8，避免中文文件名/日志乱码
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
