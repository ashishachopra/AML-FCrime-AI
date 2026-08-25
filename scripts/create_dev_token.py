"""Create a short-lived HS256 token for local development only."""

import argparse
import os
from datetime import datetime, timedelta, timezone

from jose import jwt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", default="local-analyst")
    parser.add_argument("--roles", default="admin,analyst,ingestor")
    parser.add_argument("--minutes", type=int, default=30)
    args = parser.parse_args()

    secret = os.getenv("JWT_SECRET_KEY", "")
    if len(secret) < 32:
        raise SystemExit("Set JWT_SECRET_KEY to at least 32 random characters first.")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": args.subject,
            "roles": [role.strip() for role in args.roles.split(",") if role.strip()],
            "iss": os.getenv("JWT_ISSUER", "aml-reference"),
            "aud": os.getenv("JWT_AUDIENCE", "aml-api"),
            "iat": now,
            "exp": now + timedelta(minutes=args.minutes),
        },
        secret,
        algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
    )
    print(token)


if __name__ == "__main__":
    main()
