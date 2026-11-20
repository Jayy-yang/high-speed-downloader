"""eD2K 引擎联调脚本：拉起引擎、发指令、按行读事件。

用法::

    python tools/run_ed2k_engine.py --seconds 60
    python tools/run_ed2k_engine.py --link "ed2k://|file|..." --out "D:\\Downloads\\ed2k" --seconds 120

默认用一条假链接（哈希全零之外的值），只验证「登录服务器 + Kad + 事件通道」；
给了 ``--link`` 才真正下载。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "ed2k-engine" / "engine.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="eD2K 引擎联调")
    parser.add_argument("--link", default="ed2k://|file|TEST.BIN|1048576|0123456789ABCDEF0123456789ABCDEF|/")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "ed2k-test"))
    parser.add_argument("--state", default=str(Path(tempfile.gettempdir()) / "ed2k-state"))
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument("--port", type=int, default=4662)
    parser.add_argument("--udp-port", type=int, default=4672)
    parser.add_argument("--no-kad", action="store_true")
    parser.add_argument("--seed", action="store_true")
    arguments = parser.parse_args()

    command = [sys.executable, str(ENGINE)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin and process.stdout and process.stderr

    events: list[dict] = []
    started = time.time()

    def read_events() -> None:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                print("[RAW]", line)
                continue
            events.append(payload)
            stamp = time.time() - started
            kind = payload.get("event") or "reply"
            detail = {k: v for k, v in payload.items() if k != "event"}
            print(f"[{stamp:6.1f}s] {kind}: {json.dumps(detail, ensure_ascii=False)}", flush=True)

    def read_stderr() -> None:
        for line in process.stderr:
            print("[stderr]", line.rstrip(), flush=True)

    readers = [
        threading.Thread(target=read_events, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for thread in readers:
        thread.start()

    start_command = {
        "id": 1,
        "cmd": "start",
        "link": arguments.link,
        "out_dir": arguments.out,
        "state_dir": arguments.state,
        "tcp_port": arguments.port,
        "udp_port": arguments.udp_port,
        "use_kad": not arguments.no_kad,
        "seed": arguments.seed,
        "max_peers": 8,
    }
    process.stdin.write(json.dumps(start_command, ensure_ascii=False) + "\n")
    process.stdin.flush()

    deadline = started + arguments.seconds
    while time.time() < deadline:
        if any(item.get("event") in {"done", "error"} and item.get("fatal") for item in events):
            break
        time.sleep(0.2)

    process.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
    process.stdin.flush()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        print("[warn] 引擎未在 15 秒内退出，已强制结束", flush=True)

    print("--- 事件统计 ---")
    counts: dict[str, int] = {}
    for item in events:
        kind = item.get("event", "reply")
        counts[kind] = counts.get(kind, 0) + 1
    print(json.dumps(counts, ensure_ascii=False))
    net = [item for item in events if item.get("event") == "net"]
    if net:
        print("最后一条 net:", json.dumps(net[-1], ensure_ascii=False))
        return 0 if net[-1].get("servers") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
