# HighSpeedDownloader v1.1.1

首个带 **eD2K** 支持的版本。HTTP 分片下载部分完全兼容 v1.0.1，新增能力全部围绕 `ed2k://` 链接。

## 新增

- **eD2K 下载**：粘贴 `ed2k://|file|名字|大小|哈希|/` 即可下载，自动识别链接类型、自动填文件名。
- **独立引擎**：eD2K 协议由单独进程 `Ed2kEngine.exe` 承担，主程序只通过 stdin/stdout 的 JSON 行与它通信；组件以 GPL-3.0 单独分发，主程序仍是 MIT。
- **服务器 + Kad**：自动登录 eD2K 服务器，可选 Kad 提高找源成功率。
- **上传做种**：默认下载完继续做种，可在设置里关闭。
- **端口可配**：默认 TCP 4662 / UDP 4672，可在设置里改成空闲端口。
- **隔离区**：分块先落 `<保存目录>\.incoming\`，校验 eD2K/AICH 哈希后才移动到最终文件名；中途取消可续传。
- **打开目录**：主界面“保存到”右侧新增按钮，直接打开文件所在文件夹。
- **`--selfcheck`**：`HighSpeedDownloader.exe --selfcheck` 把依赖/组件检查结果写到 `%TEMP%\hsd-selfcheck.json`，方便排错。

## 三种下载方式

| 文件 | 大小 | 适用 |
| --- | --- | --- |
| `HighSpeedDownloader-Setup-1.1.1.exe` | 45 MB | **推荐**。一键安装到 `%LOCALAPPDATA%\Programs\HighSpeedDownloader`，带开始菜单/桌面快捷方式、卸载项，eD2K 组件已内置 |
| `HighSpeedDownloader-1.1.1-portable.zip` | 33 MB | 解压即用，不写注册表；启动最快（无需自解压） |
| `HighSpeedDownloader.exe` | 20 MB | 单个文件随处运行；**不含** eD2K 组件，首次下 eD2K 链接时按需下载约 13 MB |

`Ed2kEngine.zip` 是 eD2K 组件本身，只有想给单文件版手动配组件时才需要。

## 校验

```
HighSpeedDownloader.exe                 21445837  8c9596cde879159dfafaad79ced3742b7b966d6ca668ee56e3206c5dc591abbf
HighSpeedDownloader-1.1.1-portable.zip  34789244  0a01e07e82b9b56105459f704236d0700d9f14f399810a4c1f40c2bdb98e1444
HighSpeedDownloader-Setup-1.1.1.exe     47210355  342bf829eb5d6540f5f26710b198e97b553fed6bdba4ced9fb9eaeed96a59140
Ed2kEngine.zip                          13603544  9a1f8171c6be76bfd21b537b02a36b71d4d90af8bf0835e796d835f364c95aa7
```

## 已知限制

- eD2K 同一时间只跑一个任务；没有搜索界面；上传固定 2 个槽位，暂无限速设置。
- 未做端口映射时是 **Low-ID**，只能连已连接的 High-ID 客户端，速度明显低于 High-ID。
- 未做代码签名，SmartScreen 可能提示“Windows 已保护你的电脑”，点“更多信息 → 仍要运行”。
- BT / 磁力链计划在 v1.1.2 用 libtorrent 实现。

## 验证

- HTTP 回归：`range / subsplit / norange / flaky / resume` 五场景全 PASS。
- 设置项回归：13 项全 PASS（含默认目录、记忆、分片上限、证书开关）。
- eD2K 本地回环：直接调引擎 / 走 Ed2kBridge / 走冻结的 `Ed2kEngine.exe` 三种方式传输 + 校验全 PASS。
- 安装器逻辑：用真实 `payload.zip` 走完解压 → 快捷方式 → 卸载项 → 卸载，PASS。

**完整变更**：<https://github.com/Jayy-yang/high-speed-downloader/compare/v1.0.1...v1.1.1>
