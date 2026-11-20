"""eD2K 本地回环自测：A 共享文件，B 直连 A 下载，比对哈希。

不依赖外网、不依赖 eD2K 服务器：两个引擎都用 ``offline`` 模式启动，
B 通过 ``sources`` 直连 A 的监听端口。用来验证「握手 + 分块传输 + MD4 校验 + 隔离区释放」整条链路。

用法::

    python tools/ed2k_loopback.py
    python tools/ed2k_loopback.py --size 8 --port 4701
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "ed2k-engine" / "engine.py"
# 每次跑在独立子目录里：eD2K 的隔离区是 .incoming，复用同一目录容易残留旧 ACL/旧分片
WORK = ROOT / "out" / f"loopback-{time.strftime('%H%M%S')}"


class Engine:
    """一个引擎子进程，带事件缓冲与查询等待。"""

    def __init__(self, name: str, out_dir: Path, state_dir: Path, port: int) -> None:
        self.name = name
        self.out_dir = out_dir
        self.state_dir = state_dir
        self.port = port
        self.events: list[dict] = []
        self.stderr: list[str] = []
        self.process = subprocess.Popen(
            [sys.executable, str(ENGINE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if line:
                try:
                    self.events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def _drain_stderr(self) -> None:
        assert self.process.stderr
        for line in self.process.stderr:
            self.stderr.append(line.rstrip())

    def send(self, payload: dict) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def wait_for(self, predicate, timeout: float, description: str) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for item in self.events:
                if predicate(item):
                    return item
            if self.process.poll() is not None:
                raise RuntimeError(f"[{self.name}] 引擎提前退出：{self.process.returncode}")
            time.sleep(0.1)
        raise TimeoutError(
            f"[{self.name}] 等待 {description} 超时\n"
            f"事件：{json.dumps(self.events[-12:], ensure_ascii=False)}\n"
            f"stderr：{chr(10).join(self.stderr[-15:])}"
        )

    def stop(self) -> None:
        try:
            self.send({"cmd": "shutdown"})
            self.process.wait(timeout=10)
        except Exception:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="eD2K 本地回环自测")
    parser.add_argument("--size", type=int, default=4, help="测试文件大小（MB）")
    parser.add_argument("--port", type=int, default=4701, help="A 的监听端口")
    parser.add_argument(
        "--use-bridge", action="store_true", help="B 走主程序的 Ed2kBridge（验证集成路径）"
    )
    parser.add_argument(
        "--engine", default="", help="桥接模式下指定引擎路径（默认由主程序自动查找）"
    )
    arguments = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    share_dir = WORK / "share"
    share_dir.mkdir(parents=True, exist_ok=True)
    payload = bytes(random.getrandbits(8) for _ in range(1024)) * (arguments.size * 1024)
    source_file = share_dir / "loopback.bin"
    source_file.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    print(f"[准备] {source_file} {len(payload)} 字节 sha256={expected[:16]}…")

    a = Engine("A", WORK / "a", WORK / "state-a", arguments.port)
    b = Engine("B", WORK / "b", WORK / "state-b", arguments.port + 1)
    try:
        a.wait_for(lambda e: e.get("event") == "ready", 20, "A ready")
        b.wait_for(lambda e: e.get("event") == "ready", 20, "B ready")

        # A：算哈希 → 生成链接
        a.send({"cmd": "hash", "path": str(source_file)})
        hash_event = a.wait_for(lambda e: e.get("event") == "hash", 30, "A 计算哈希")
        link = hash_event["link"]
        print(f"[A] 链接 {link}")

        # A：加入共享（offline + 自动建网 + 启动监听）
        a.send(
            {
                "cmd": "share",
                "path": str(source_file),
                "out_dir": str(a.out_dir),
                "state_dir": str(a.state_dir),
                "tcp_port": arguments.port,
                "udp_port": arguments.port + 2,
                "use_kad": False,
                "offline": True,
            }
        )
        seed_event = a.wait_for(lambda e: e.get("event") == "seed", 30, "A 开始做种")
        net_event = a.wait_for(
            lambda e: e.get("event") == "net" and e.get("port"), 15, "A 监听端口"
        )
        listen_port = int(net_event["port"])
        print(f"[A] 做种 {seed_event['files']} 个文件，监听端口 {listen_port}")

        # B：直连 A 下载（--use-bridge 时走主程序里的 Ed2kBridge，验证真实集成路径）
        if arguments.use_bridge:
            sys.path.insert(0, str(ROOT))
            import high_speed_downloader as sd

            seen: list[tuple[str, str]] = []
            bridge = sd.Ed2kBridge(lambda kind, text: seen.append((kind, text)))
            bridge.start(
                {
                    "link": link,
                    "out_dir": str(b.out_dir),
                    "state_dir": str(b.state_dir),
                    "tcp_port": arguments.port + 1,
                    "udp_port": arguments.port + 2,
                    "use_kad": False,
                    "offline": True,
                    "seed": False,
                    "max_peers": 4,
                    "sources": [{"host": "127.0.0.1", "port": listen_port}],
                    "_settings": {"ed2k_engine": arguments.engine} if arguments.engine else {},
                }
            )
            deadline = time.time() + 180
            while time.time() < deadline and bridge.running:
                time.sleep(0.2)
            if bridge.running:
                bridge.cancel()
                print("[失败] 桥接模式超时")
                return 1
            done_events = [text for kind, text in seen if kind == "done"]
            if not done_events:
                print(f"[失败] 没有收到完成事件：{seen[-5:]}")
                return 1
            target = Path(done_events[-1])
            bridge.shutdown()
        else:
            b.send(
                {
                    "cmd": "start",
                    "link": link,
                    "out_dir": str(b.out_dir),
                    "state_dir": str(b.state_dir),
                    "tcp_port": arguments.port + 1,
                    "udp_port": arguments.port + 2,
                    "use_kad": False,
                    "offline": True,
                    "seed": False,
                    "max_peers": 4,
                    "sources": [{"host": "127.0.0.1", "port": listen_port}],
                }
            )
            done = b.wait_for(
                lambda e: e.get("event") in {"done", "error"}, 180, "B 下载结果"
            )
            if done.get("event") == "error":
                print(f"[失败] {done.get('message')}\n{done.get('detail', '')}")
                return 1
            target = Path(done["path"])
            print(f"[B] 完成 {target} {done['size']} 字节")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        ok = actual == expected
        print(f"[校验] sha256 {'一致' if ok else '不一致'}：{actual[:16]}…")
        print("[结果]", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        a.stop()
        b.stop()


if __name__ == "__main__":
    raise SystemExit(main())
