"""仅测试进程使用：旁观真实信号/Popen/wait，不替代被测生命周期实现。"""

from contextlib import contextmanager
from types import SimpleNamespace

import json
import os
import signal
import subprocess
import time

PREFIX = "MAIBOT_SHUTDOWN_TEST "


def emit(event: str, **fields) -> None:
    record = {"event": event, "pid": os.getpid(), "ppid": os.getppid(), "ns": time.monotonic_ns(), **fields}
    # 单次小写入，避免父子进程同时打印时拼接成损坏的 JSON。
    os.write(1, (PREFIX + json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def worker_handler(handler):
    def observed(signum, frame):
        emit("worker_signal_received", signum=int(signum))
        return handler(signum, frame)

    return observed


def worker_exit(code: int) -> None:
    emit("worker_exit_requested", code=code)
    os._exit(code)


@contextmanager
def observe_runner():
    from src.common import process_runner

    original_signal = process_runner.signal
    original_subprocess = process_runner.subprocess
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    if hasattr(signal, "SIGBREAK"):
        previous_handlers[signal.SIGBREAK] = signal.getsignal(signal.SIGBREAK)

    def install(sig, handler):
        if callable(handler):

            def observed(signum, frame):
                emit("runner_signal_received", signum=int(signum))
                return handler(signum, frame)

            return signal.signal(sig, observed)
        return signal.signal(sig, handler)

    class ObservedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            self.exit_reported = False
            super().__init__(*args, **kwargs)
            emit("worker_spawned", child_pid=self.pid)

        def report_exit(self):
            if self.returncode is not None and not self.exit_reported:
                self.exit_reported = True
                emit("runner_worker_reaped", child_pid=self.pid, code=self.returncode)

        def poll(self):
            result = super().poll()
            self.report_exit()
            return result

        def wait(self, *args, **kwargs):
            result = super().wait(*args, **kwargs)
            self.report_exit()
            return result

        def send_signal(self, sig):
            # POSIX Popen.kill() 也经由 send_signal；强杀已有独立事件，不能冒充第二次受控停止。
            if sig != getattr(signal, "SIGKILL", None):
                emit("runner_signal_forwarding", child_pid=self.pid, signum=int(sig))
            return super().send_signal(sig)

        def kill(self):
            emit("runner_force_kill", child_pid=self.pid)
            return super().kill()

    process_runner.signal = SimpleNamespace(**{**vars(signal), "signal": install})
    process_runner.subprocess = SimpleNamespace(**{**vars(subprocess), "Popen": ObservedPopen})
    try:
        yield
    finally:
        process_runner.signal = original_signal
        process_runner.subprocess = original_subprocess
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
