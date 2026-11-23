"""Claude Code reviewer with hook-first finding collection.

The built-in `/code-review <base>...<head>` explores the repository and submits
structured findings through the native `ReportFindings` tool.  A PostToolUse
hook persists those calls as the primary channel; stream-json tool calls, the
MCP findings server, and stdout parsing remain fallbacks when the hook is
unavailable.  The final result keeps all channel outputs for audit while
`review_output` remains the scoring contract.

LLM 配置通过环境变量提供：
  ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from repo_utils import diff_stat, log, prepare_repo, run_command, clean_worktree
from schema import ReviewInstance

# 显式表示"无 finding"的短语，命中则直接判为空评审
_EMPTY_REVIEW_PATTERNS = [
    r"\bno\s+findings?\b",
    r"\bno\s+issues?\s+found\b",
    r"\bnothing\s+to\s+report\b",
    r"\bno\s+comments?\b",
    r"^\s*\(?\s*none\s*\)?\s*$",
]


def ensure_claude_installed() -> None:
    result = run_command(["claude", "--version"])
    if result.returncode != 0:
        raise SystemExit(
            "claude CLI not found. Install Claude Code, e.g.: npm i -g @anthropic-ai/claude-code"
        )
    log(f"claude available: {result.stdout.strip()}")


def resolve_claude_env() -> Dict[str, str]:
    """从环境变量解析 Claude Code 配置，缺失则早报错。

    同时设置 CLAUDE_CODE_MAX_RETRIES：优先读环境变量，未设置则使用 config 默认值。
    """
    missing = config.missing_env_vars(config.CLAUDE_REQUIRED_ENV_VARS)
    if missing:
        raise SystemExit(
            f"Missing env vars: {', '.join(missing)}. "
            "Run: set -a && source evaluation/.env && set +a"
        )
    max_retries = os.environ.get(
        config.CLAUDE_MAX_RETRIES_VAR,
        str(config.CLAUDE_MAX_RETRIES_DEFAULT),
    )
    log(f"CLAUDE_CODE_MAX_RETRIES = {max_retries}")
    return {
        config.CLAUDE_URL_VAR: os.environ[config.CLAUDE_URL_VAR],
        config.CLAUDE_TOKEN_VAR: os.environ[config.CLAUDE_TOKEN_VAR],
        config.CLAUDE_MODEL_VAR: os.environ[config.CLAUDE_MODEL_VAR],
        config.CLAUDE_MAX_RETRIES_VAR: max_retries,
    }


def build_mcp_config(results_dir: Path, instance_id: str) -> Dict[str, Any]:
    """构造内联 --mcp-config，spawn findings server。

    用绝对路径：Claude Code 启动 MCP server 时 cwd 指向被评审仓库，相对路径会
    解析到仓库目录，导致 partial 文件写错位置、save_result 永远找不到而静默兜底。
    """
    absolute_results_dir = Path(results_dir).resolve()
    return {
        "mcpServers": {
            config.MCP_SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(config.MCP_SERVER_SCRIPT.resolve())],
                "env": {
                    config.MCP_RESULTS_DIR_ENV_VAR: str(absolute_results_dir),
                    config.MCP_INSTANCE_ID_ENV_VAR: instance_id,
                },
            }
        }
    }


def build_findings_system_prompt() -> str:
    """Append a minimal reporting instruction without changing the review flow.

    The native ReportFindings tool is the primary contract.  MCP is mentioned
    only as a failure fallback; field names are enforced by the tool/MCP schema
    and are intentionally not repeated here.
    """
    return (
        "When the review is complete, report verified findings with the native "
        "`ReportFindings` tool exactly once (empty array if none). "
        "If `ReportFindings` is unavailable or its call fails, use the MCP tool "
        f"`{config.MCP_REPORT_TOOL}` once per finding. "
        "Do not report the same finding through both tools. "
        "Reporting must not change the review scope or effort."
    )


def prepare_instance_result_files(results_dir: Path, instance_id: str, run_id: str) -> None:
    """Reset per-instance collection files before a new review attempt.

    Custom harnesses sometimes call `run_claude_review` + `save_result` directly
    instead of `review_instance`.  They must call this helper first; otherwise a
    `.hook.jsonl` left by an aborted attempt is appended to by the new run and
    the reviewer silently mixes findings from two attempts.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    partial = config.partial_path(results_dir, instance_id).resolve()
    if partial.exists():
        try:
            partial.unlink()
        except OSError as error:
            log(f"Warning: could not remove partial file {partial.name}: {error}")
    hook_path = config.hook_findings_path(results_dir, instance_id).resolve()
    meta_path = config.hook_meta_path(results_dir, instance_id).resolve()
    try:
        hook_path.write_text("[]", encoding="utf-8")
    except OSError as error:
        log(f"Warning: could not reset hook findings {hook_path.name}: {error}")
    reset_meta = {
        "version": 1,
        "instance_id": instance_id,
        "run_id": run_id,
        "reset": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "calls": [],
    }
    try:
        meta_path.write_text(json.dumps(reset_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        log(f"Warning: could not reset hook meta {meta_path.name}: {error}")


def build_hook_settings(
    results_dir: Path, instance_id: str, run_id: Optional[str] = None
) -> Dict[str, Any]:
    """构造包含 StopFailure 与 ReportFindings 两个 hook 的 settings。

    StopFailure：Claude Code 内置 API retry 耗尽后触发，写入 retry_exhausted.jsonl。

    PostToolUse(ReportFindings)：`/code-review` 通过原生 ReportFindings 工具提交结果；
    hook 把 tool_input.findings 落盘为 <safe_id>.hook.jsonl，作为优先于 MCP 与 stdout
    的主通道。

    注意：Claude Code 2.1.x 不保证把 hook 级 `env` 传给子进程，因此 REVIEW_* 环境
    变量直接写在命令里；保留 env 字段用于向前兼容。
    """
    run_id = run_id or f"{config.safe_id(instance_id)}-{uuid.uuid4().hex[:12]}"
    absolute_results_dir = Path(results_dir).resolve()
    hook_script = str(config.STOP_FAILURE_HOOK_SCRIPT.resolve())
    stop_failure_command = (
        f"env {config.MCP_RESULTS_DIR_ENV_VAR}={shlex.quote(str(absolute_results_dir))} "
        f"{config.MCP_INSTANCE_ID_ENV_VAR}={shlex.quote(instance_id)} "
        f"{shlex.quote(sys.executable)} {shlex.quote(hook_script)}"
    )
    collect_script = str(config.REPORT_FINDINGS_HOOK_SCRIPT.resolve())
    collector_command = (
        f"env {config.MCP_RESULTS_DIR_ENV_VAR}={shlex.quote(str(absolute_results_dir))} "
        f"{config.MCP_INSTANCE_ID_ENV_VAR}={shlex.quote(instance_id)} "
        f"{config.REVIEW_RUN_ID_ENV_VAR}={shlex.quote(run_id)} "
        f"{shlex.quote(sys.executable)} {shlex.quote(collect_script)}"
    )
    return {
        "hooks": {
            "StopFailure": [
                {
                    "matcher": "rate_limit|overloaded|server_error|unknown",
                    "hooks": [
                        {
                            "type": "command",
                            "command": stop_failure_command,
                            "timeout": 10000,
                            "env": {
                                config.MCP_RESULTS_DIR_ENV_VAR: str(absolute_results_dir),
                                config.MCP_INSTANCE_ID_ENV_VAR: instance_id,
                            },
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "ReportFindings",
                    "hooks": [
                        {
                            "type": "command",
                            "command": collector_command,
                            # Claude Code hook timeout unit is seconds; collector is sub-second.
                            "timeout": 10,
                        }
                    ],
                }
            ],
        }
    }


def _write_temp_settings(settings: Dict[str, Any], prefix: str = "claude_settings_") -> str:
    """将 settings dict 写入临时 JSON 文件，返回文件路径。

    临时文件不自动删除——由调用方在进程结束后清理。
    """
    temp_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=prefix, delete=False,
    )
    with temp_file:
        json.dump(settings, temp_file, ensure_ascii=False)
    return temp_file.name


def run_claude_review(
    repo_path: Path,
    base_commit: str,
    head_commit: str,
    instance_id: str,
    results_dir: Path,
    claude_env: Dict[str, str],
    timeout_minutes: int,
    run_id: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """运行官方 /code-review，评审 base...head 范围。"""
    review_target = f"{base_commit}...{head_commit}"
    run_id = run_id or f"{config.safe_id(instance_id)}-{uuid.uuid4().hex[:12]}"
    slash_command = f"/code-review {review_target} "

    mcp_config = build_mcp_config(results_dir, instance_id)
    findings_prompt = build_findings_system_prompt()
    hook_settings = build_hook_settings(results_dir, instance_id, run_id)
    settings_path = _write_temp_settings(hook_settings)

    command = [
        "claude",
        "-p",
        slash_command,
        "--add-dir", str(repo_path),
        "--permission-mode", "acceptEdits",
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", json.dumps(mcp_config),
        "--allowed-tools", f"{config.MCP_REPORT_TOOL},ReportFindings",
        "--append-system-prompt", findings_prompt,
        "--settings", settings_path,
    ]

    process_timeout = timeout_minutes * 60 + 60
    environment = os.environ.copy()
    environment.update(claude_env)
    # Enable the native ReportFindings contract.  Docker can extend the
    # background-subagent wait separately through its child environment.
    environment["CLAUDE_CODE_REPORT_FINDINGS"] = "1"
    environment[config.REVIEW_RUN_ID_ENV_VAR] = run_id
    log(f"$ claude -p '/code-review {review_target}'  (cwd={repo_path}, run_id={run_id})")
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_path),
            timeout=process_timeout,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as timeout_error:
        log(f"TIMEOUT: claude process exceeded {process_timeout}s — treating as failed")
        stdout_value = timeout_error.stdout or ""
        if isinstance(stdout_value, bytes):
            stdout_value = stdout_value.decode("utf-8", errors="replace")
        completed = subprocess.CompletedProcess(
            args=command,
            returncode=-1,
            stdout=stdout_value,
            stderr=f"Process timed out after {process_timeout} seconds",
        )
    finally:
        try:
            os.unlink(settings_path)
        except OSError:
            pass
    # Attach the run id so save_result can ignore stale hook records even when
    # a custom harness bypasses review_instance()'s reset.
    completed.review_run_id = run_id
    return completed


def looks_like_empty_review(review_text: str) -> bool:
    """文本是否明确表示无 finding（命中则直接判为空评审）。"""
    if not review_text or not review_text.strip():
        return True
    lowered = review_text.lower()
    return any(re.search(pattern, lowered, re.MULTILINE) for pattern in _EMPTY_REVIEW_PATTERNS)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _normalize_claude_finding(raw: Any, source: str = "stdout") -> Optional[Dict[str, Any]]:
    """把 stdout/JSON 中的 finding 归一化为 Claude schema。

    兼容 `path` 别名，强制 file/summary/failure_scenario 为 str、line 为 int，
    避免 evaluate 侧 `.strip()` / 行号比较因类型不一致崩溃。
    """
    if not isinstance(raw, dict):
        return None
    file_value = raw.get("file") or raw.get("path") or ""
    summary = raw.get("summary") or ""
    failure = raw.get("failure_scenario") or raw.get("description") or ""
    if not isinstance(file_value, str) or not file_value.strip():
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None
    if file_value.strip().lower() in ("n/a", "none", "-"):
        return None
    cleaned_summary = summary.strip()
    cleaned_failure = failure.strip() if isinstance(failure, str) else str(failure).strip()
    if (
        cleaned_summary.rstrip(".!。！ ").lower()
        in {"no issues found", "no findings", "no bugs found", "nothing to report", "none"}
        and cleaned_failure.lower() in ("", "n/a", "none", "-")
    ):
        return None
    finding: Dict[str, Any] = {
        "file": file_value.strip(),
        "line": _as_int(raw.get("line")),
        "summary": cleaned_summary,
        "failure_scenario": cleaned_failure,
        "source": source,
    }
    for extra in ("short_summary", "category", "verdict", "outcome"):
        if raw.get(extra) is not None:
            finding[extra] = raw.get(extra)
    return finding


def parse_findings_from_result(result_text: str) -> Dict[str, Any]:
    """Parse the final text and keep enough state to distinguish failure modes.

    Returns a dict with:
      findings: always a list
      status: structured | structured_empty | empty | unstructured | non_list
      raw_text: original text (used only when unstructured)
      structured: whether findings came from a JSON/list contract
    """
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", result_text, re.DOTALL)
    json_text = fence_match.group(1) if fence_match else result_text
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        if looks_like_empty_review(result_text):
            log("Fallback: review text explicitly reports no findings — treating as empty")
            return {
                "findings": [],
                "status": "empty",
                "raw_text": result_text,
                "structured": True,
            }
        log("stdout: no structured findings — raw text kept for audit; other channels may still have findings")
        return {
            "findings": [],
            "status": "unstructured",
            "raw_text": result_text,
            "structured": False,
        }

    if isinstance(parsed, list):
        valid = [
            item
            for item in (_normalize_claude_finding(raw) for raw in parsed)
            if item is not None
        ]
        if len(valid) != len(parsed):
            log(
                f"Fallback: dropped {len(parsed) - len(valid)} invalid stdout item(s); "
                f"{len(valid)} valid finding(s)"
            )
        log(f"Fallback level 1: parsed {len(valid)} finding(s) directly from stdout JSON")
        return {
            "findings": valid,
            "status": "structured" if valid else "structured_empty",
            "raw_text": result_text,
            "structured": True,
        }

    log("Fallback: stdout JSON is not a list — treating as unstructured")
    return {
        "findings": [],
        "status": "non_list",
        "raw_text": result_text,
        "structured": False,
        "parsed": parsed,
    }


def extract_findings_from_result(result_text: str) -> Any:
    """Backward-compatible wrapper around parse_findings_from_result()."""
    parsed = parse_findings_from_result(result_text)
    if parsed["status"] in {"structured", "structured_empty", "empty"}:
        return parsed["findings"]
    if parsed["status"] == "non_list":
        return parsed.get("parsed", [])
    return result_text


def _finding_key(item: Dict[str, Any]) -> tuple:
    return (
        str(item.get("file") or item.get("path") or "").strip(),
        item.get("line") if item.get("line") is not None else item.get("start_line"),
        str(item.get("summary") or "").strip().lower(),
        str(item.get("failure_scenario") or item.get("description") or "").strip().lower(),
    )


def _dedupe_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (file, line, summary, failure_scenario) 去重。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        key = _finding_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _load_collector_errors(
    results_dir: Path, instance_id: str, run_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Read hook collector failures for this run (distinguish failure from no findings)."""
    path = Path(results_dir) / "collector_errors.jsonl"
    if not path.exists():
        return []
    errors: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("instance_id") not in (None, instance_id):
                    continue
                if run_id is not None and entry.get("run_id") != run_id:
                    continue
                errors.append(entry)
    except OSError:
        return []
    return errors[-20:]


def _normalize_paths(findings: List[Dict[str, Any]], repo_path: Optional[Path]) -> List[Dict[str, Any]]:
    """Strip a checkout root from absolute finding paths so evaluation can match.

    Native ReportFindings paths are absolute; aacr-bench reference comments use
    repository-relative paths.
    """
    if repo_path is None:
        return findings
    roots = {str(Path(repo_path)), str(Path(repo_path).resolve())}
    prefixes = tuple(root.rstrip("/") + "/" for root in roots)
    out: List[Dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        value = item.get("file") or item.get("path") or ""
        if isinstance(value, str):
            for prefix in prefixes:
                if value.startswith(prefix):
                    item = dict(item)
                    item["file"] = value[len(prefix):]
                    break
        out.append(item)
    return out


def _extract_token_usage(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """从 Claude Code JSON 输出中提取 token 消耗明细。

    Claude Code 在 result 事件 / JSON envelope 中通过 model_usage（Python SDK 命名）
    或 modelUsage（TypeScript SDK 命名）提供 per-model token breakdown：
      {modelName: {inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD}}

    本函数将其汇总为总量，同时保留原始 per-model 明细。
    """
    # 兼容两种命名风格
    model_usage = envelope.get("model_usage") or envelope.get("modelUsage") or {}

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0

    for model_name, usage in model_usage.items():
        if not isinstance(usage, dict):
            continue
        total_input += usage.get("inputTokens", 0) or 0
        total_output += usage.get("outputTokens", 0) or 0
        total_cache_read += usage.get("cacheReadInputTokens", 0) or 0
        total_cache_creation += usage.get("cacheCreationInputTokens", 0) or 0

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_creation,
        "total_tokens": total_input + total_output + total_cache_read + total_cache_creation,
        "model_usage": model_usage if model_usage else None,
    }


def extract_from_stream_json(raw_stdout: str) -> Tuple[str, Dict[str, Any]]:
    """从 stream-json 输出提取最终文本与元数据。

    即使指定 --output-format json，模型仍有概率返回逐行 stream-json 事件，
    此处按行解析，取 type=result 的事件作为最终文本与元数据。
    """
    result_text = ""
    claude_meta: Dict[str, Any] = {}
    for line in raw_stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result_value = event.get("result", "")
            result_text = (
                result_value
                if isinstance(result_value, str)
                else json.dumps(result_value, ensure_ascii=False)
            )
            token_usage = _extract_token_usage(event)
            claude_meta = {
                "claude_duration_ms": event.get("duration_ms"),
                "claude_num_turns": event.get("num_turns"),
                "claude_cost_usd": event.get("total_cost_usd"),
                "claude_session_id": event.get("session_id"),
                "token_usage": token_usage,
            }
            break
    return result_text, claude_meta


def inspect_report_findings_tool_use(raw_stdout: str) -> Dict[str, Any]:
    """Inspect every ReportFindings tool_use block, including failed calls.

    The PostToolUse hook only sees calls that actually executed.  A call rejected
    by Claude's input validation still appears in stream-json, and its findings
    are often recoverable.  Returning call metadata here lets the reviewer
    distinguish "called empty" from "called but failed" and from "not called".
    """
    calls: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    results: Dict[str, bool] = {}
    for line in raw_stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "user":
            message = event.get("message") or {}
            content = message.get("content") or []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id:
                        results[str(tool_use_id)] = bool(block.get("is_error"))
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "ReportFindings":
                continue
            payload = block.get("input") or {}
            items = payload.get("findings") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                items = []
                payload_ok = False
            else:
                payload_ok = True
            calls.append({
                "tool_use_id": block.get("id"),
                "raw_count": len(items),
                "payload_ok": payload_ok,
                "level": payload.get("level") if isinstance(payload, dict) else None,
                "findings": [item for item in items if isinstance(item, dict)],
            })
            findings.extend(item for item in items if isinstance(item, dict))

    for call in calls:
        call["is_error"] = results.get(str(call.get("tool_use_id")), False)

    failed_calls = [call for call in calls if call.get("is_error")]
    successful_calls = [call for call in calls if not call.get("is_error")]
    successful_findings = [
        item for call in successful_calls for item in call.get("findings", [])
    ]
    failed_findings = [
        item for call in failed_calls for item in call.get("findings", [])
    ]
    empty_calls = [
        call for call in successful_calls
        if call.get("payload_ok") and call.get("raw_count") == 0
    ]
    if not calls:
        status = "not_called"
    elif findings:
        status = "called_with_findings"
    elif empty_calls:
        status = "called_empty"
    elif failed_calls:
        status = "called_failed"
    elif any(not call.get("payload_ok") for call in calls):
        status = "invalid_payload"
    else:
        status = "called_empty"
    return {
        "findings": findings,
        "successful_findings": successful_findings,
        "failed_findings": failed_findings,
        "status": status,
        "calls": calls,
        "successful_calls": successful_calls,
        "empty_calls": empty_calls,
        "failed_calls": failed_calls,
        "raw_count": sum(call.get("raw_count", 0) for call in calls),
    }


def extract_report_findings_tool_use(raw_stdout: str) -> List[Dict[str, Any]]:
    """Backward-compatible wrapper returning the recoverable findings only."""
    return inspect_report_findings_tool_use(raw_stdout)["findings"]


def _detect_retry_exhausted(review_process: subprocess.CompletedProcess) -> Optional[Dict[str, Any]]:
    """从 stderr 和 stdout stream-json 中检测 API retry 是否耗尽。

    当 Claude Code 内置 retry 全部用完仍失败时：
      - stream-json 中会出现 system/api_retry 事件，其 attempt == max_retries
      - stderr 中可能包含 "max retries" / "rate limit" 等关键词
    返回 None 表示未检测到；返回 dict 则包含错误详情。
    """
    if review_process.returncode == 0:
        return None

    retry_info: Optional[Dict[str, Any]] = None

    # 从 stream-json 事件中提取最后一次 api_retry 事件
    raw_stdout = review_process.stdout or ""
    last_retry_event: Optional[Dict[str, Any]] = None
    for line in raw_stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "api_retry":
            last_retry_event = event

    if last_retry_event:
        attempt = last_retry_event.get("attempt", 0)
        max_retries = last_retry_event.get("max_retries", 0)
        if attempt >= max_retries:
            retry_info = {
                "detected_from": "stream_json",
                "error": last_retry_event.get("error", "unknown"),
                "error_status": last_retry_event.get("error_status"),
                "attempt": attempt,
                "max_retries": max_retries,
            }
            log(f"Retry exhausted detected: {retry_info['error']} "
                f"(attempt {attempt}/{max_retries})")
            return retry_info

    # 从 stderr 关键词中兜底检测
    stderr = (review_process.stderr or "").lower()
    retry_keywords = ["max retries", "retry limit", "retries exceeded", "rate limit"]
    matched_keyword = next((kw for kw in retry_keywords if kw in stderr), None)
    if matched_keyword:
        retry_info = {
            "detected_from": "stderr_keyword",
            "matched_keyword": matched_keyword,
            "error": "unknown",
        }
        log(f"Retry exhausted detected from stderr keyword: '{matched_keyword}'")
        return retry_info

    return None


def _check_and_log_retry_exhausted(result_path: Path, results_dir: Path) -> bool:
    """读取结果 JSON 中的 retry_exhausted 字段，若非空则追加写入汇总日志。

    返回 True 表示该案例因 retry 耗尽而失败。
    汇总日志路径：<results_dir>/retry_exhausted.jsonl（与 StopFailure hook 写同一文件）。
    """
    try:
        with result_path.open("r", encoding="utf-8") as handle:
            result_data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return False

    retry_info = result_data.get("retry_exhausted")
    if not retry_info:
        return False

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instance_id": result_data.get("instance_id"),
        "source": "save_result",
        "exit_code": result_data.get("claude_exit_code"),
        "duration_seconds": result_data.get("duration_seconds"),
        **retry_info,
    }
    exhausted_log = config.retry_exhausted_path(results_dir)
    exhausted_log.parent.mkdir(parents=True, exist_ok=True)
    with exhausted_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    log(f"⚠ Retry exhausted for {result_data.get('instance_id')} — logged to {exhausted_log.name}")
    return True


def load_streamed_state(results_dir: Path, instance_id: str) -> Dict[str, Any]:
    """Read the MCP partial file and report whether it was present or empty."""
    path = config.partial_path(results_dir, instance_id).resolve()
    if not path.exists():
        log(f"No partial file at {path} — MCP tool not called or wrote elsewhere.")
        return {"findings": [], "status": "not_called", "path": str(path)}
    try:
        with path.open("r", encoding="utf-8") as handle:
            findings = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        log(f"Warning: could not read streamed findings {path}: {error}")
        return {"findings": [], "status": "read_error", "path": str(path), "error": str(error)}
    if not isinstance(findings, list):
        log(f"Warning: partial file {path.name} is not a JSON list")
        return {"findings": [], "status": "invalid", "path": str(path)}
    if findings:
        return {"findings": findings, "status": "present", "path": str(path)}
    log(f"Partial file {path.name} exists but contains 0 findings")
    return {"findings": [], "status": "empty", "path": str(path)}


def load_streamed_findings(results_dir: Path, instance_id: str) -> Optional[List[Dict[str, Any]]]:
    """Backward-compatible wrapper around load_streamed_state()."""
    state = load_streamed_state(results_dir, instance_id)
    return state["findings"] if state["findings"] else None


def _record_matches_run(record: Dict[str, Any], run_id: Optional[str], meta_run_id: Optional[str]) -> bool:
    if not run_id:
        return True
    record_run = record.get("run_id")
    if record_run:
        return record_run == run_id
    # Legacy records have no run id.  If the meta file itself belongs to a
    # different run, treat them as stale; otherwise keep backward compatibility.
    if meta_run_id and meta_run_id != run_id:
        return False
    return True


def load_hook_state(
    results_dir: Path,
    instance_id: str,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read hook findings plus the sidecar state that records empty/failed calls."""
    path = config.hook_findings_path(results_dir, instance_id).resolve()
    meta_path = config.hook_meta_path(results_dir, instance_id).resolve()
    meta: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                meta = loaded
        except (json.JSONDecodeError, OSError) as error:
            log(f"Warning: could not read hook meta {meta_path}: {error}")
    meta_run_id = meta.get("run_id") if isinstance(meta.get("run_id"), str) else None
    calls = [
        call for call in (meta.get("calls") or [])
        if isinstance(call, dict) and _record_matches_run(call, run_id, meta_run_id)
    ]

    findings: List[Dict[str, Any]] = []
    read_error: Optional[str] = None
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded_findings = json.load(handle)
            if isinstance(loaded_findings, list):
                findings = [
                    item for item in loaded_findings
                    if isinstance(item, dict) and _record_matches_run(item, run_id, meta_run_id)
                ]
        except (json.JSONDecodeError, OSError) as error:
            read_error = str(error)
            log(f"Warning: could not read hook findings {path}: {error}")

    if read_error:
        status = "read_error"
    elif findings and any(call.get("status") == "written" for call in calls):
        status = "called_with_findings"
    elif findings:
        status = "called_with_findings"
    elif calls:
        empty = any(call.get("status") == "empty" for call in calls)
        filtered = any(call.get("status") in {"all_filtered", "invalid_payload"} for call in calls)
        if empty and not filtered:
            status = "called_empty"
        elif filtered and not empty:
            status = "called_all_filtered"
        elif empty:
            status = "called_empty"
        else:
            status = "called_all_filtered"
    elif meta.get("reset"):
        status = "not_called"
    elif path.exists():
        status = "legacy_empty_file"
    else:
        status = "not_called"

    raw_count = sum(int(call.get("raw_count") or 0) for call in calls)
    normalized_count = sum(int(call.get("normalized_count") or 0) for call in calls)
    if findings:
        log(f"ReportFindings hook: {len(findings)} finding(s), status={status}")
    else:
        log(f"ReportFindings hook state: {status} (calls={len(calls)}, raw={raw_count})")
    return {
        "findings": findings,
        "status": status,
        "calls": calls,
        "meta": meta,
        "raw_count": raw_count,
        "normalized_count": normalized_count,
        "path": str(path),
        "meta_path": str(meta_path),
        "read_error": read_error,
    }


def load_hook_findings(results_dir: Path, instance_id: str) -> Optional[List[Dict[str, Any]]]:
    """Backward-compatible wrapper around load_hook_state()."""
    state = load_hook_state(results_dir, instance_id)
    return state["findings"] if state["findings"] else None


def save_result(
    results_dir: Path,
    instance: ReviewInstance,
    review_process: subprocess.CompletedProcess,
    started_at: str,
    duration_seconds: float,
    repo_path: Optional[Path] = None,
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.result_path(results_dir, instance.instance_id)
    raw_stdout = review_process.stdout or ""

    # stdout 可能是单 JSON envelope，或 stream-json（逐行事件）：
    # 即使指定 --output-format json，模型仍有概率返回 stream-json，故两种都兜底
    result_text = raw_stdout
    claude_meta: Dict[str, Any] = {}
    try:
        envelope = json.loads(raw_stdout)
    except json.JSONDecodeError:
        envelope = None

    if isinstance(envelope, dict):
        result_value = envelope.get("result", raw_stdout)
        result_text = (
            result_value
            if isinstance(result_value, str)
            else json.dumps(result_value, ensure_ascii=False)
        )
        token_usage = _extract_token_usage(envelope)
        claude_meta = {
            "claude_duration_ms": envelope.get("duration_ms"),
            "claude_num_turns": envelope.get("num_turns"),
            "claude_cost_usd": envelope.get("total_cost_usd"),
            "claude_session_id": envelope.get("session_id"),
            "token_usage": token_usage,
        }
        log(f"stdout parsed as single JSON envelope "
            f"(tokens: in={token_usage['input_tokens']}, out={token_usage['output_tokens']}, "
            f"cache_read={token_usage['cache_read_input_tokens']})")
    else:
        if envelope is not None:
            log("stdout JSON is not an object; treating it as result text")
        stream_result, stream_meta = extract_from_stream_json(raw_stdout)
        if stream_result:
            result_text = stream_result
            claude_meta = stream_meta
            log("stdout parsed as stream-json (line-delimited events)")
        else:
            log("stdout is neither JSON envelope nor stream-json; using raw stdout")

    # Hook-first policy: ReportFindings success is canonical.  tool_use, MCP and
    # stdout are only consulted when the hook is unavailable (not called, read
    # error, or no usable hook record).  A hook that explicitly returned an
    # empty list is still "available" and must not be overridden by another
    # channel.
    run_id = getattr(review_process, "review_run_id", None)
    hook_state = load_hook_state(results_dir, instance.instance_id, run_id)
    hook_findings = _normalize_paths(hook_state["findings"], repo_path)
    hook_findings = _dedupe_findings(hook_findings) if hook_findings else []
    hook_count = len(hook_findings)
    hook_available = (
        hook_state["status"] == "called_empty"
        or (hook_state["status"] == "called_with_findings" and hook_count > 0)
    )
    tool_state = inspect_report_findings_tool_use(raw_stdout)
    tool_use_findings = _normalize_paths(tool_state["findings"], repo_path)
    tool_use_findings = _dedupe_findings(tool_use_findings) if tool_use_findings else []
    tool_use_count = len(tool_use_findings)
    tool_use_successful_findings = [
        item
        for item in (
            _normalize_claude_finding(raw, "ReportFindings")
            for raw in tool_state["successful_findings"]
        )
        if item is not None
    ]
    tool_use_successful_findings = _normalize_paths(
        tool_use_successful_findings, repo_path
    )
    tool_use_successful_findings = (
        _dedupe_findings(tool_use_successful_findings)
        if tool_use_successful_findings else []
    )
    tool_use_successful_count = len(tool_use_successful_findings)
    mcp_state = load_streamed_state(results_dir, instance.instance_id)
    streamed_findings = _normalize_paths(mcp_state["findings"], repo_path)
    streamed_findings = _dedupe_findings(streamed_findings) if streamed_findings else []
    mcp_count = len(streamed_findings)
    stdout_state = parse_findings_from_result(result_text)
    stdout_findings = _normalize_paths(stdout_state["findings"], repo_path)
    stdout_findings = _dedupe_findings(stdout_findings) if stdout_findings else []
    stdout_count = len(stdout_findings)
    stdout_is_structured = bool(stdout_state["structured"])
    collection_errors = _load_collector_errors(
        results_dir, instance.instance_id, run_id
    )

    log(
        f"Finding counts — hook: {hook_count} ({hook_state['status']}), "
        f"tool_use: {tool_use_count} ({tool_state['status']}; "
        f"successful={tool_use_successful_count}), "
        f"MCP: {mcp_count} ({mcp_state['status']}), "
        f"stdout: {stdout_count} ({stdout_state['status']})"
    )

    selection_reason = ""
    if hook_available:
        if hook_findings:
            review_output = hook_findings
            review_output_source = "report_findings"
            selection_reason = "hook_primary"
            log(f"Result: using hook ({hook_count} findings) — hook-first policy")
        else:
            review_output = []
            if hook_state["status"] == "called_empty":
                review_output_source = "report_findings_empty"
                selection_reason = "hook_empty"
            else:
                review_output_source = "report_findings_all_filtered"
                selection_reason = "hook_all_filtered"
            log(
                f"Result: hook is available but empty ({review_output_source}); "
                "no fallback"
            )
    elif tool_use_successful_findings:
        review_output = tool_use_successful_findings
        review_output_source = "report_findings_tool_use"
        selection_reason = "hook_unavailable_tool_use"
        log(
            f"Result: hook unavailable; using successful tool_use "
            f"({tool_use_successful_count} findings)"
        )
    elif streamed_findings:
        review_output = streamed_findings
        review_output_source = "mcp_stream"
        selection_reason = "hook_unavailable_mcp"
        log(f"Result: hook unavailable; using MCP ({mcp_count} findings)")
    elif stdout_findings:
        review_output = stdout_findings
        review_output_source = "stdout_fallback"
        selection_reason = "hook_unavailable_stdout"
        log(f"Result: hook unavailable; using stdout ({stdout_count} findings)")
    else:
        # No usable channel.  Keep review_output a list and record the actual
        # reason instead of letting raw prose flow into eval.
        review_output = []
        selection_reason = "no_structured_channel"
        if hook_state["status"] == "read_error" or mcp_state["status"] == "read_error":
            review_output_source = "collection_error"
        elif tool_state["status"] == "called_failed":
            review_output_source = "report_findings_failed"
        elif (
            tool_state["status"] == "called_all_filtered"
            or tool_state["status"] == "invalid_payload"
        ):
            review_output_source = "report_findings_all_filtered"
        elif collection_errors:
            review_output_source = "collection_error"
        elif stdout_state["status"] == "unstructured":
            review_output_source = "stdout_unstructured"
        elif stdout_state["status"] in {"structured_empty", "empty"}:
            review_output_source = "stdout_empty"
        elif mcp_state["status"] == "empty":
            review_output_source = "mcp_empty"
        else:
            review_output_source = "not_called"
        log(f"Result: no non-empty structured channel — source={review_output_source}")

    # 检测是否因 API retry 耗尽而失败（从 stderr 和 stream-json 事件中识别）
    retry_exhausted = _detect_retry_exhausted(review_process)
    if collection_errors and not any((hook_count, tool_use_count, mcp_count, stdout_count)):
        log(f"Collection errors recorded for this instance: {len(collection_errors)}")

    if (
        collection_errors
        and not any((hook_count, tool_use_count, mcp_count, stdout_count))
        and review_output_source != "collection_error"
    ):
        review_output_source = "collection_error"
        log("Result: no findings captured and collector errors exist — marking collection_error")

    safe_id = config.safe_id(instance.instance_id)
    stream_log_path = results_dir / f"{safe_id}.claude_stream.jsonl"
    stderr_log_path = results_dir / f"{safe_id}.claude_stderr.log"
    try:
        stream_log_path.write_text(raw_stdout, encoding="utf-8")
        stderr_log_path.write_text(review_process.stderr or "", encoding="utf-8")
    except OSError as error:
        log(f"Warning: could not write raw Claude logs: {error}")

    payload = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "head_commit": instance.head_commit,
        "reviewer": "claude-code/code-review",
        "model": os.environ.get(config.CLAUDE_MODEL_VAR),
        "started_at": started_at,
        "duration_seconds": round(duration_seconds, 2),
        "claude_exit_code": review_process.returncode,
        "retry_exhausted": retry_exhausted,
        "review_output_source": review_output_source,
        "review_output": review_output,
        "selection_reason": selection_reason,
        "hook_available": hook_available,
        "tool_use_successful_count": tool_use_successful_count,
        # 保留所有通道的原始捕获，便于事后审计/重新合并；评分仍以 review_output 为准。
        "channel_outputs": {
            "report_findings": hook_findings,
            "report_findings_tool_use": tool_use_findings,
            "report_findings_tool_use_successful": tool_use_successful_findings,
            "mcp_stream": streamed_findings,
            "stdout": stdout_findings,
        },
        "channel_status": {
            "report_findings": hook_state["status"],
            "report_findings_tool_use": tool_state["status"],
            "mcp_stream": mcp_state["status"],
            "stdout": stdout_state["status"],
        },
        "channel_meta": {
            "report_findings": {
                "raw_count": hook_state["raw_count"],
                "normalized_count": hook_state["normalized_count"],
                "calls": hook_state["calls"][-20:],
                "meta_path": hook_state["meta_path"],
            },
            "report_findings_tool_use": {
                "raw_count": tool_state["raw_count"],
                "calls": tool_state["calls"][-20:],
                "successful_calls": len(tool_state["successful_calls"]),
                "empty_calls": len(tool_state["empty_calls"]),
                "failed_calls": len(tool_state["failed_calls"]),
                "successful_count": tool_use_successful_count,
            },
            "mcp_stream": {"path": mcp_state.get("path"), "status": mcp_state["status"]},
            "stdout": {"status": stdout_state["status"], "structured": stdout_is_structured},
        },
        "finding_counts": {
            "report_findings": hook_count,
            "report_findings_tool_use": tool_use_count,
            "report_findings_tool_use_successful": tool_use_successful_count,
            "mcp_stream": mcp_count,
            "stdout": stdout_count,
            "stdout_structured": stdout_is_structured,
        },
        "raw_review_text": result_text if stdout_state["status"] == "unstructured" else None,
        "raw_review_text_length": len(result_text),
        "run_id": run_id,
        "collection_errors": collection_errors,
        "claude_stream_log": str(stream_log_path),
        "claude_stderr_log": str(stderr_log_path),
        "stderr": review_process.stderr,
        **claude_meta,
    }
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    # partial 已折叠进最终结果，删除以免下次运行残留
    partial = config.partial_path(results_dir, instance.instance_id).resolve()
    if partial.exists():
        try:
            partial.unlink()
        except OSError as error:
            log(f"Warning: could not remove partial file {partial.name}: {error}")

    return out_path


def review_instance(
    instance: ReviewInstance,
    repo_dir: Path,
    results_dir: Path,
    claude_env: Dict[str, str],
    timeout_minutes: int = 30,
    preview: bool = False,
) -> Dict[str, Any]:
    """对单条样本执行 Claude Code 评审，返回状态摘要。"""
    log(f"=== [claude] Processing {instance.instance_id} ===")

    repo_path = prepare_repo(
        repo_dir=repo_dir,
        clone_url=instance.resolved_clone_url,
        repo_full_name=instance.repo,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
    )

    stat = diff_stat(repo_path, instance.base_commit, instance.head_commit)
    log(f"diff stat: {stat}")

    if preview:
        log("Preview mode: skipping Claude call.")
        return {"instance_id": instance.instance_id, "status": "preview", "diff_stat": stat}

    # 清理上一次运行可能残留的 partial/hook 文件。  Hook 元数据会记录本次
    # run_id；即使自定义 harness 忘了调用本函数，save_result 也会按 run_id
    # 过滤掉旧记录。
    run_id = f"{config.safe_id(instance.instance_id)}-{uuid.uuid4().hex[:12]}"
    prepare_instance_result_files(results_dir, instance.instance_id, run_id)

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    review_process = run_claude_review(
        repo_path=repo_path,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
        instance_id=instance.instance_id,
        results_dir=results_dir,
        claude_env=claude_env,
        timeout_minutes=timeout_minutes,
        run_id=run_id,
    )
    duration_seconds = time.monotonic() - start_time

    out_path = save_result(
        results_dir=results_dir,
        instance=instance,
        review_process=review_process,
        started_at=started_at,
        duration_seconds=duration_seconds,
        repo_path=repo_path,
    )

    # 丢弃 Claude 在仓库里留下的临时文件，保持工作区干净
    clean_worktree(repo_path)

    # 检查结果文件中是否标记了 retry 耗尽，记录到汇总日志
    retry_exhausted = _check_and_log_retry_exhausted(out_path, results_dir)

    status = "ok" if review_process.returncode == 0 else "failed"
    log(f"--- [claude] {instance.instance_id} {status} (exit={review_process.returncode}) -> {out_path}")
    return {
        "instance_id": instance.instance_id,
        "status": status,
        "exit_code": review_process.returncode,
        "result_path": str(out_path),
        "retry_exhausted": retry_exhausted,
    }
