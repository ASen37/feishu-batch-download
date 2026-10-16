"""全局请求节流：把「并发 4 个 worker」压成一条不惹毛服务端的请求流。

## 为什么要有这个文件

2026-10-16 那次运行，58 篇里最后 10 篇**整片**报 HTTP 429。排查过程：

- 单线程连打 40 次 `get_node`（约 3.7 次/秒）—— 不触发；
- 连打 25 次 `export/create`（约 2.1 次/秒）—— 也不触发；
- 但一次运行 84 秒内提交了 58 个导出任务，到第 49 篇开始被拦。

所以它是**累计配额**打满，不是简单的每秒速率。这带来两个后果：

1. 固定的「每秒几次」猜不准 —— 得能自己退让，也能自己收回来；
2. 撞上之后，光靠每个 worker 各自 `sleep` 是没用的：当时 4 个 worker
   同时重试、同时失败，2 秒 + 4 秒敲完 3 次，6 秒内全军覆没。
   必须**全体一起停**，等够一个配额窗口。

## 模型

    acquire()   请求起点排队，保证两次起点至少隔 interval
    penalize()  收到 429 → interval 翻倍 + 设一个所有 worker 都看的冷却截止点
    reward()    连续成功够 recovery 次 → interval 往回缩一点

上行快（翻倍）、下行慢（每次退 20%），是 AIMD 那一套：撞墙就痛、恢复就轻。

## 并发语义

`_slot` 只保护「等到可以发」这一段，且**在等待期间一直持有** —— 这正是
限流生效的关键：4 个 worker 会在这里排成一队，谁也绕不过去。
内部状态另外用 `_lock` 护着，因为 penalize/reward 在等待窗口之外被调用。
"""

import asyncio
import time

# 撞上 429 之后，下一次请求的起点至少往后推 interval 的这么多倍。
# 这个冷却跟「该请求自己的重试 sleep」是叠加的：一个是让**所有** worker
# 停下来，一个是让**这一篇**慢慢再试，两者都要有。
_COOLDOWN_FACTOR = 4.0
_COOLDOWN_CAP = 30.0


class Throttle:
    """全局请求节流器。一个进程一个，所有 worker 共用。"""

    def __init__(self, min_interval=0.25, max_interval=4.0,
                 recovery=20, recover_ratio=0.8):
        self.base = max(0.0, float(min_interval))
        self.max_interval = max(self.base, float(max_interval))
        self.recovery = max(1, int(recovery))
        self.recover_ratio = min(1.0, max(0.1, float(recover_ratio)))

        self.interval = self.base     # 当前实际用的间隔，随 429 涨落
        self._next_at = 0.0           # 下一次允许发请求的单调时刻
        self._streak = 0              # 连续成功次数
        self._slot = asyncio.Lock()   # 串行化「等到可以发」
        self._lock = asyncio.Lock()   # 保护上面几个可变状态

        # 只用于日志和自检
        self.penalties = 0
        self.waits = 0

    async def acquire(self):
        """排队等到可以发下一个请求。所有请求起点都从这里过。"""
        async with self._slot:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                self.waits += 1
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next_at = now + self.interval

    async def penalize(self):
        """收到 429 时调用：拉开间隔，并让所有 worker 一起停一会儿。"""
        async with self._lock:
            self.penalties += 1
            self._streak = 0
            self.interval = min(self.max_interval, max(self.base, self.interval * 2))
            cooldown = min(_COOLDOWN_CAP, self.interval * _COOLDOWN_FACTOR)
            # 用 max 而不是直接赋值：别的 worker 可能刚把 _next_at 推到更晚
            self._next_at = max(self._next_at, time.monotonic() + cooldown)

    async def reward(self):
        """请求成功时调用。攒够连续成功次数就把间隔往回收一点。"""
        async with self._lock:
            self._streak += 1
            if self._streak >= self.recovery and self.interval > self.base:
                self.interval = max(self.base, self.interval * self.recover_ratio)
                self._streak = 0

    def describe(self):
        """给日志用的一句话。间隔回到基准值时不提，省得刷屏。"""
        if abs(self.interval - self.base) < 1e-6 and not self.penalties:
            return f"限流间隔 {self.base:.2f}s"
        return (f"限流间隔 {self.base:.2f}s→{self.interval:.2f}s"
                f"（429 退让 {self.penalties} 次）")
