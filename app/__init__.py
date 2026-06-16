"""dn-notification package.

`__version__` is read from the top-level VERSION file at import time. The
VERSION file is the single source of truth and is written by
`scripts/write-version.sh`, which is invoked by semantic-release on every
release. Do not edit it by hand.

The `# Version:` header in this file is updated alongside VERSION by the
same script; it serves as a fallback for editable installs and for tooling
that parses this file directly.
"""
# Version: 1.3.0
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _load_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "0.0.0+unknown"


__version__ = _load_version()
