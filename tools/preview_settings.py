"""Temporary: launch the GUI, open the settings dialog, report its screen rect.

用于开发期截图：把对话框的屏幕坐标写到 settings_rect.txt，供 PowerShell 精确裁剪。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import high_speed_downloader as sd  # noqa: E402

RECT_FILE = Path(__file__).resolve().parent / "settings_rect.txt"


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """返回窗口（含标题栏）的屏幕矩形：先向上找到最外层窗口。"""
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    top = hwnd
    rect = RECT()
    ctypes.windll.user32.GetWindowRect(top, ctypes.byref(rect))
    # winfo_id 给的是客户区窗口，向外扩出标题栏与边框
    frame = ctypes.windll.user32.GetSystemMetrics(32)
    caption = ctypes.windll.user32.GetSystemMetrics(4)
    return (
        rect.left - frame,
        rect.top - caption - frame,
        rect.right - rect.left + frame * 2,
        rect.bottom - rect.top + caption + frame * 2,
    )


def main() -> None:
    sd.enable_dpi_awareness()
    root = tk.Tk()
    app = sd.DownloadApp(root)

    def open_dialog() -> None:
        app._open_settings()
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.attributes("-topmost", True)
                child.lift()
                child.focus_force()
                child.update_idletasks()
                rect = window_rect(child.winfo_id())
                RECT_FILE.write_text(" ".join(str(v) for v in rect), encoding="ascii")

    root.after(800, open_dialog)
    root.mainloop()


if __name__ == "__main__":
    main()
