"""Validate the files crossing the review-container boundary."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

MAX_RESULT_BYTES = 64 * 1024 * 1024


def read_json(path: Path) -> dict:
    """Read one regular, unshared file without following directory/file links."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory = os.open(path.parent, flags | getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"result must be a regular, unshared file: {path.name}")
        if info.st_size > MAX_RESULT_BYTES:
            raise ValueError(f"result exceeds {MAX_RESULT_BYTES} bytes: {path.name}")
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"result must contain a JSON object: {path.name}")
    return payload


def validate_identity(payload: dict, instance: dict) -> None:
    for key in ("instance_id", "repo", "base_commit", "head_commit"):
        if payload.get(key) != instance[key]:
            raise ValueError(f"result identity mismatch: {key}")


def validate_result(payload: dict, instance: dict, reviewer: str, preview: bool = False) -> None:
    validate_identity(payload, instance)
    expected_reviewer = {"ocr": "ocr", "claude": "claude-code/code-review", "codex": "codex"}[reviewer]
    if payload.get("reviewer") != expected_reviewer:
        raise ValueError("result reviewer mismatch")
    if payload.get(f"{reviewer}_exit_code") != 0:
        raise ValueError("reviewer did not exit successfully")
    if preview:
        return
    if reviewer == "ocr":
        review = payload.get("review")
        comments = review.get("comments") if isinstance(review, dict) else None
        text_keys = ("content",)
    else:
        comments = payload.get("review_output")
        text_keys = ("summary", "failure_scenario" if reviewer == "claude" else "description")
    if not isinstance(comments, list):
        raise ValueError("review output must contain a structured comments array")
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("each finding must be a JSON object")
        if not isinstance(comment.get("path" if reviewer == "ocr" else "file", ""), str):
            raise ValueError("finding file path must be a string")
        if any(comment.get(key) is not None and not isinstance(comment.get(key), str) for key in text_keys):
            raise ValueError("finding text must be a string")
        for key in (("line",) if reviewer == "claude" else ("start_line", "end_line")):
            line = comment.get(key)
            if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
                raise ValueError("finding line must be an integer or null")


def redact_payload(payload: Any, values) -> Any:
    if isinstance(payload, str):
        for value in values:
            if value:
                payload = payload.replace(value, "<redacted>")
        return payload
    if isinstance(payload, list):
        return [redact_payload(value, values) for value in payload]
    if isinstance(payload, dict):
        return {key: redact_payload(value, values) for key, value in payload.items()}
    return payload

def write_result(directory: Path, filename: str, payload: Any, overwrite: bool = False, mode: int = 0o600) -> None:
    """Write a regular result, optionally replacing contents without following links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        target = os.open(filename, os.O_WRONLY | os.O_CREAT | (0 if overwrite else os.O_EXCL) | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(target, "w", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("destination must be a regular, unshared file")
        os.fchmod(handle.fileno(), mode)
        if overwrite:
            os.ftruncate(handle.fileno(), 0)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
