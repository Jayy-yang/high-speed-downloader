"""临时验证：meta.json 丢失时，能否靠残留分片反推布局继续续传。

场景一（安全中止）：分片大小不一致、反推不出布局 → 必须报错，不能拼错文件。
场景二（正常反推）：只有最后一片没下满 → 反推旧布局，续传后内容哈希正确。
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

OUT = ROOT / "out" / "infer.bin"
EXPECTED = hashlib.sha256(BODY).hexdigest()


def wait(engine) -> None:
    while engine.is_running():
        time.sleep(0.05)


def clean() -> Path:
    parts = OUT.with_name(OUT.name + ".parts")
    if parts.exists():
        for item in parts.iterdir():
            item.unlink()
        parts.rmdir()
    if OUT.exists():
        OUT.unlink()
    return parts


def case_ambiguous(parts: Path, url: str) -> None:
    parts.mkdir(parents=True, exist_ok=True)
    (parts / "part.0").write_bytes(BODY[: 400 * 1024])
    (parts / "part.1").write_bytes(BODY[400 * 1024: 900 * 1024])
    events = []
    engine = sd.DownloadEngine(url, str(OUT), concurrency=8, shards=16, retry=1)
    engine.on_event = lambda kind, text: events.append((kind, text))
    engine.start()
    wait(engine)
    health = [t for k, t in events if k == "error"]
    assert health, f"应当报错而不是继续：{events}"
    assert "meta.json" in health[0], f"错误提示没说清原因：{health[0]}"
    assert not OUT.exists(), "报错时不应生成输出文件"
    print("[1] 分片不一致时安全中止：", health[0].splitlines()[0])


def case_infer(parts: Path, url: str) -> None:
    parts.mkdir(parents=True, exist_ok=True)
    chunk = SIZE // 4
    for index in range(4):
        start = index * chunk
        end = min(SIZE, start + chunk)
        piece = BODY[start:end] if index < 3 else BODY[start:start + chunk // 2]
        (parts / f"part.{index}").write_bytes(piece)
    events = []
    engine = sd.DownloadEngine(url, str(OUT), concurrency=8, shards=16, retry=2)
    engine.on_event = lambda kind, text: events.append((kind, text))
    engine.start()
    wait(engine)
    texts = [t for _, t in events]
    kinds = [k for k, _ in events]
    assert "done" in kinds, f"未完成：{events}"
    assert any("反推布局" in t for t in texts), f"没有反推提示：{texts}"
    assert OUT.stat().st_size == SIZE
    assert hashlib.sha256(OUT.read_bytes()).hexdigest() == EXPECTED, "内容哈希不符"
    leftover = sorted(p.name for p in OUT.parent.glob("infer*"))
    assert leftover == ["infer.bin"], f"临时文件未清理：{leftover}"
    print("[2] 反推布局续传成功，哈希一致")


def main() -> int:
    server, port = start_server("range", 0)
    url = f"http://127.0.0.1:{port}/file.bin"
    try:
        clean()
        case_ambiguous(clean(), url)
        case_infer(clean(), url)
    finally:
        server.shutdown()
    print("[PASS] meta 丢失场景：不一致时安全中止，可反推时正常续传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
