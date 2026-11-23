"""Select non-secret reviewer versions without loading model credentials."""
from __future__ import annotations
import os
import re
import shlex
from pathlib import Path
from typing import Mapping, Optional

VERSION_KEYS = ("OCR_VERSION", "CLAUDE_CODE_VERSION", "CODEX_VERSION")
VERSION_LABELS = {
    "OCR_VERSION": "org.aacr.ocr.version",
    "CLAUDE_CODE_VERSION": "org.aacr.claude.version",
    "CODEX_VERSION": "org.aacr.codex.version",
}
REVIEWER_VERSION_KEYS = {"ocr": "OCR_VERSION", "claude": "CLAUDE_CODE_VERSION", "codex": "CODEX_VERSION"}


def resolve_versions(env_file: Optional[Path], environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    environment = os.environ if environ is None else environ
    values: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        for raw in Path(env_file).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in VERSION_KEYS:
                continue
            parts = shlex.split(value, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError(f"{key} must contain one version or tag")
            values[key] = parts[0] if parts else ""
    result = {}
    for key in VERSION_KEYS:
        value = (environment.get(key, "") or "").strip() or values.get(key, "").strip() or "latest"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
            raise ValueError(f"{key} must be a version or npm tag; interpolation is not supported")
        result[key] = value
    return result
