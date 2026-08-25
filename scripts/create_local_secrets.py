"""Generate untracked local secret files without overwriting existing values."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = PROJECT_ROOT / "secrets"
SECRET_FILES = {
    "rabbitmq_password": 32,
    "jwt_secret": 48,
}


def main() -> None:
    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for filename, byte_count in SECRET_FILES.items():
        path = SECRET_DIR / filename
        if path.exists():
            print(f"kept existing {path.relative_to(PROJECT_ROOT)}")
            continue
        path.write_text(secrets.token_urlsafe(byte_count), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print(f"created {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
