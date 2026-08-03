#!/usr/bin/env python3
"""codex 专用 stdio MCP server：把 code-review finding 增量落盘到 partial 文件。

整体克隆自 `mcp_finding_server.py`（Claude 专用）的架构：env 绑定 instance +
增量落盘 partial.json + atomic write + stdio transport。差异仅在 `report` 工具
的签名与 finding schema：

- Claude 版：`report(file, summary, failure_scenario, line=None)`
  finding 字段：`{file, line, summary, failure_scenario}`
- codex 版：`report(file, summary, description, start_line=None, end_line=None, severity="Minor")`
  finding 字段：`{file, start_line, end_line, severity, summary, description}`

行号从单值升级为闭区间 [start_line, end_line]（与 schema.py 的 ReferenceComment
闭区间语义对齐；单行问题 end_line 回填为 start_line）；新增 `severity` 枚举校验；
`failure_scenario` 改名为 `description`（语义扩展为 failure scenario + suggested fix）。

环境注入（由 pipeline 在启动 codex 时透传给 MCP server 子进程）：
  REVIEW_RESULTS_DIR  — absolute directory where partial files are written
  REVIEW_INSTANCE_ID  — the instance this codex process is reviewing
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

RESULTS_DIR_ENV_VAR = "REVIEW_RESULTS_DIR"
INSTANCE_ID_ENV_VAR = "REVIEW_INSTANCE_ID"

# severity 枚举（非法值不报错，回退到默认 Minor 并提示，保持 server 高可用）
SEVERITIES = {"Critical", "Major", "Minor", "Trivial", "Info"}
DEFAULT_SEVERITY = "Minor"

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


def _normalize_severity(value: Optional[str]) -> str:
    """severity 非法值回退到默认，保持 server 高可用。"""
    if not value or not isinstance(value, str):
        return DEFAULT_SEVERITY
    cleaned = value.strip()
    if cleaned not in SEVERITIES:
        return DEFAULT_SEVERITY
    return cleaned


@mcp.tool()
def report(
    file: str,
    summary: str,
    description: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    severity: str = DEFAULT_SEVERITY,
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
        summary: Concise one-line title of the issue.
        description: Detailed explanation including failure scenario plus the
            suggested fix.
        start_line: 1-based starting line number, or null if not applicable.
        end_line: 1-based ending line number; defaults to start_line for
            single-line issues. null falls back to start_line.
        severity: One of Critical, Major, Minor, Trivial, Info (defaults to Minor).

    Returns:
        A short confirmation including how many findings have been recorded so far.
    """
    if not file or not file.strip():
        return "ERROR: file is required and must be non-empty."

    # 归一化：end_line 缺省时回填为 start_line（与 schema.py 闭区间语义一致）
    normalized_start = start_line
    normalized_end = end_line
    if normalized_start is not None and normalized_end is None:
        normalized_end = normalized_start
    if normalized_end is not None and normalized_start is None:
        normalized_start = normalized_end
    if normalized_start is not None and normalized_end is not None and normalized_start > normalized_end:
        normalized_start, normalized_end = normalized_end, normalized_start

    final_severity = _normalize_severity(severity)

    instance_id = _resolve_instance_id()
    partial_path = _partial_path_for(instance_id)
    findings = _load_existing_findings(partial_path)
    findings.append(
        {
            "file": file.strip(),
            "start_line": normalized_start,
            "end_line": normalized_end,
            "severity": final_severity,
            "summary": summary.strip(),
            "description": description.strip(),
        }
    )
    _atomic_write_findings(partial_path, findings)
    return f"Recorded finding #{len(findings)} -> {partial_path.name}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
