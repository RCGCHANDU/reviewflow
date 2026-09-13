"""Print the SHAPE of credential files: keys, types, lengths, masked values.

Never prints a full secret. Run: python tools/inspect_credentials.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path.home()

CANDIDATES = [
    HOME / ".commandcode" / "auth.json",
    HOME / ".commandcode" / "config.json",
    HOME / ".codex" / "auth.json",
    HOME / "AppData" / "Local" / "hermes" / "auth.json",
]


def mask(value: object, depth: int = 0, prefix: str = "") -> None:
    if depth > 4:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(item, (dict, list)):
                print(f"  {path}: {type(item).__name__} (len {len(item)})")
                mask(item, depth + 1, path)
            elif isinstance(item, str):
                if len(item) > 16:
                    print(f"  {path}: str len={len(item)} head={item[:6]!r} tail={item[-4:]!r}")
                else:
                    print(f"  {path}: str = {item!r}")
            else:
                print(f"  {path}: {type(item).__name__} = {item!r}")
    elif isinstance(value, list):
        print(f"  {prefix}: list of {len(value)}")
        if value:
            mask(value[0], depth + 1, f"{prefix}[0]")


def main() -> None:
    for path in CANDIDATES:
        print(f"\n=== {path} ===")
        if not path.exists():
            print("  MISSING")
            continue
        print(f"  size={path.stat().st_size}B mtime={int(path.stat().st_mtime)}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  unreadable as JSON: {exc}")
            continue
        mask(data)
    print("\n=== env (names only) ===")
    for key in sorted(os.environ):
        if any(part in key.upper() for part in ("TOKEN", "KEY", "SECRET", "OAUTH", "COMMANDCODE", "OPENAI")):
            print(f"  {key} (len {len(os.environ[key])})")


if __name__ == "__main__":
    main()
