"""后台 stdin 读取：让「终端输入」和「浏览器操作」可以赛跑。

为什么要单独搞一个读取器：
  之前用 `asyncio.to_thread(input, ...)` 问密码。那个线程一旦阻塞在 `read()`
  上就**取消不掉** —— Python 没有标准办法把它从读操作里拽出来。后果是程序卡在
  等终端输入，而主人已经切到浏览器里自己把密码输了、页面早进去了，程序却还在
  傻等，必须回终端随便敲个回车才解得开。

  改成一个守护线程**持续**读 stdin，读到一行就丢进队列。异步侧 poll 队列，随时
  可以放弃等待，而且放弃也不会吞掉后续输入 —— 于是「等终端输入」和「等主人在
  浏览器里操作」可以同时进行，谁先完成算谁的。

⚠️ 一旦启用本读取器，**所有**终端输入都得走它，不要再直接调 `input()`，
   否则两边会抢同一份 stdin（典型症状：某个提示莫名其妙被跳过）。
   `start.py` 那种同步场景用 `ask_sync()`。
"""

import asyncio
import sys
import threading
import time
from collections import deque

# 轮询间隔。等的是人的手速，每秒 10 次唤醒的开销可以忽略。
_POLL = 0.1


class StdinReader:
    """守护线程读 stdin，行进 deque，异步侧轮询取走。

    用 deque 而不是 asyncio.Queue：deque 的 append / popleft 是原子的，
    不需要把队列绑定到某个事件循环上，也就不会在「创建读取器时还没有 loop」
    这件事上翻车。
    """

    def __init__(self):
        self._lines = deque()
        self._eof = False
        self._thread = None

    @property
    def available(self):
        """有没有可用的 stdin（被 pythonw 之类无控制台的进程拉起时为 False）。"""
        return sys.stdin is not None

    def start(self):
        if self._thread is not None or self._eof:
            return self
        if not self.available:
            self._eof = True
            return self
        self._thread = threading.Thread(target=self._run, name="stdin-reader", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        try:
            for line in sys.stdin:
                self._lines.append(line.rstrip("\r\n"))
        except Exception:
            pass
        self._eof = True

    # ---------------- 取行 ----------------

    def _try_pop(self, deadline):
        """非阻塞取一行；没货返回 _EMPTY，EOF 返回 None，超时抛 TimeoutError。"""
        if self._lines:
            return self._lines.popleft()
        if self._eof:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            raise asyncio.TimeoutError
        return _EMPTY

    async def readline(self, timeout=None):
        """异步等一行。

        返回 str（一行内容，可能就是 ""）；
        返回 None 表示 stdin 已关闭（EOF，非交互环境）；
        超时抛 asyncio.TimeoutError。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            got = self._try_pop(deadline)
            if got is not _EMPTY:
                return got
            await asyncio.sleep(_POLL)

    def readline_sync(self, timeout=None):
        """同步版，语义与 readline 相同，给 start.py 这类场景用。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            got = self._try_pop(deadline)
            if got is not _EMPTY:
                return got
            time.sleep(_POLL)


class _Empty:
    """哨兵：区分「暂时没货」和「取到空字符串/EOF」。"""


_EMPTY = _Empty()

_reader = None
_lock = threading.Lock()


def get_reader():
    """全局单例。所有终端输入都从这里走。"""
    global _reader
    with _lock:
        if _reader is None:
            _reader = StdinReader().start()
        return _reader


def ask_sync(prompt=""):
    """同步问一行，返回字符串；EOF 返回 ""。"""
    print(prompt, end="", flush=True)
    line = get_reader().readline_sync()
    return "" if line is None else line
