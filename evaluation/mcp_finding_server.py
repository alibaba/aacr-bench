#!/usr/bin/env python3
"""Stdio MCP server that lets Claude Code stream code-review findings to disk.

Problem this solves:
  When driving `/code-review` via `claude -p`, the final stdout format is
  unreliable — it may be clean JSON, line-delimited stream-json, or free-text
  prose. Parsing that after the fact is brittle.

Solution:
  Expose a single MCP tool, `report`, and instruct Claude (via an
  appended system prompt in run_review_claude.py) to call it exactly once for
  every finding it discovers. Each call atomically appends one structured
  finding to a per-instance partial file:

      <REVIEW_RESULTS_DIR>/<safe_instance_id>.partial.json

  where safe_instance_id = instance_id.replace("/", "__").

  CRITICAL design choice — the instance_id is NOT a tool parameter. One claude
  process reviews exactly one instance, so the server learns the instance_id
  from the REVIEW_INSTANCE_ID environment variable at startup. Letting the model
  pass instance_id caused it to hallucinate values (or copy the docstring
  example), scattering findings into wrong/orphan files. Binding it server-side
  removes that entire failure class: the model literally cannot misroute a
  finding.

Transport: stdio (the server is spawned by Claude Code itself via --mcp-config).
Environment injected by the main pipeline when launching claude:
  REVIEW_RESULTS_DIR  — absolute directory where partial files are written
  REVIEW_INSTANCE_ID  — the instance this claude process is reviewing
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

RESULTS_DIR_ENV_VAR = "REVIEW_RESULTS_DIR"
INSTANCE_ID_ENV_VAR = "REVIEW_INSTANCE_ID"

mcp = FastMCP("findings")


def _resolve_results_dir() -> Path:
    """Resolve the directory where partial finding files are written."""
    configured = os.environ.get(RESULTS_DIR_ENV_VAR)
    if not configured:
        raise RuntimeError(
            f"{RESULTS_DIR_ENV_VAR} is not set; the MCP server cannot locate the results directory."
        )
    results_dir = Path(configured)
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir


def _resolve_instance_id() -> str:
    """Resolve the instance this process reviews, from the environment only."""
    instance_id = os.environ.get(INSTANCE_ID_ENV_VAR)
    if not instance_id or not instance_id.strip():
        raise RuntimeError(
            f"{INSTANCE_ID_ENV_VAR} is not set; the MCP server does not know which instance to record findings for."
        )
    return instance_id.strip()


def _partial_path_for(instance_id: str) -> Path:
    """Map an instance_id to its partial findings file path."""
    safe_id = instance_id.replace("/", "__")
    return _resolve_results_dir() / f"{safe_id}.partial.json"


def _load_existing_findings(partial_path: Path) -> List[Dict[str, Any]]:
    """Load the findings already written for this instance, tolerating absence."""
    if not partial_path.exists():
        return []
    try:
        with partial_path.open("r", encoding="utf-8") as handle:
            existing = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return []
    return existing if isinstance(existing, list) else []


def _atomic_write_findings(partial_path: Path, findings: List[Dict[str, Any]]) -> None:
    """Write the findings list, replacing the file atomically to avoid corruption."""
    temp_path = partial_path.with_suffix(partial_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(findings, handle, ensure_ascii=False, indent=2)
    temp_path.replace(partial_path)


@mcp.tool()
def report(
    file: str,
    summary: str,
    failure_scenario: str,
    line: Optional[int] = None,
) -> str:
    """Append exactly one code-review finding for the current review.

    Call this once per finding the moment you confirm it, instead of collecting
    everything for a single final message. This keeps the structured output
    independent of the assistant's final text format.

    You do NOT specify which instance the finding belongs to — the server is
    already bound to the single instance under review, so just describe the
    finding itself.

    Args:
        file: Path to the offending file, relative to the repository root.
        summary: Concise description of the issue.
        failure_scenario: Concrete scenario in which this issue causes a failure,
            plus the suggested fix.
        line: 1-based line number of the issue, or omitted/null if not applicable.

    Returns:
        A short confirmation including how many findings have been recorded so far.
    """
    if not file or not file.strip():
        return "ERROR: file is required and must be non-empty."

    instance_id = _resolve_instance_id()
    partial_path = _partial_path_for(instance_id)
    findings = _load_existing_findings(partial_path)
    findings.append(
        {
            "file": file.strip(),
            "line": line,
            "summary": summary.strip(),
            "failure_scenario": failure_scenario.strip(),
        }
    )
    _atomic_write_findings(partial_path, findings)
    return f"Recorded finding #{len(findings)} -> {partial_path.name}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
