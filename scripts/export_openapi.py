"""Export runtime OpenAPI without starting services, brokers, or model clients."""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("feature-engine", "risk-scorer", "gateway", "ingestion", "alert-manager")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--service", choices=SERVICES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.service:
        service = ROOT / "services" / args.service
        sys.path.insert(0, str(service))
        spec = importlib.util.spec_from_file_location("main", service / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["main"] = module
        spec.loader.exec_module(module)
        print(json.dumps(module.app.openapi(), indent=2, sort_keys=True))
        return
    for name in SERVICES:
        # Separate interpreters prevent the services' unqualified module imports
        # (events, main, etc.) from contaminating another service's contract.
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--service", name],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        content = result.stdout.replace("\r\n", "\n")
        schema = json.loads(content)
        # Preserve the historical YAML entry points as standards-compliant path
        # references to the complete generated JSON schema.
        yaml = f"openapi: {schema['openapi']}\ninfo: {json.dumps(schema['info'])}\npaths:\n"
        for path in schema["paths"]:
            pointer = path.replace("~", "~0").replace("/", "~1")
            yaml += f"  {json.dumps(path)}:\n    $ref: {json.dumps(f'./{name}-api.json#/paths/{pointer}')}\n"
        for suffix, value in (("json", content), ("yaml", yaml)):
            target = ROOT / "contracts" / "openapi" / f"{name}-api.{suffix}"
            if args.check:
                if not target.exists() or target.read_text(encoding="utf-8") != value:
                    raise SystemExit(
                        f"Contract drift: run python scripts/export_openapi.py ({name})"
                    )
            else:
                target.write_text(value, encoding="utf-8")
    print("OpenAPI contracts verified" if args.check else "OpenAPI contracts exported")


if __name__ == "__main__":
    main()
