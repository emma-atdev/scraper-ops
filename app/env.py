"""환경변수 로딩. .env 파일을 읽어서 os.environ에 주입한다 (기존 값 우선)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """주어진 .env 파일에서 KEY=VALUE 라인을 읽어 환경변수로 설정한다.

    이미 os.environ에 같은 키가 있으면 덮어쓰지 않는다.
    """
    p = Path(path)
    if not p.exists():
        return

    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
