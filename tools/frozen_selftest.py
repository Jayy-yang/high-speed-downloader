"""打包验证：把一个「自检用」的冻结 EXE 跑起来，确认无 Python 环境也能下载。

构建：
    python -m PyInstaller --onefile --console --noupx \
        --name HighSpeedDownloaderSelftest \
        --paths <项目目录> --distpath <项目目录>/dist-selftest \
        --workpath <项目目录>/build-selftest --specpath <项目目录>/build-selftest \
        tools/frozen_selftest.py

运行（会自动起本地测试服务）：
    HighSpeedDownloaderSelftest.exe
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from pathlib import Path

import aiohttp
import requests

import high_speed_downloader as sd

URL = "http://127.0.0.1:8765/range.bin"
OUT = Path("C:/Temp/_frozen_selftest/range.bin")
SIZE = 16 * 1024 * 1024
BODY = bytes(((i * 37) + 11) & 0xFF for i in range(SIZE))


def start_test_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import re

    range_re = re.compile(r"bytes=(\d+)-(\d+)")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(SIZE))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            raw = self.headers.get("Range")
            if raw:
                match = range_re.search(raw)
                start = int(match.group(1))
                end = min(int(match.group(2)), SIZE - 1)
                status = 206
            else:
                start, end, status = 0, SIZE - 1, 200
            self.send_response(status)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                self.wfile.write(BODY[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    print("frozen=", getattr(sys, "frozen", False), flush=True)
    print("python=", sys.version.split()[0], flush=True)
    print("aiohttp=", aiohttp.__version__, "requests=", requests.__version__, flush=True)

    httpd = start_test_server()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    parts = OUT.parent / (OUT.name + ".parts")
    if parts.exists():
        for item in parts.iterdir():
            item.unlink()
        parts.rmdir()

    events = []
    engine = sd.DownloadEngine(URL, str(OUT), shards=4, concurrency=8, retry=2)
    engine.on_event = lambda kind, text: (events.append(kind), print(f"[{kind}] {text}", flush=True))
    started = time.time()
    engine.start()
    engine.wait()
    elapsed = time.time() - started

    httpd.shutdown()

    if not OUT.exists():
        print("[FAIL] 输出文件不存在", flush=True)
        return 1
    data = OUT.read_bytes()
    print(f"[size] {len(data)} bytes in {elapsed:.1f}s", flush=True)
    print(f"[sha256] {hashlib.sha256(data).hexdigest()}", flush=True)
    print(f"[events] {events}", flush=True)
    if len(data) != SIZE or data != BODY:
        print("[FAIL] 内容与源不一致", flush=True)
        return 1
    if parts.exists() and any(parts.iterdir()):
        print("[FAIL] 临时文件未清理", flush=True)
        return 1
    print("FROZEN_SELFTEST_PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
