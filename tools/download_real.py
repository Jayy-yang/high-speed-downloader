"""临时脚本：用真实外网 URL 做验收，并抽样比对首尾字节。

用法（默认续传，不删临时分片）：
    python tools/download_real.py [--concurrency 32] [--shards 8] [--fresh]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import high_speed_downloader as sd  # noqa: E402

URL = "https://installers.lmstudio.ai/win32/x64/0.4.26-4/LM-Studio-0.4.26-4-x64.exe"


def on_event(kind: str, text: str) -> None:
    print(f"[{kind}] {text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--fresh", action="store_true", help="删除已有临时分片，从头下载")
    args = parser.parse_args()

    out = ROOT / "out" / "lmstudio.exe"
    parts = out.parent / (out.name + ".parts")

    if args.fresh:
        if out.exists():
            out.unlink()
        if parts.exists():
            shutil.rmtree(parts)

    engine = sd.DownloadEngine(
        URL, str(out), shards=args.shards, concurrency=args.concurrency, retry=3
    )
    engine.on_event = on_event
    started = time.time()
    engine.start()
    while engine.is_running():
        time.sleep(2.0)
        done, total, speed = engine.snapshot()
        pct = done / total * 100 if total else 0
        print(
            f"[progress] {pct:5.1f}%  {sd.format_bytes(done)} / {sd.format_bytes(total)}"
            f"  {sd.format_speed(speed)}  {time.time() - started:.0f}s",
            flush=True,
        )
    engine.wait()

    done, total, _ = engine.snapshot()
    elapsed = time.time() - started
    print(f"[final] done={done} total={total} elapsed={elapsed:.0f}s", flush=True)
    if not out.exists():
        print("[FAIL] 输出文件不存在", flush=True)
        return 1
    size = out.stat().st_size
    print(f"[size] {size} bytes ({sd.format_bytes(size)})", flush=True)
    if size != total:
        print("[FAIL] 文件大小与 Content-Length 不一致", flush=True)
        return 1

    leftovers = sorted(p.name for p in parts.iterdir()) if parts.exists() else []
    print(f"[cleanup] parts_dir_exists={parts.exists()} leftovers={leftovers}", flush=True)
    if leftovers:
        print("[FAIL] 临时分片未清理", flush=True)
        return 1

    # 抽样校验首尾各 64KB，与服务器 Range 响应逐字节比对
    headers = {"User-Agent": sd.DEFAULT_UA, "Accept-Encoding": "identity"}
    for label, start, end in (("head", 0, 65535), ("tail", size - 65536, size - 1)):
        resp = requests.get(
            URL, headers={**headers, "Range": f"bytes={start}-{end}"}, timeout=(10, 60)
        )
        remote = resp.content
        with open(out, "rb") as file_obj:
            file_obj.seek(start)
            local = file_obj.read(len(remote))
        print(
            f"[verify-{label}] status={resp.status_code} remote={len(remote)} "
            f"match={remote == local}",
            flush=True,
        )
        if remote != local:
            print("[FAIL] 抽样字节不一致", flush=True)
            return 1

    print("[PASS] 外网验收通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
