"""Safe configuration check; no OpenAI request is made."""

import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent / "services" / "alert-manager"
sys.path.insert(0, str(SERVICE_DIR))

from alerts import AlertManager  # noqa: E402


def main() -> None:
    manager = AlertManager()
    if manager.openai_client:
        print(f"OpenAI narrative drafting is configured with model {manager.openai_model}.")
    else:
        enabled = os.getenv("SAR_GENERATION_ENABLED", "true").lower() == "true"
        mode = "template fallback" if enabled else "disabled"
        print(f"OpenAI client is not configured; narrative mode: {mode}.")
    print("No API request was made and no credential value was displayed.")


if __name__ == "__main__":
    main()
