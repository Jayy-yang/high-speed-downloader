# eD2K 组件说明（GPL-3.0）

本目录是 **HighSpeedDownloader 的可选 eD2K 组件**，以独立进程方式运行，单独以 GPL-3.0-or-later 分发。
主程序（MIT）只通过 stdin/stdout 的按行 JSON 与它通信，两者不共享进程、不互相 import，
因此在法律上按「独立程序聚合」处理；如果你把本目录的代码并入主程序源码树再分发，整个程序将受 GPL-3.0 约束。

## 目录结构

```
ed2k-engine/
├─ engine.py                  # 引擎入口：JSON 行协议、下载/做种/共享调度
├─ win_lock.py -> vendor/ed2k_client/win_lock.py   # Windows 上的文件锁实现
├─ vendor/
│  ├─ ed2k_client/            # 上游 ed2k-client 0.1.0（GPL-3.0-or-later），带两处 Windows 补丁
│  └─ Crypto/                 # pycryptodome 4.4.0（公有领域 / BSD-2-Clause），用于 MD4/SHA1
├─ PATCHES.md                 # 相对上游的改动清单
├─ requirements.txt           # 版本来源
└─ dist/Ed2kEngine/           # 打包产物（Ed2kEngine.exe + 依赖）
```

## 协议（主程序 → 引擎）

请求与响应都是**一行一个 JSON**，UTF-8 编码。

| 命令 | 说明 |
| --- | --- |
| `{"cmd":"ping","id":1}` | 探活，回 `{"id":1,"ok":true}` |
| `{"cmd":"start", ...}` | 开始下载，字段见下 |
| `{"cmd":"cancel"}` | 取消当前下载（隔离区进度保留） |
| `{"cmd":"status"}` | 主动要一次 `net` 状态 |
| `{"cmd":"share","path":"..."}` | 把本地文件加入共享（做种） |
| `{"cmd":"hash","path":"..."}` | 计算 eD2K 哈希并回一条 `ed2k://` 链接 |
| `{"cmd":"shutdown"}` | 优雅退出 |

`start` 可用字段：

```json
{
  "cmd": "start",
  "link": "ed2k://|file|name.bin|1048576|0123456789ABCDEF0123456789ABCDEF|/",
  "out_dir": "D:\\Downloads",
  "state_dir": "%APPDATA%\\HighSpeedDownloader\\ed2k-state",
  "tcp_port": 4662,
  "udp_port": 4672,
  "use_kad": true,
  "seed": true,
  "max_peers": 16,
  "nickname": "HighSpeedDownloader",
  "offline": false,
  "sources": [{"host": "127.0.0.1", "port": 4701}]
}
```

* `offline` 为真时：不登录服务器、不拉 `server.met`/`nodes.dat`/IP 过滤表、允许私有地址 peer，只用于本地联调。
* `sources` 仅在联调/内网直连时使用，正常下载留空。

## 事件（引擎 → 主程序）

| 事件 | 载荷要点 |
| --- | --- |
| `ready` | `version` / `python` / `platform` |
| `task` | `name` / `size` / `hash` / `has_aich` |
| `log` | `message`（给用户看的中文进度提示） |
| `progress` | `name` / `size` / `verified` / `peers` / `sources` |
| `net` | `servers` / `low_id` / `kad` / `port` / `shared` |
| `seed` | `files` / `path` |
| `done` | `path` / `size` |
| `cancelled` | — |
| `error` | `message` / `fatal` / `detail` |
| `bye` | — |

## 直接手动运行

```powershell
# 看版本
.\Ed2kEngine.exe --version

# 手动喂一条指令（PowerShell 里一行一条 JSON）
'{"cmd":"ping","id":1}' | .\Ed2kEngine.exe
```

引擎把日志写到 stderr，主程序不显示；需要排错时单独在控制台运行即可看到。

## 已知限制

* 只支持单个 eD2K 下载任务，不支持排队；需要并发请开多个实例（各自不同端口与 state 目录）。
* 上传遵循协议默认的 2 个上传槽位，没有做限速/配额界面。
* 处于 Low-ID（未做端口映射）时下载与上传都只能连已连接的 High-ID 客户端，速度明显低于 High-ID。
