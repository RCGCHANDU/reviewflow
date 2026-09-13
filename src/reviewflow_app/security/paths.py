"""File and path safety for the application layer.

Everything the browser sends is untrusted. A filename is a string an attacker controls, so
it is never used to build a path directly: names are sanitised for display, and the path a
file is actually written to is derived from server-generated ids only.

The second concern here is the *write boundary*. A run may only modify its own workspace, so
that directory is computed from the run id and is the only root the engine is granted.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from reviewflow_app.errors import UnsafePathError

#: Characters that are never acceptable in a stored filename, plus control chars.
_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
MAX_FILENAME_LENGTH = 120


def sanitize_filename(raw: str, *, fallback: str = "source") -> str:
    """Reduce an uploaded filename to something safe to display and store.

    Never returns a path: separators, ``..`` and drive letters are removed rather than
    escaped, so the result cannot traverse anywhere even if it is joined naively.
    """
    name = unicodedata.normalize("NFKC", raw or "").strip()
    # Take the final path component for both separators before doing anything else, so
    # "../../etc/passwd" and "C:\\Windows\\win.ini" both collapse to their last element.
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("_", name).strip(" ._")
    if not name:
        return fallback
    stem, dot, suffix = name.rpartition(".")
    if dot:
        stem = stem[: MAX_FILENAME_LENGTH - len(suffix) - 1] or fallback
        name = f"{stem}.{suffix[:16]}"
    else:
        name = name[:MAX_FILENAME_LENGTH]
    if name.split(".")[0].lower() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name or fallback


def assert_within(path: Path, root: Path) -> Path:
    """Resolve ``path`` and refuse it unless it is inside ``root``.

    Resolution happens before the check, so symlinks and ``..`` segments cannot smuggle a
    path out of the root. Raises rather than returning a flag: a caller that forgets to
    check a boolean is exactly how traversal bugs survive review.
    """
    resolved_root = Path(root).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise UnsafePathError(f"{resolved} is outside the allowed root {resolved_root}")
    return resolved
