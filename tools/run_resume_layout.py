"""临时验证：改了分片数之后还能不能接着上次的断点续传。

不属于交付物，仅开发期自测。
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import high_speed_downloader as sd  # noqa: E402
from test_server import BODY, SIZE, start_server  # noqa: E402

OUT = ROOT / "out" / "resume_layout.bin"
EXPECTED = hashlib.sha256(BODY).hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait(engine, timeout=120.0) -> None:
    deadline = time.time() + timeout
    while engine.is_running() and time.time() < deadline:
        time.sleep(0.05)
    assert not engine.is_running(), "engine timeout"


def main() -> int:
    for stale in OUT.parent.glob("resume_layout*"):
        stale.unlink()
    server, port = start_server("range", 384 * 1024)
    url = f"http://127.0.0.1:{port}/file.bin"

    # 第一轮：6 分片、2 并发，下 1.2 秒就取消，留下断点
    first = []
    engine = sd.DownloadEngine(url, str(OUT), concurrency=2, shards=6, retry=2)
    engine.on_event = lambda kind, text: first.append((kind, text))
    engine.start()
    time.sleep(1.2)
    engine.cancel()
    wait(engine, 30)
    parts = sorted((OUT.with_name(OUT.name + ".parts")).glob("part.*"))
    sizes = {p.name: p.stat().st_size for p in parts}
    print("[1] 取消后分片：", sizes)
    assert any(sizes.values()), "第一轮没有留下任何数据"

    # 第二轮：故意把分片数改成 16，期望沿用旧布局继续下
    server.RequestHandlerClass.throttle = 0
    second = []
    engine = sd.DownloadEngine(url, str(OUT), concurrency=8, shards=16, retry=2)
    engine.on_event = lambda kind, text: second.append((kind, text))
    engine.start()
    wait(engine, 120)
    server.shutdown()

    texts = [t for _, t in second]
    kinds = [k for k, _ in second]
    for kind, text in second:
        print(f"    [{kind}] {text}")

    assert "done" in kinds, f"第二轮未完成：{second}"
    assert any("沿用" in t for t in texts), "没有出现“沿用旧布局”的提示"
    assert OUT.stat().st_size == SIZE, f"大小不符：{OUT.stat().st_size} != {SIZE}"
    assert sha256_of(OUT) == EXPECTED, "内容哈希不符"
    leftover = list(OUT.parent.glob("resume_layout*"))
    assert leftover == [OUT], f"临时文件未清理：{leftover}"
    print("[PASS] 改分片数后续传成功，内容哈希一致，临时文件已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
