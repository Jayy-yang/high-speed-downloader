"""HighSpeedDownloader 安装程序（自解压安装 / 卸载）。

打包后是单文件 `HighSpeedDownloader-Setup-x.y.z.exe`，内部带一个 payload.zip（直装包的全部内容）。

设计取向：
* 装到当前用户目录（``%LOCALAPPDATA%\\Programs\\HighSpeedDownloader``），不需要管理员权限；
* 只写 HKCU 的卸载项，不碰系统目录、不写服务、不改 PATH；
* 卸载 = 删自己装出来的文件和两个快捷方式，再删注册表项，不做任何“清理无关文件”的动作。

用法：
    直接双击 → 图形安装
    Setup.exe /uninstall → 图形卸载（也是 HKCU 里的 UninstallString）
    Setup.exe /silent → 静默安装到默认目录（不建桌面快捷方式）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

APP_NAME = "HighSpeedDownloader"
APP_VERSION = "1.1.1"
APP_ID = "HighSpeedDownloader"
PUBLISHER = "Jayy Yang"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
MARKER_FILE = ".hsd-install"


def default_install_dir() -> Path:
    """默认安装目录：当前用户的 Programs 目录。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_NAME


def payload_path() -> Path:
    """payload.zip 的位置：PyInstaller 解包后位于 ``_MEIPASS`` 下。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "payload.zip"


def user_desktop() -> Path:
    """桌面目录（兼容 OneDrive 重定向）。"""
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        candidate = Path(onedrive) / "Desktop"
        if candidate.is_dir():
            return candidate
    return Path(profile) / "Desktop"


def start_menu_dir() -> Path:
    """开始菜单程序目录。"""
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def create_shortcut(link: Path, target: Path, workdir: Path, description: str) -> None:
    """用 WScript.Shell 创建 .lnk（不依赖第三方库）。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{target}';$s.WorkingDirectory='{workdir}';"
        "$s.IconLocation='{icon}';$s.Description='{desc}';$s.Save()"
    ).format(
        link=str(link).replace("'", "''"),
        target=str(target).replace("'", "''"),
        workdir=str(workdir).replace("'", "''"),
        icon=str(target).replace("'", "''"),
        desc=description.replace("'", "''"),
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        creationflags=0x08000000,
    )


def write_uninstall_entry(install_dir: Path, uninstaller: Path) -> None:
    """写 HKCU 卸载项（只写自己的键）。"""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        values = {
            "DisplayName": APP_NAME,
            "DisplayVersion": APP_VERSION,
            "Publisher": PUBLISHER,
            "InstallLocation": str(install_dir),
            "DisplayIcon": str(install_dir / "HighSpeedDownloader.exe"),
            "UninstallString": f'"{uninstaller}" /uninstall',
            "NoModify": 1,
            "NoRepair": 1,
        }
        for name, value in values.items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)


def remove_uninstall_entry() -> None:
    """删除自己的卸载项。"""
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def install(install_dir: Path, desktop_shortcut: bool, progress=lambda text: None) -> None:
    """把 payload 解到目标目录并创建快捷方式。"""
    archive = payload_path()
    if not archive.is_file():
        raise RuntimeError(f"安装包损坏：找不到 {archive}")

    install_dir.mkdir(parents=True, exist_ok=True)
    progress("正在解压文件 ...")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(install_dir)

    (install_dir / MARKER_FILE).write_text(APP_VERSION, encoding="utf-8")

    exe = install_dir / "HighSpeedDownloader.exe"
    if not exe.is_file():
        raise RuntimeError("安装包内容不完整：缺少 HighSpeedDownloader.exe")

    progress("正在创建快捷方式 ...")
    create_shortcut(
        start_menu_dir() / f"{APP_NAME}.lnk", exe, install_dir, "HighSpeedDownloader 下载器"
    )
    if desktop_shortcut:
        create_shortcut(user_desktop() / f"{APP_NAME}.lnk", exe, install_dir, "HighSpeedDownloader 下载器")

    # 卸载程序就是安装包自身的副本
    progress("正在写入卸载信息 ...")
    uninstaller = install_dir / "Uninstall.exe"
    try:
        shutil.copy2(Path(sys.executable), uninstaller)
    except OSError:
        pass
    try:
        write_uninstall_entry(install_dir, uninstaller)
    except OSError:
        # 个别环境（受限令牌、组策略）禁止写 HKCU：不阻断安装，用户仍可从目录里手动卸载
        pass


def uninstall(install_dir: Path, progress=lambda text: None) -> None:
    """删除安装目录、快捷方式与注册表项。"""
    progress("正在删除快捷方式 ...")
    for link in (start_menu_dir() / f"{APP_NAME}.lnk", user_desktop() / f"{APP_NAME}.lnk"):
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    remove_uninstall_entry()

    progress("正在删除程序文件 ...")
    if install_dir.exists():
        # 先删内容，最后让一个独立的 cmd 删掉目录本身（此时程序自己还在目录里）
        staging = Path(tempfile.gettempdir()) / f"hsd-remove-{os.getpid()}.cmd"
        staging.write_text(
            "@echo off\r\ntimeout /t 2 /nobreak >nul\r\n"
            f'rmdir /s /q "{install_dir}"\r\ndel "%~f0"\r\n',
            encoding="mbcs",
            errors="replace",
        )
        subprocess.Popen(
            ["cmd", "/c", str(staging)],
            creationflags=0x08000000 | 0x00000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
        )


