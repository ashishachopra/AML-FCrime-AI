"""Create a short-lived HS256 token for local development only."""

import argparse
import os
from datetime import datetime, timedelta, timezone

from jose import jwt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", default="local-analyst")
    parser.add_argument("--roles", default="analyst,ingestor")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument(
        "--principal-type", choices=["human", "service", "agent"], default="service"
    )
    parser.add_argument(
        "--simulate-mfa",
        action="store_true",
        help="Local testing only; never substitutes for an identity provider's MFA",
    )
    args = parser.parse_args()
    if not 0 < args.minutes <= 60 or args.principal_type == "agent" and args.minutes > 15:
        parser.error("tokens must last 1-60 minutes; agent tokens at most 15 minutes")
    if args.simulate_mfa and args.principal_type != "human":
        parser.error("simulated MFA is only for explicitly human local test tokens")

    secret = os.getenv("JWT_SECRET_KEY", "")
    if len(secret) < 32:
        raise SystemExit("Set JWT_SECRET_KEY to at least 32 random characters first.")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": args.subject,
            "roles": [role.strip() for role in args.roles.split(",") if role.strip()],
            "principal_type": args.principal_type,
            "amr": ["mfa"] if args.simulate_mfa else [],
            "auth_time": int(now.timestamp()) if args.simulate_mfa else None,
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
