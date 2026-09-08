"""Container health and updater coordination; no Telegram requests or secrets."""
import json
import os
import sys
import time
from pathlib import Path


def main():
    directory = Path(os.environ.get("DATA_DIR", "data"))
    command = sys.argv[1] if len(sys.argv) > 1 else "health"
    if command == "drain":
        (directory / "drain").touch(mode=0o600)
        return 0
    if command == "resume":
        (directory / "drain").unlink(missing_ok=True)
        return 0
    if command not in ("health", "ready"):
        return 2
    try:
        state = json.loads((directory / "status.json").read_text())
        healthy = time.time() - state["at"] < 180
        return 0 if healthy and (command == "health" or (state["draining"] and not state["busy"])) else 1
    except (OSError, ValueError, KeyError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