class InstallerWindow:
    """极简安装向导：选目录 → 勾选快捷方式 → 安装。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(f"{APP_NAME} {APP_VERSION} 安装程序")
        root.resizable(False, False)
        root.option_add("*Font", "{Microsoft YaHei UI} 10")

        body = ttk.Frame(root, padding=18)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=f"安装 {APP_NAME} {APP_VERSION}", font=("Microsoft YaHei UI", 14, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            body,
            text="aiohttp 异步分片 + requests/threading 同步兜底，另含 eD2K 组件。\n"
                 "安装到当前用户目录，不需要管理员权限。",
            foreground="#666666",
            justify="left",
        ).pack(anchor="w", pady=(4, 14))

        ttk.Label(body, text="安装位置").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(4, 10))
        self.dir_var = tk.StringVar(value=str(default_install_dir()))
        ttk.Entry(row, textvariable=self.dir_var, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", width=8, command=self._browse).pack(side="left", padx=(6, 0))

        self.desktop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="创建桌面快捷方式", variable=self.desktop_var).pack(anchor="w")

        self.progress_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.progress_var, foreground="#666666").pack(
            anchor="w", pady=(12, 0)
        )

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(16, 0))
        self.install_button = ttk.Button(actions, text="安装", width=10, command=self._install)
        self.install_button.pack(side="right")
        ttk.Button(actions, text="取消", width=10, command=root.destroy).pack(
            side="right", padx=(0, 8)
        )

        root.update_idletasks()
        width, height = root.winfo_width(), root.winfo_height()
        x = (root.winfo_screenwidth() - width) // 2
        y = (root.winfo_screenheight() - height) // 3
        root.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title="选择安装位置", initialdir=self.dir_var.get()
        )
        if chosen:
            self.dir_var.set(chosen)

    def _install(self) -> None:
        target = Path(self.dir_var.get()).expanduser()
        if target.exists() and not target.is_dir():
            messagebox.showerror("安装", "安装位置必须是一个目录", parent=self.root)
            return
        self.install_button.configure(state="disabled")

        def worker() -> None:
            try:
                install(target, bool(self.desktop_var.get()), self._report)
            except Exception as exc:  # 安装失败统一提示
                self.root.after(0, lambda: self._failed(exc))
                return
            self.root.after(0, lambda: self._done(target))

        threading.Thread(target=worker, daemon=True).start()

    def _report(self, text: str) -> None:
        self.root.after(0, lambda: self.progress_var.set(text))

    def _failed(self, exc: BaseException) -> None:
        self.install_button.configure(state="normal")
        self.progress_var.set("")
        messagebox.showerror("安装失败", str(exc), parent=self.root)

    def _done(self, target: Path) -> None:
        messagebox.showinfo(
            "安装完成",
            f"{APP_NAME} 已安装到：\n{target}\n\n可以从开始菜单或桌面快捷方式启动。",
            parent=self.root,
        )
        self.root.destroy()


def run_uninstall_gui() -> int:
    """卸载确认窗口。"""
    root = tk.Tk()
    root.title(f"卸载 {APP_NAME}")
    root.resizable(False, False)
    root.option_add("*Font", "{Microsoft YaHei UI} 10")

    install_dir = default_install_dir()
    body = ttk.Frame(root, padding=18)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=f"卸载 {APP_NAME}", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    ttk.Label(
        body,
        text=f"将从以下目录删除程序文件与快捷方式：\n{install_dir}\n\n个人下载的文件不会被删除。",
        justify="left",
        foreground="#444444",
    ).pack(anchor="w", pady=(8, 14))

    status = tk.StringVar(value="")
    ttk.Label(body, textvariable=status, foreground="#666666").pack(anchor="w")

    actions = ttk.Frame(body)
    actions.pack(fill="x", pady=(16, 0))

    def do_uninstall() -> None:
        status.set("正在卸载 ...")
        uninstall(install_dir, lambda text: status.set(text))
        messagebox.showinfo("卸载完成", "程序文件已删除，窗口关闭后目录会被清理。", parent=root)
        root.destroy()

    ttk.Button(actions, text="卸载", width=10, command=do_uninstall).pack(side="right")
    ttk.Button(actions, text="取消", width=10, command=root.destroy).pack(side="right", padx=(0, 8))

    root.update_idletasks()
    width, height = root.winfo_width(), root.winfo_height()
    root.geometry(
        f"+{max((root.winfo_screenwidth() - width) // 2, 0)}+"
        f"{max((root.winfo_screenheight() - height) // 3, 0)}"
    )
    root.mainloop()
    return 0


def main() -> int:
    """入口：/uninstall 走卸载，/silent 静默安装，否则打开安装向导。"""
    arguments = {item.lower() for item in sys.argv[1:]}
    if "/uninstall" in arguments or "--uninstall" in arguments:
        return run_uninstall_gui()
    if "/silent" in arguments:
        try:
            install(default_install_dir(), desktop_shortcut=False, progress=print)
        except Exception as exc:
            print(f"安装失败：{exc}", file=sys.stderr)
            return 1
        return 0

    root = tk.Tk()
    InstallerWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
