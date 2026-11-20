"""临时预览脚本：打开主界面并填入一条 ed2k 链接，用于截图验证 eD2K 相关布局。

仅开发期用，不属于发行物。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import high_speed_downloader as sd  # noqa: E402

LINK = (
    "ed2k://|file|ubuntu-24.04.1-desktop-amd64.iso|6303080440|"
    "8A1C7AB1E6B58C0A2E3E9E7A2F2D4C11|/"
)


def main() -> None:
    root = tk.Tk()
    app = sd.DownloadApp(root)
    app.url_var.set(LINK)
    root.after(900, lambda: app._apply_ed2k_link(LINK))
    root.after(
        1300,
        lambda: app._set_status(
            "eD2K：服务器 4 · 端口 4662 · Low-ID · 源 42 · 活跃 3 · Kad 594"
        ),
    )
    root.mainloop()


if __name__ == "__main__":
    main()
