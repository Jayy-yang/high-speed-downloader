"""临时测试服务：模拟三种服务端行为，仅供本地验证，不属于交付物。

模式：
  range   —— 正确响应 Range，返回 206 + Content-Range
  norange —— 忽略 Range，永远 200 全量
  flaky   —— 支持 Range，但偏移 4MiB 的分片前 3 次请求返回 500
"""

from __future__ import annotations

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIZE = 16 * 1024 * 1024
BODY = bytes(((i * 37) + 11) & 0xFF for i in range(SIZE))
FLAKY_START = 4 * 1024 * 1024

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")


def make_handler(mode: str, rate: int = 0):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        fail_counts = {}
        counter_lock = threading.Lock()
        throttle = rate

        def log_message(self, *args):  # 静默
            pass

        def _common_headers(self, length: int, ranges: bool) -> None:
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("ETag", '"test-etag"')
            if ranges:
                self.send_header("Accept-Ranges", "bytes")

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self._common_headers(SIZE, mode != "norange")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            raw_range = self.headers.get("Range")
            if mode == "norange" or not raw_range:
                start, end, status = 0, SIZE - 1, 200
            else:
                match = _RANGE_RE.search(raw_range)
                if not match:
                    self.send_error(400, "bad range")
                    return
                start, end = int(match.group(1)), min(int(match.group(2)), SIZE - 1)
                status = 206

                # flaky：让 4MiB 起始的分片前三次失败，逼迫异步重试耗尽后走同步兜底
                if mode == "flaky" and start == FLAKY_START:
                    with Handler.counter_lock:
                        count = Handler.fail_counts.get(start, 0) + 1
                        Handler.fail_counts[start] = count
                    if count <= 3:
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

            self.send_response(status)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
            self._common_headers(end - start + 1, mode != "norange")
            self.end_headers()

            payload = BODY[start:end + 1]
            throttle = self.throttle
            try:
                if throttle <= 0:
                    self.wfile.write(payload)
                else:
                    block = 64 * 1024
                    for offset in range(0, len(payload), block):
                        self.wfile.write(payload[offset:offset + block])
                        time.sleep(block / throttle)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端主动断开（例如探测 200 后立刻关闭）

    return Handler


def start_server(mode: str, rate: int = 0):
    """启动测试服务，返回 (httpd, port)。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(mode, rate))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


if __name__ == "__main__":
    server, port = start_server("range")
    print(f"serving on http://127.0.0.1:{port}/file.bin")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
