"""临时验证：系统证书信任（SteamTools 拦截场景）+ huggingface 真实下载。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import high_speed_downloader as sd  # noqa: E402

URL = "https://huggingface.co/openbmb/MiniCPM5-1B-GGUF/resolve/main/MiniCPM5-1B-F16.gguf"
SMALL = "https://huggingface.co/openbmb/MiniCPM5-1B-GGUF/resolve/main/README.md"


def main() -> int:
    # 1. 系统信任会话（默认）应能直连；certifi 会话应失败
    with sd.build_http_session(True) as session:
        resp = session.get("https://huggingface.co", timeout=15)
        print("[1] system-trust session →", resp.status_code)
        if resp.status_code != 200:
            print("FAIL: 系统证书会话未通过")
            return 1

    with sd.build_http_session(False) as session:
        try:
            session.get("https://huggingface.co", timeout=15)
            print("[2] certifi session → 意外成功（本机可能未拦截）")
        except Exception as exc:  # noqa: BLE001
            print("[2] certifi session 如预期失败:", type(exc).__name__)

    # 2. 探测用户截图里的 URL
    probe = sd.probe_url(URL, trust_system_ca=True)
    print("[3] probe:", probe.status_code, probe.total_size, probe.supports_range, probe.reason)
    if not probe.supports_range or not probe.total_size:
        print("FAIL: 探测未拿到 Range 支持")
        return 1

    # 3. 小文件跑完整流程（探测 → 异步分片 → 合并）
    out = ROOT / "out" / "hf_readme.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    for stray in out.parent.glob(out.name + ".*"):
        stray.unlink()
    if out.exists():
        out.unlink()

    events = []
    engine = sd.DownloadEngine(SMALL, str(out), shards=4, retry=2, trust_system_ca=True)
    engine.on_event = lambda kind, text: events.append((kind, text))
    engine.start()
    engine.wait(120)
    print("[4] events:", [k for k, _ in events])
    for kind, text in events:
        if kind in ("degrade", "error"):
            print("    ", kind, text[:200])
    if not out.exists():
        print("FAIL: 小文件未下载成功")
        return 1
    print("[4] small file ok:", out.stat().st_size, "bytes")

    out.unlink()
    for stray in out.parent.glob(out.name + ".*"):
        stray.unlink()
    print("[PASS] 系统证书信任与下载链路验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
