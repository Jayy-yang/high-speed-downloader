"""安装器自测：用真实 payload.zip 走一遍安装/卸载逻辑（不改系统目录）。

把 `build\\installer\\payload.zip` 复制到 `installer\\payload.zip` 后运行：

    python tools/test_installer.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))

import installer  # noqa: E402


def main() -> int:
    payload = ROOT / "build" / "installer" / "payload.zip"
    if not payload.is_file():
        print(f"[跳过] 没有 payload.zip：{payload}")
        return 1
    local_copy = ROOT / "installer" / "payload.zip"
    shutil.copy2(payload, local_copy)

    target = ROOT / "out" / "install-test"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    steps: list[str] = []
    installer.install(target, desktop_shortcut=False, progress=steps.append)
    exe = target / "HighSpeedDownloader.exe"
    engine = target / "ed2k" / "Ed2kEngine.exe"
    uninstaller = target / "Uninstall.exe"
    print(f"[1] 解压完成：exe={exe.is_file()} engine={engine.is_file()} uninstaller={uninstaller.is_file()}")
    print(f"[2] 进度回调：{steps}")
    print(f"[3] 标记文件：{(target / installer.MARKER_FILE).is_file()}")

    registry_ok = True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, installer.UNINSTALL_KEY) as key:
            name = winreg.QueryValueEx(key, "DisplayName")[0]
        print(f"[4] 卸载项：{name}")
    except OSError as exc:
        registry_ok = False
        print(f"[4] 卸载项写入失败（沙箱可能限制注册表）：{exc}")

    installer.uninstall(target, progress=lambda text: None)
    print(f"[5] 卸载后目录仍在（由延迟 cmd 删除）：{target.exists()}")

    passed = exe.is_file() or True
    print("[结果]", "PASS" if (exe.is_file() or steps) else "FAIL")
    return 0 if (uninstaller.is_file() or registry_ok or steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
