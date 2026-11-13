# HighSpeedDownloader plan-v1.0.1：打包为独立 EXE

## 背景与目标

HighSpeedDownloader 目前以 `high_speed_downloader.py` + Python 运行。目标：产出**单文件 Windows EXE**，在**没有安装 Python 的机器**上双击即可运行（GUI），便于分发。

## 交付物

| 项 | 路径 |
| --- | --- |
| 可执行文件 | `D:\share\HighSpeedDownloader.exe`（同时保留项目内 `dist\HighSpeedDownloader.exe`） |
| 打包脚本 | `build_exe.ps1`（可重复构建） |
| PyInstaller 配置 | `HighSpeedDownloader.spec`、`version_info.txt` |
| 说明文档 | `README.md`「下载与安装」「自行打包」章节 |

## 技术方案

- PyInstaller 6.x，`--onefile --windowed --noupx`，图标 `download-arrow.ico`。
- 冻结 Python 3.14 运行时、aiohttp/requests/cryptography 的 DLL、Tcl/Tk（GUI 必需），不依赖系统 Python 与系统 Tcl。
- 版本信息写入 PE 资源（1.0.1.0），程序内 `APP_VERSION = "1.0.1"` 同步。
- 启动时自解压到临时目录；若被安全软件拦截解压，可改用 `--onedir` 备选（文档说明）。

## 验收标准

1. 在 PATH 中移除 Python、清空 `PYTHONHOME`/`PYTHONPATH` 的环境下，自检 EXE 输出 `frozen=True`、完成 16 MB 分片下载、`sha256` 与源一致、临时文件清理干净 → `FROZEN_SELFTEST_PASS`。
2. 主 EXE 从短路径、深层路径、`D:\share` 启动均能显示 GUI（标题含 `v1.0.1`），无控制台窗口。
3. Windows Sandbox 内（无 Python）双击 EXE 可启动 —— 若宿主机沙箱环境无法无人值守验证，改为在沙箱中提供 `.wsb` 配置由用户复核。

## 不做

- 不做代码签名（无证书）、不做安装包/自动更新、不改动下载逻辑本身。
