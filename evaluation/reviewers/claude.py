"""Claude Code 评审器（MCP findings 增量上报 + stdout 兜底）。

整合自 eval/run_review_claude.py：调用官方 `/code-review <base>...<head>`，
让 Claude Code 的 agent 自行探索仓库并评审 diff 范围。

结构化输出的可靠性靠两条腿：
  1. MCP findings server：Claude 每确认一条 finding 就调一次 report 工具，
     增量写入 <safe_id>.partial.json，与最终文本格式解耦。
  2. stdout 解析兜底：兼容单 JSON envelope 与逐行 stream-json（模型即使被要求
     输出 json 仍可能返回 stream-json），从中取最终文本再解析 finding；
     无法解析时按空评审 / 原始文本处理。
最终取两者中 finding 数量较多的一方（平局优先 MCP）。

LLM 配置通过环境变量提供：
  ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
    """追加到 Claude 系统提示：要求每条 finding 立即调一次 report 工具。"""
    return (
        "STRUCTURED OUTPUT REQUIREMENT (highest priority):\n"
        f"You have an MCP tool named `{config.MCP_REPORT_TOOL}`. The moment you "
        "confirm any individual code-review finding, immediately call this tool "
        "once for that single finding — do not wait until the end or batch them "
        "into one final message.\n"
        "The tool is already bound to the instance under review, so you do NOT "
        "pass any instance id. For every call provide ONLY the finding itself:\n"
        "  - file: path relative to the repository root\n"
        "  - line: the 1-based line number, or omit it if not applicable\n"
        "  - summary: a concise description of the issue\n"
        "  - failure_scenario: a concrete failure scenario plus the suggested fix\n"
        "Call the tool exactly once per finding, in addition to whatever you "
        "normally print. Any finding not sent through this tool will be lost."
    )


def build_hook_settings(results_dir: Path, instance_id: str) -> Dict[str, Any]:
    """构造包含 StopFailure hook 的 settings，用于 --settings 传入。

    当 Claude Code 内置 API retry 耗尽后触发 StopFailure 事件，
    hook 脚本将失败信息追加写入 retry_exhausted.jsonl。
    """
    absolute_results_dir = Path(results_dir).resolve()
    hook_script = str(config.STOP_FAILURE_HOOK_SCRIPT.resolve())
    return {
        "hooks": {
            "StopFailure": [
                {
                    "matcher": "rate_limit|overloaded|server_error|unknown",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{sys.executable} {hook_script}",
                            "timeout": 10000,
                            "env": {
                                config.MCP_RESULTS_DIR_ENV_VAR: str(absolute_results_dir),
                                config.MCP_INSTANCE_ID_ENV_VAR: instance_id,
                            },
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
) -> subprocess.CompletedProcess:
    """运行官方 /code-review，评审 base...head 范围。"""
    review_target = f"{base_commit}...{head_commit}"
    slash_command = f"/code-review {review_target} "

    mcp_config = build_mcp_config(results_dir, instance_id)
    findings_prompt = build_findings_system_prompt()
    hook_settings = build_hook_settings(results_dir, instance_id)
    settings_path = _write_temp_settings(hook_settings)

    command = [
        "claude",
        "-p",
        slash_command,
        "--add-dir", str(repo_path),
        "--permission-mode", "acceptEdits",
        "--output-format", "json",
        "--mcp-config", json.dumps(mcp_config),
        "--allowed-tools", config.MCP_REPORT_TOOL,
        "--append-system-prompt", findings_prompt,
        "--settings", settings_path,
    ]

    process_timeout = timeout_minutes * 60 + 60
    environment = os.environ.copy()
    environment.update(claude_env)
    log(f"$ claude -p '/code-review {review_target}'  (cwd={repo_path})")
    try:
        return subprocess.run(
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
        return subprocess.CompletedProcess(
            args=command,
            returncode=-1,
            stdout=timeout_error.stdout or "",
            stderr=f"Process timed out after {process_timeout} seconds",
        )
    finally:
        try:
            os.unlink(settings_path)
        except OSError:
            pass


def looks_like_empty_review(review_text: str) -> bool:
    """文本是否明确表示无 finding（命中则直接判为空评审）。"""
    if not review_text or not review_text.strip():
        return True
    lowered = review_text.lower()
    return any(re.search(pattern, lowered, re.MULTILINE) for pattern in _EMPTY_REVIEW_PATTERNS)


def extract_findings_from_result(result_text: str) -> Any:
    """从最终文本解析 findings：直接 JSON -> 空评审 -> 原始文本兜底。"""
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", result_text, re.DOTALL)
    json_text = fence_match.group(1) if fence_match else result_text
    try:
        parsed = json.loads(json_text)
        count = len(parsed) if isinstance(parsed, list) else "?"
        log(f"Fallback level 1: parsed {count} finding(s) directly from stdout JSON")
        return parsed
    except json.JSONDecodeError:
        if looks_like_empty_review(result_text):
            log("Fallback: review text explicitly reports no findings — treating as empty")
            return []
        log("Fallback level 2 (WORST CASE): storing raw text — needs manual review / rerun")
        return result_text


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
        if event.get("type") == "result":
            result_text = event.get("result", "")
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


def load_streamed_findings(results_dir: Path, instance_id: str) -> Optional[List[Dict[str, Any]]]:
    """读取 MCP server 增量写入的 partial 文件；为空或不存在返回 None。"""
    path = config.partial_path(results_dir, instance_id).resolve()
    if not path.exists():
        log(f"No partial file at {path} — MCP tool not called or wrote elsewhere.")
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            findings = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        log(f"Warning: could not read streamed findings {path}: {error}")
        return None
    if isinstance(findings, list) and findings:
        return findings
    log(f"Partial file {path.name} exists but contains 0 findings")
    return None


def save_result(
    results_dir: Path,
    instance: ReviewInstance,
    review_process: subprocess.CompletedProcess,
    started_at: str,
    duration_seconds: float,
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
        result_text = envelope.get("result", raw_stdout)
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
    except json.JSONDecodeError:
        stream_result, stream_meta = extract_from_stream_json(raw_stdout)
        if stream_result:
            result_text = stream_result
            claude_meta = stream_meta
            log("stdout parsed as stream-json (line-delimited events)")
        else:
            log("stdout is neither JSON envelope nor stream-json; using raw stdout")

    # 双路比较：MCP partial vs stdout 解析，取数量多者，平局优先 MCP
    streamed_findings = load_streamed_findings(results_dir, instance.instance_id)
    mcp_count = len(streamed_findings) if streamed_findings is not None else 0
    stdout_findings = extract_findings_from_result(result_text)
    stdout_is_structured = isinstance(stdout_findings, list)
    stdout_count = len(stdout_findings) if stdout_is_structured else 0

    log(
        f"Finding counts — MCP: {mcp_count}, "
        f"stdout: {stdout_count if stdout_is_structured else 'unstructured/raw-text'}"
    )

    if mcp_count == 0 and not stdout_is_structured:
        review_output = stdout_findings
        review_output_source = "stdout_fallback"
        log("Result: no structured findings from either source — storing RAW TEXT")
    elif mcp_count >= stdout_count:
        review_output = streamed_findings if streamed_findings is not None else []
        review_output_source = "mcp_stream"
        reason = "more findings" if mcp_count > stdout_count else "tie, prefer MCP"
        log(f"Result: using MCP ({mcp_count}) over stdout ({stdout_count}) — {reason}")
    else:
        review_output = stdout_findings
        review_output_source = "stdout_fallback"
        log(f"Result: using stdout ({stdout_count}) over MCP ({mcp_count}) — recovered from text")

    # 检测是否因 API retry 耗尽而失败（从 stderr 和 stream-json 事件中识别）
    retry_exhausted = _detect_retry_exhausted(review_process)

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

    # 清理上一次运行可能残留的 partial 文件
    results_dir.mkdir(parents=True, exist_ok=True)
    stale_partial = config.partial_path(results_dir, instance.instance_id).resolve()
    if stale_partial.exists():
        stale_partial.unlink()

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
    )
    duration_seconds = time.monotonic() - start_time

    out_path = save_result(
        results_dir=results_dir,
        instance=instance,
        review_process=review_process,
        started_at=started_at,
        duration_seconds=duration_seconds,
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
