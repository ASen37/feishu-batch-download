"""日志：终端 + 当次归档 logs/run-<时间戳>.log + run.log（最新一次）。

旧版每次启动都会删掉 run.log，历史全丢；现在改为归档，便于回溯对比。
"""

import time
from pathlib import Path


class Logger:
    def __init__(self, base_dir, console=True):
        self.base_dir = Path(base_dir)
        self.console = console
        self.log_dir = self.base_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.archive_path = self.log_dir / f"run-{stamp}.log"
        self.latest_path = self.base_dir / "run.log"
        self._handles = [
            open(self.archive_path, "w", encoding="utf-8"),
            open(self.latest_path, "w", encoding="utf-8"),
        ]

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if self.console:
            print(line, flush=True)
        for fh in self._handles:
            try:
                fh.write(line + "\n")
                fh.flush()
            except Exception:
                pass

    def close(self):
        for fh in self._handles:
            try:
                fh.close()
            except Exception:
                pass
