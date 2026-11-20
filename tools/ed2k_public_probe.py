"""eD2K 公网实链验收：用公开文件自建链接，验证「解析→找源→下载→哈希校验」。

做法不依赖任何来路不明的链接：
1. 从 SourceForge 下载官方 eMule 安装包（开源项目的官方产物），记录 sha256 作为真值；
2. 用本项目的引擎算它的 eD2K MD4，拼出标准 ed2k 链接；
3. 再用引擎按这个链接去公网下载，完成则比对 sha256 —— 全程可验证、内容可追溯。

用法::

    python tools/ed2k_public_probe.py --seconds 240
"""

from __future__ import annotations

import argparse
import hashlib
import ssl
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "out" / f"public-{time.strftime('%H%M%S')}"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ed2k_loopback import Engine  # noqa: E402

SOURCE_URL = "https://downloads.sourceforge.net/project/emule/eMule/0.50a/eMule0.50a-Installer.exe"


class SystemTrustAdapter(HTTPAdapter):
    """让 requests 信任 Windows 证书库（本机有 TLS 拦截时也能用）。"""

    def __init__(self, context: ssl.SSLContext, **kwargs) -> None:
        self._context = context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)


def fetch_reference(target: Path) -> str:
    """下载官方安装包并返回 sha256。"""
    if not target.exists():
        session = requests.Session()
        session.mount("https://", SystemTrustAdapter(ssl.create_default_context()))
        with session.get(SOURCE_URL, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                for chunk in resp.iter_content(256 * 1024):
                    handle.write(chunk)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="eD2K 公网实链验收")
    parser.add_argument("--seconds", type=int, default=240, help="等待下载完成的时长")
    arguments = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    reference = WORK / "reference" / "eMule0.50a-Installer.exe"
    expected = fetch_reference(reference)
    print(f"[参考文件] {reference.name} {reference.stat().st_size} 字节 sha256={expected[:16]}…")

    engine = Engine("P", WORK / "p", WORK / "state-p", 4664)
    try:
        engine.wait_for(lambda e: e.get("event") == "ready", 20, "引擎就绪")
        engine.send({"cmd": "hash", "path": str(reference)})
        hashed = engine.wait_for(lambda e: e.get("event") == "hash", 60, "计算 eD2K 哈希")
        link = hashed["link"]
        print(f"[自建链接] {link}")

        engine.send(
            {
                "cmd": "start",
                "link": link,
                "out_dir": str(WORK / "download"),
                "state_dir": str(WORK / "state-p"),
                "tcp_port": 4664,
                "udp_port": 4674,
                "use_kad": True,
                "offline": False,
                "seed": True,
                "max_peers": 12,
            }
        )

        deadline = time.time() + arguments.seconds
        best = {"sources": 0, "peers": 0, "verified": 0}
        while time.time() < deadline:
            for event in list(engine.events):
                if event.get("event") == "progress":
                    best["sources"] = max(best["sources"], int(event.get("sources") or 0))
                    best["peers"] = max(best["peers"], int(event.get("peers") or 0))
                    best["verified"] = max(best["verified"], int(event.get("verified") or 0))
                elif event.get("event") == "done":
                    path = Path(event["path"])
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    print(f"[完成] {path} {event['size']} 字节")
                    print(f"[校验] sha256 {'一致' if actual == expected else '不一致'}")
                    print("[结果]", "PASS" if actual == expected else "FAIL")
                    return 0 if actual == expected else 1
            if engine.process.poll() is not None:
                print("[引擎退出]", engine.process.returncode)
                break
            time.sleep(1.0)

        print(f"[超时] 源峰值 {best['sources']} · 连接峰值 {best['peers']} · 已校验 {best['verified']} 字节")
        print("[stderr]", " | ".join(engine.stderr[-5:]))
        print("[结果]", "PARTIAL（链路可用，但本次未在时限内下完）" if best["verified"] else "FAIL（没找到源）")
        return 2 if best["verified"] else 1
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(main())
