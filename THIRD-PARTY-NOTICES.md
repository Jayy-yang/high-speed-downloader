# 第三方组件与许可

HighSpeedDownloader 本身以 MIT 许可发布。发行包中同时包含以下第三方组件：

| 组件 | 版本 | 许可 | 用途 | 位置 |
| --- | --- | --- | --- | --- |
| aiohttp | 3.14.x | Apache-2.0 | 异步分片下载 | 内嵌（PyInstaller） |
| attrs / aiohappyeyeballs / aiosignal / frozenlist / multidict / propcache / yarl | 随 aiohttp | Apache-2.0 / MIT | aiohttp 依赖 | 内嵌 |
| requests | 2.34.x | Apache-2.0 | 同步兜底、证书探测 | 内嵌 |
| urllib3 / certifi / charset_normalizer / idna | 随 requests | MIT / MPL-2.0 / Apache-2.0 | requests 依赖 | 内嵌 |
| ed2k-client | 0.1.0 | **GPL-3.0-or-later** | eD2K / Kad 协议 | `ed2k\` 目录，独立进程 |
| pycryptodome | 4.4.0 | 公有领域 / BSD-2-Clause | MD4 / SHA1 哈希 | `ed2k\` 目录 |
| Python | 3.14 | PSF-2.0 | 运行时 | 内嵌 |

## GPL-3.0 组件边界

* `Ed2kEngine.exe` 是**独立可执行文件**，主程序通过 stdin/stdout 的 JSON 行与它通信，
  不链接、不共享进程内存、不互相 import。
* 其完整源码就在 `ed2k-engine/`（含 `vendor/ed2k_client/` 与 `vendor/Crypto/`），
  许可见 `LICENSE-GPL-3.0.txt`，改动见 `ed2k-engine/PATCHES.md`。
* 不使用 eD2K 时可以直接删除 `ed2k\` 目录，主程序的其它功能不受影响。

## 源码再分发提醒

如果你把 `ed2k-engine/` 的代码并入主程序源码树后再整体分发，合并后的作品需整体遵循 GPL-3.0。
保持「独立进程 + 独立目录 + 独立发布物」的边界，才能维持主程序的 MIT 许可。
