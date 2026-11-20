"""``fcntl.flock`` 的 Windows 实现（本仓库补丁，见 ../PATCHES.md）。

上游 ``ed2k_client`` 用 ``fcntl.flock`` 给状态目录加进程锁，Windows 没有 ``fcntl``。
这里用 ``msvcrt.locking`` 实现同样语义：非阻塞锁失败时抛 ``BlockingIOError``，
与上游 ``client.py`` 里 ``except BlockingIOError`` 的分支保持一致。

非 Windows 平台直接转调标准库 ``fcntl``，行为不变。
"""

from __future__ import annotations

import os
import sys
from typing import IO, Any

# 与 fcntl 同名常量，供调用方按位组合
LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4


def flock(file: IO[Any], operation: int) -> None:
    """对 ``file`` 加锁，语义与 ``fcntl.flock`` 一致。

    :param file: 已打开的文件对象，取其文件描述符。
    :param operation: ``LOCK_SH`` / ``LOCK_EX`` 与可选 ``LOCK_NB`` 的按位组合。
    :raises BlockingIOError: 已加 ``LOCK_NB`` 且锁被其他进程持有。
    """
    if sys.platform != "win32":
        import fcntl as _fcntl

        _fcntl.flock(file, operation)
        return

    import msvcrt

    fd = file.fileno()
    blocking = not operation & LOCK_NB
    # 锁定文件第一个字节：win32 没有整文件锁，约定用 0 号字节作互斥位
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    position = file.tell()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, mode, 1)
    except OSError as exc:  # 被占用时 msvcrt 抛 OSError(EACCES)
        raise BlockingIOError(exc.errno, exc.strerror or "lock is held") from exc
    finally:
        try:
            os.lseek(fd, position, os.SEEK_SET)
        except OSError:  # 恢复位置失败不影响锁本身
            pass
