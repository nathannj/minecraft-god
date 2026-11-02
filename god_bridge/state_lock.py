"""Reentrant profile locks shared by controller and MCP processes."""
from __future__ import annotations

import os
import threading
import time
from functools import wraps
from pathlib import Path


def profile_transaction(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class ProfileLock:
    def __init__(self, path: Path, timeout: float = 30) -> None:
        self.path = path
        self.timeout = timeout
        self.local = threading.RLock()
        self.depth = 0
        self.stream = None

    def __enter__(self):
        self.local.acquire()
        try:
            if self.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.stream = self.path.open("a+b")
                if self.path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    deadline = time.monotonic() + self.timeout
                    while True:
                        try:
                            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("profile state lock timed out")
                            time.sleep(0.02)
                else:
                    import fcntl
                    deadline = time.monotonic() + self.timeout
                    while True:
                        try:
                            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("profile state lock timed out")
                            time.sleep(0.02)
            self.depth += 1
            return self
        except Exception:
            if self.stream:
                self.stream.close()
                self.stream = None
            self.local.release()
            raise

    def __exit__(self, *_):
        self.depth -= 1
        try:
            if self.depth == 0:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
                self.stream.close()
                self.stream = None
        finally:
            self.local.release()
