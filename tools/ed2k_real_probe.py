"""eD2K 真实网络探测：A 通过真实服务器/Kad 发布自有文件，B 只靠网络找源。

不依赖任何外部 ed2k 链接（避免下载来路不明的文件）：
A、B 都是本机引擎，但走真实的 eD2K 服务器与 Kad，
用来验证「服务器登录 → 文件发布 → 源发现」这条真实链路。

用法::

    python tools/ed2k_real_probe.py --seconds 150
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "out" / f"real-{time.strftime('%H%M%S')}"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ed2k_loopback import Engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="eD2K 真实网络源探测")
    parser.add_argument("--seconds", type=int, default=150, help="B 观察时长")
    parser.add_argument("--size", type=int, default=1, help="共享文件大小（MB）")
    arguments = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    share_dir = WORK / "share"
    share_dir.mkdir(parents=True, exist_ok=True)
    source_file = share_dir / "hsd-probe.bin"
    source_file.write_bytes(bytes(range(256)) * (arguments.size * 4096))
    print(f"[准备] {source_file} {source_file.stat().st_size} 字节")

    a = Engine("A", WORK / "a", WORK / "state-a", 4662)
    b = Engine("B", WORK / "b", WORK / "state-b", 4663)
    try:
        a.wait_for(lambda e: e.get("event") == "ready", 20, "A ready")
        b.wait_for(lambda e: e.get("event") == "ready", 20, "B ready")

        # A：真实联网 + 发布文件
        a.send(
            {
                "cmd": "share",
                "path": str(source_file),
                "out_dir": str(a.out_dir),
                "state_dir": str(a.state_dir),
                "tcp_port": 4662,
                "udp_port": 4672,
                "use_kad": True,
                "offline": False,
            }
        )
        seed = a.wait_for(lambda e: e.get("event") == "seed", 120, "A 发布文件")
        net_a = a.wait_for(lambda e: e.get("event") == "net" and e.get("port"), 120, "A 启动监听")
        print(f"[A] 服务器 {net_a['servers']} · 端口 {net_a['port']} · Low-ID {net_a['low_id']} · 共享 {seed['files']}")

        a.send({"cmd": "hash", "path": str(source_file)})
        link = a.wait_for(lambda e: e.get("event") == "hash", 60, "A 计算哈希")["link"]
        print(f"[A] {link}")

        # B：同样真实联网，但只给链接，靠服务器/Kad 找 A
        b.send(
            {
                "cmd": "start",
                "link": link,
                "out_dir": str(b.out_dir),
                "state_dir": str(b.state_dir),
                "tcp_port": 4663,
                "udp_port": 4673,
                "use_kad": True,
                "offline": False,
                "seed": False,
                "max_peers": 4,
            }
        )

        deadline = time.time() + arguments.seconds
        best_sources = 0
        best_peers = 0
        best_bytes = 0
        while time.time() < deadline:
            for event in list(b.events):
                if event.get("event") == "progress":
                    best_sources = max(best_sources, int(event.get("sources") or 0))
                    best_peers = max(best_peers, int(event.get("peers") or 0))
                    best_bytes = max(best_bytes, int(event.get("verified") or 0))
                if event.get("event") == "done":
                    print("[B] 下载完成", event.get("path"), event.get("size"))
                    return 0
            if b.process.poll() is not None:
                print("[B] 引擎退出", b.process.returncode)
                return 1
            time.sleep(1.0)

        print(f"[B] {arguments.seconds}s 内：源峰值 {best_sources} · 活跃连接峰值 {best_peers} · 已校验 {best_bytes} 字节")
        ok = best_sources > 0
        if not ok:
            print("[A stderr] " + " | ".join(a.stderr[-6:]))
            print("[B stderr] " + " | ".join(b.stderr[-6:]))
        print("[结果]", "PASS（真实网络已发现源）" if ok else "FAIL（未发现源）")
        return 0 if ok else 1
    finally:
        a.stop()
        b.stop()


if __name__ == "__main__":
    raise SystemExit(main())
