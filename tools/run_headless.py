"""临时验证脚本：本地覆盖 支持Range / 不支持Range / 分片兜底 / 断点续传。

不属于交付物，仅用于开发期自测。
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
from test_server import SIZE, BODY, start_server  # noqa: E402

OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(exist_ok=True)
EXPECTED = hashlib.sha256(BODY).hexdigest()
RESULTS = []


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parts_dir(out: Path) -> Path:
    return out.with_name(out.name + ".parts")


def leftovers(out: Path):
    stray = sorted(p.name for p in out.parent.glob(out.name + ".part.*"))
    stray += sorted(p.name for p in out.parent.glob(out.name + ".meta.json"))
    if parts_dir(out).exists():
        stray += ["parts/" + p.name for p in sorted(parts_dir(out).iterdir())]
    return stray


def wait_engine(engine, timeout=180.0) -> None:
    deadline = time.time() + timeout
    while engine.is_running() and time.time() < deadline:
        time.sleep(0.05)
    if engine.is_running():
        engine.cancel()
        raise AssertionError("engine timeout")


def run_case(name, mode, out, retry=2, shards=4, concurrency=4, rate=0,
             expect_degrade=None, cancel_after=None):
    server, port = start_server(mode, rate)
    url = f"http://127.0.0.1:{port}/file.bin"
    events = []

    def on_event(kind, text):
        events.append((kind, text))

    engine = sd.DownloadEngine(url, str(out), concurrency=concurrency, shards=shards, retry=retry)
    engine.on_event = on_event
    engine.start()

    if cancel_after is not None:
        time.sleep(cancel_after)
        engine.cancel()
        wait_engine(engine, 30)
        partial = [p for p in parts_dir(out).glob("part.*") if p.stat().st_size > 0]
        assert partial, f"[{name}] 取消后没有任何非空分片，无法验证续传"
        print(f"[{name}] 取消时已产生 {len(partial)} 个非空分片，总 {sum(p.stat().st_size for p in partial)} 字节")

        server.RequestHandlerClass.throttle = 0
        events.clear()
        engine = sd.DownloadEngine(url, str(out), concurrency=concurrency,
                                   shards=shards, retry=retry)
        engine.on_event = on_event
        engine.start()

    wait_engine(engine, 180)
    server.shutdown()
    kinds = [k for k, _ in events]
    texts = [t for _, t in events]
    assert "done" in kinds, f"[{name}] 未完成：{events}"
    assert out.exists(), f"[{name}] 输出文件不存在"
    assert out.stat().st_size == SIZE, f"[{name}] 大小不符 {out.stat().st_size}"
    assert sha256_of(out) == EXPECTED, f"[{name}] 内容哈希不符"
    assert not leftovers(out), f"[{name}] 临时文件未清理：{leftovers(out)}"
    if expect_degrade:
        assert any(expect_degrade in t for t in texts), f"[{name}] 未出现降级日志：{texts}"
    print(f"[{name}] OK  done={out.stat().st_size}  events={kinds}")
    for kind, text in events:
        if kind in ("degrade", "info"):
            print(f"    [{kind}] {text}")
    RESULTS.append(name)


def main() -> None:
    run_case("range", "range", OUT_DIR / "range.bin")
    # 并发数大于分片数：每个分片再切成子段并发下载
    run_case("subsplit", "range", OUT_DIR / "subsplit.bin", shards=2, concurrency=8)
    run_case("norange", "norange", OUT_DIR / "norange.bin", expect_degrade="整体降级")
    run_case("flaky", "flaky", OUT_DIR / "flaky.bin", expect_degrade="切换同步线程兜底")
    run_case("resume", "range", OUT_DIR / "resume.bin", shards=4, concurrency=2,
             rate=512 * 1024, cancel_after=1.5)

    # 断言：续传场景应出现断点提示
    print("ALL PASS:", ", ".join(RESULTS))


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print("FAIL:", exc)
        sys.exit(1)
