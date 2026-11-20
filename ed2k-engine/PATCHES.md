# 相对上游的改动（ed2k-client 0.1.0）

上游仓库：<https://github.com/akhmialeuski/ed2k-client>，许可证 GPL-3.0-or-later。
`vendor/ed2k_client/` 是 0.1.0 的原样复制，只做了下面两处 Windows 修补，其余代码未动。

## 1. 文件锁：`fcntl` → `msvcrt`

* 文件：`vendor/ed2k_client/client.py:17`
* 上游写法：

  ```python
  import fcntl
  ```

  并在 `client.py:277` 用 `fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)` 防止同一 state 目录被两个进程同时使用。

* 问题：Windows 没有 `fcntl`，导入即崩。
* 改法：

  ```python
  try:
      import fcntl
  except ImportError:  # Windows：用 msvcrt 提供同名接口
      from ed2k_client import win_lock as fcntl
  ```

  `win_lock.flock()` 用 `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` 实现，冲突时抛 `BlockingIOError`，
  与 `LOCK_NB` 的语义一致；`win_lock.LOCK_EX` / `LOCK_NB` 只是占位常量。

## 2. 隔离区目录权限：Windows 上不使用 `mode=0o700`

* 文件：`vendor/ed2k_client/partfile.py:189`
* 上游写法：

  ```python
  directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
  ```

  `DIRECTORY_MODE = 0o700`。

* 问题：在 Windows 上，`mode=0o700` 会让 Python 生成只含所有者 ACE 的 DACL。
  当进程跑在受限令牌下（Windows 沙箱、部分服务账号、某些安全软件）时，
  **同一个进程随后 `os.open()` 自己刚建的目录会得到 `PermissionError: [Errno 13]`**，
  连 `icacls` 都进不去，`.incoming` 于是变成永久无法写入的死目录。
* 改法：

  ```python
  if os.name == 'nt':
      directory.mkdir(parents=True, exist_ok=True)   # 交回默认 ACL
  else:
      directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
  ```

  安全性影响：Windows 上没有「0o700」这种 POSIX 语义，目录仍继承父目录 ACL（用户自己的
  `%APPDATA%` 或下载目录），不额外放宽给其他用户。

## 3. 打包相关（不属于源码改动）

* `vendor/Crypto/` 是 pycryptodome 4.4.0 的 `cp39-abi3-win_amd64` wheel 解包结果，
  只保留运行所需模块（去掉 `SelfTest/`、`test_*.py`、`__pycache__`）。
* `build_engine.ps1` 用 `--paths ed2k-engine\vendor` 让 PyInstaller 能静态找到这两个包。
