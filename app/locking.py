"""중복 실행 방지 file lock.

systemd timer가 이전 실행이 끝나기 전에 다음 실행을 시작하거나, 운영자가 수동
실행을 겹쳐 실행하는 경우를 막는다.
"""

from __future__ import annotations

import os
from pathlib import Path


class RunLock:
    """단순 file-based lock. acquire는 atomic O_CREAT|O_EXCL을 쓴다."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode())
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "RunLock":
        if not self.acquire():
            raise RuntimeError(f"Could not acquire lock: {self.path}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
