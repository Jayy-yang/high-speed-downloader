"""临时验证脚本：检查“设置默认保存目录”的读取、保存与自动刷新。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import high_speed_downloader as sd  # noqa: E402

errors: list[str] = []
sd.messagebox.showerror = lambda *a, **k: errors.append(a[1] if len(a) > 1 else str(a))
sd.messagebox.askyesno = lambda *a, **k: True


def main() -> int:
    import shutil

    tmp = ROOT / "tools" / "_tmp_settings"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        return _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp: Path) -> int:
    if True:
        os.environ["APPDATA"] = str(tmp / "appdata")

        target = tmp / "MyDownloads"
        target.mkdir()

        root = tk.Tk()
        root.withdraw()
        app = sd.DownloadApp(root)

        # 1. 默认值 = 系统下载目录
        assert app.download_dir == sd.system_downloads_dir(), app.download_dir
        print("[1] 默认目录 =", app.download_dir)

        # 2. 粘贴 URL 后自动填充到默认目录
        app.url_var.set("https://example.com/files/tool.zip")
        app._fill_default_output()
        assert Path(app.output_var.get()).parent == app.download_dir
        print("[2] 自动填充 =", app.output_var.get())

        # 3. 保存新目录：写配置文件 + “保存到”跟着刷新
        assert app.apply_download_dir(str(target))
        cfg = json.loads(sd.config_path().read_text(encoding="utf-8"))
        assert cfg["download_dir"] == str(target), cfg
        assert Path(app.output_var.get()).parent == target, app.output_var.get()
        print("[3] 配置已写入", sd.config_path(), "→", cfg["download_dir"])

        # 4. 新实例启动时读取配置
        root2 = tk.Tk()
        root2.withdraw()
        app2 = sd.DownloadApp(root2)
        assert app2.download_dir == target, app2.download_dir
        print("[4] 重启后读取 =", app2.download_dir)

        # 5. 目录不存在时自动创建
        newdir = tmp / "nested" / "downloads"
        assert app2.apply_download_dir(str(newdir))
        assert newdir.is_dir()
        print("[5] 不存在目录已创建 =", newdir)

        # 6. 指向文件时拒绝
        bad = tmp / "file.txt"
        bad.write_text("x", encoding="utf-8")
        assert not app2.apply_download_dir(str(bad))
        print("[6] 非目录被拒绝，错误提示 =", errors[-1] if errors else None)

        # 7. 非法输入为空时拒绝
        assert not app2.apply_download_dir("   ")
        print("[7] 空路径被拒绝")

        # 8. 退出时记住下载参数与保存路径
        target_file = tmp / "remembered.bin"
        app2.shards_var.set(64)
        app2.concurrency_var.set(48)
        app2.retry_var.set(5)
        app2.chunk_mb_var.set("8")
        app2.output_var.set(str(target_file))
        app2._save_ui_state()
        saved = json.loads(sd.config_path().read_text(encoding="utf-8"))
        assert saved["shards"] == 64 and saved["retry"] == 5, saved
        assert saved["chunk_mb"] == "8", saved
        assert saved["concurrency"] == 48, saved
        assert "output" not in saved, saved
        print("[8] 已记忆：分片数/并发连接/重试次数/分片大小（不记文件名）")

        # 9. 重启后原样恢复
        root3 = tk.Tk()
        root3.withdraw()
        app3 = sd.DownloadApp(root3)
        assert app3.shards_var.get() == 64, app3.shards_var.get()
        assert app3.concurrency_var.get() == 48, app3.concurrency_var.get()
        assert app3.retry_var.get() == 5, app3.retry_var.get()
        assert app3.chunk_mb_var.get() == "8", app3.chunk_mb_var.get()
        assert app3.output_var.get() == "", app3.output_var.get()
        app3.url_var.set("https://example.com/files/tool.zip")
        app3._fill_default_output()
        assert Path(app3.output_var.get()).parent == app3.download_dir, app3.output_var.get()
        print("[9] 重启后不记文件名，按 URL 重新生成 =", app3.shards_var.get(), app3.retry_var.get(),
              app3.chunk_mb_var.get(), app3.output_var.get())

        # 10. 最高分片数：硬上限 256，且每片不小于 0.5 MB
        assert sd.max_shards_for_size(0) == 256
        assert sd.max_shards_for_size(590 * 1024 * 1024) == 256
        assert sd.max_shards_for_size(100 * 1024 * 1024) == 200
        assert sd.max_shards_for_size(1024 * 1024) == 2
        assert sd.max_shards_for_size(300 * 1024) == 1
        print("[10] 最高分片数换算正确（590MB→256，100MB→200，1MB→2）")

        # 11. 超限提示变橙色、不夹取输入
        app3._max_shards = 20
        app3.shards_var.set(30)
        assert "超过上限" in app3.shard_hint_var.get(), app3.shard_hint_var.get()
        assert str(app3.shard_hint_label.cget("style")) == "Warn.TLabel"
        assert app3.shards_var.get() == 30
        app3.shards_var.set(5)
        assert app3.shard_hint_var.get() == "最高 20 片", app3.shard_hint_var.get()
        assert str(app3.shard_hint_label.cget("style")) == "Hint.TLabel"
        print("[11] 超限只警告不夹取：", app3.shard_hint_var.get())

        # 12. 探测到总大小后，“最高 N 片”按 0.5 MB 最小分片收紧
        class FakeEngine:
            def __init__(self, size: int) -> None:
                self.last_probe = sd.ProbeResult(
                    final_url="https://example.com/x.bin", status_code=200,
                    total_size=size, supports_range=True, reason="test",
                )

            def snapshot(self):
                return 0, 16 * 1024 * 1024, 0.0

        app3.engine = FakeEngine(16 * 1024 * 1024)
        app3._poll_events()
        assert app3._max_shards == 32, app3._max_shards
        assert app3.shard_hint_var.get() == "最高 32 片", app3.shard_hint_var.get()
        print("[12] 探测 16 MB 后上限收紧为", app3._max_shards, "片")
        app3.engine = None

        # 13. 证书信任开关：默认开启、可关闭、重启后保持
        assert app3.trust_system_ca is True
        app3.apply_trust_system_ca(False)
        assert json.loads(sd.config_path().read_text(encoding="utf-8"))["trust_system_ca"] is False
        root4 = tk.Tk()
        root4.withdraw()
        app4 = sd.DownloadApp(root4)
        assert app4.trust_system_ca is False, app4.trust_system_ca
        app4.apply_trust_system_ca(True)
        assert json.loads(sd.config_path().read_text(encoding="utf-8"))["trust_system_ca"] is True
        print("[13] 证书信任开关可关闭并记忆")

        root4.destroy()

        root.destroy()
        root2.destroy()
        root3.destroy()

    print("ALL PASS: settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
