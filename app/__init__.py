"""dn-notification package.

`__version__` is resolved at import time, in this order:

  1. The ``APP_VERSION`` environment variable, which the production
     Docker image is built with (set from the git tag the
     semantic-release pipeline just cut). This is the only source of
     truth in production: the version is the tag, the tag is the
     version, and the image is rebuilt per release.
  2. ``git describe --tags --dirty``, for editable installs and local
     development where no image build has set ``APP_VERSION``. The
     git checkout is the same repo semantic-release analyzes, so the
     describe output is the same string semantic-release would tag.
  3. ``"0.0.0+unknown"`` as a final fallback for tarball installs and
     CI sandboxes that have no APP_VERSION and no git history.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

__all__ = ["__version__"]


def _from_env() -> str | None:
    value = os.environ.get("APP_VERSION", "").strip()
    return value or None


def _from_git_describe() -> str | None:
    # Only available in editable/dev installs, not in the production
    # image (shutil.which gates the subprocess call). Errors and
    # non-zero exits are intentionally swallowed: a missing or
    # shallow checkout is expected in many sandboxes and is not a
    # version-loading failure — we just fall through to the next
    # source.
    if shutil.which("git") is None:
        return None
    try:
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    # `git describe` returns "v1.4.0" (with the leading v). The rest of
    # the project uses plain semver "1.4.0", so strip the prefix.
    if value.startswith("v") and len(value) > 1 and value[1].isdigit():
        value = value[1:]
    return value


def _resolve_version() -> str:
    for source in (_from_env, _from_git_describe):
        try:
            value = source()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value
    return "0.0.0+unknown"


__version__ = _resolve_version()
