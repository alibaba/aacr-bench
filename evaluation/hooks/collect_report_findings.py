#!/usr/bin/env python3
"""Claude Code PostToolUse hook: persist native ReportFindings calls.

Claude Code's bundled `/code-review` skill submits results through the native
`ReportFindings` tool. This hook receives that tool call and merges its findings
into `<REVIEW_RESULTS_DIR>/<safe_instance_id>.hook.jsonl` (a JSON array), which
the reviewer folds into the per-sample result the same way the MCP partial file
is folded.

Design notes:
- Never prints to stdout: hook stdout can be injected into the model context.
- Always exits 0: a collector failure must not abort the review.
- Deduplicates by (file, line, summary, failure_scenario).
- Uses an exclusive lock plus atomic replace so concurrent subagent calls cannot
  lose findings.
- Writes a sidecar `<safe_instance_id>.hook.meta.json` for every recognized
  ReportFindings event, including empty calls and calls whose items are all
  filtered out.  The sidecar is what distinguishes "called with an empty list"
  from "the hook never ran"; an empty findings file alone is ambiguous because
  the reviewer also resets it to [] before each attempt.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RESULTS_DIR_ENV = "REVIEW_RESULTS_DIR"
INSTANCE_ID_ENV = "REVIEW_INSTANCE_ID"
RUN_ID_ENV = "REVIEW_RUN_ID"
TARGET_TOOL = "ReportFindings"
META_VERSION = 1


def _safe_id(instance_id: str) -> str:
    return instance_id.replace("/", "__")


def _resolve_paths() -> Optional[Tuple[Path, Path]]:
    results_dir = os.environ.get(RESULTS_DIR_ENV)
    instance_id = os.environ.get(INSTANCE_ID_ENV)
    if not results_dir or not instance_id or not instance_id.strip():
        return None
    directory = Path(results_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    safe_id = _safe_id(instance_id.strip())
    return directory / f"{safe_id}.hook.jsonl", directory / f"{safe_id}.hook.meta.json"


def _normalize_line(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _normalize_finding_with_reason(raw: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (normalized_finding, drop_reason).  Exactly one side is non-empty."""
    if not isinstance(raw, dict):
        return None, "not_a_dict"
    file_value = raw.get("file") or raw.get("path") or ""
    summary = raw.get("summary") or ""
    failure = raw.get("failure_scenario") or raw.get("description") or ""
    if not isinstance(file_value, str) or not file_value.strip():
        return None, "missing_file"
    if not isinstance(summary, str) or not summary.strip():
        return None, "missing_summary"
    if file_value.strip().lower() in ("n/a", "none", "-"):
        return None, "placeholder_file"
    cleaned_summary = summary.strip()
    cleaned_failure = failure.strip() if isinstance(failure, str) else ""
    if (
        cleaned_summary.rstrip(".!。！ ").lower()
        in {"no issues found", "no findings", "no bugs found", "nothing to report", "none"}
        and cleaned_failure.lower() in ("", "n/a", "none", "-")
    ):
        return None, "empty_sentinel"
    finding: Dict[str, Any] = {
        "file": file_value.strip(),
        "line": _normalize_line(raw.get("line")),
        "summary": cleaned_summary,
        "failure_scenario": cleaned_failure,
        "source": TARGET_TOOL,
    }
    for extra in ("short_summary", "category", "verdict", "outcome"):
        if raw.get(extra) is not None:
            finding[extra] = raw.get(extra)
    return finding, ""


def _normalize_finding(raw: Any) -> Optional[Dict[str, Any]]:
    return _normalize_finding_with_reason(raw)[0]


def _finding_key(finding: Dict[str, Any]) -> tuple:
    return (
        finding.get("file"),
        finding.get("line"),
        finding.get("summary"),
        finding.get("failure_scenario"),
    )


def _load_existing(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    return data if isinstance(data, list) else []


def _load_meta(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": META_VERSION, "calls": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"version": META_VERSION, "calls": []}
    if not isinstance(data, dict):
        return {"version": META_VERSION, "calls": []}
    calls = data.get("calls")
    if not isinstance(calls, list):
        calls = []
    return {"version": META_VERSION, "calls": calls}


def _atomic_write_json(path: Path, payload: Any) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", delete=False
    )
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def _append_error(results_dir: Path, message: str) -> None:
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "instance_id": os.environ.get(INSTANCE_ID_ENV),
            "run_id": os.environ.get(RUN_ID_ENV),
            "error": message[:500],
        }
        with (results_dir / "collector_errors.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    """Hook entry point: any unexpected failure must still exit 0."""
    try:
        return _run()
    except Exception:
        return 0


def _run() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        event = json.loads(raw or "{}")
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(event, dict) or event.get("tool_name") != TARGET_TOOL:
        return 0

    paths = _resolve_paths()
    if paths is None:
        return 0
    path, meta_path = paths
    results_dir = path.parent

    tool_input = event.get("tool_input")
    findings_raw: Any = None
    payload_source = "tool_input"
    if isinstance(tool_input, dict):
        findings_raw = tool_input.get("findings")
    if not isinstance(findings_raw, list):
        tool_response = event.get("tool_response")
        if isinstance(tool_response, dict):
            findings_raw = tool_response.get("findings")
            payload_source = "tool_response"

    normalized: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    if isinstance(findings_raw, list):
        for index, item in enumerate(findings_raw):
            finding, reason = _normalize_finding_with_reason(item)
            if finding is None:
                dropped.append({"index": index, "reason": reason})
            else:
                normalized.append(finding)
        if not findings_raw:
            call_status = "empty"
        elif normalized:
            call_status = "written"
        else:
            call_status = "all_filtered"
    else:
        # A ReportFindings event without a list is malformed; record it so the
        # reviewer can distinguish it from "the tool was never called".
        call_status = "invalid_payload"
        dropped.append({"index": None, "reason": "findings_not_a_list"})

    call_record: Dict[str, Any] = {
        "tool_use_id": event.get("tool_use_id"),
        "agent_id": event.get("agent_id"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "run_id": os.environ.get(RUN_ID_ENV),
        "payload_source": payload_source,
        "raw_count": len(findings_raw) if isinstance(findings_raw, list) else 0,
        "normalized_count": len(normalized),
        "dropped": dropped,
        "status": call_status,
    }

    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        _append_error(results_dir, "hook lock timeout (2s), finding skipped")
                        return 0
                    time.sleep(0.02)
            if normalized:
                existing = _load_existing(path)
                seen = {_finding_key(item) for item in existing if isinstance(item, dict)}
                for finding in normalized:
                    if _finding_key(finding) in seen:
                        continue
                    record = dict(finding)
                    record["agent_id"] = event.get("agent_id")
                    record["tool_use_id"] = event.get("tool_use_id")
                    record["captured_at"] = call_record["captured_at"]
                    record["run_id"] = call_record["run_id"]
                    existing.append(record)
                    seen.add(_finding_key(finding))
                _atomic_write_json(path, existing)
            meta = _load_meta(meta_path)
            meta["version"] = META_VERSION
            meta["instance_id"] = os.environ.get(INSTANCE_ID_ENV)
            meta["run_id"] = os.environ.get(RUN_ID_ENV)
            meta["updated_at"] = call_record["captured_at"]
            meta["calls"].append(call_record)
            _atomic_write_json(meta_path, meta)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError as error:
        _append_error(results_dir, f"hook write failed: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
