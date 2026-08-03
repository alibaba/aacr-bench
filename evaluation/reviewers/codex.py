"""Codex 评审器（MCP findings 增量上报 + JSONL 兜底，与 Claude 同构）。

调用 `codex exec "<prompt>"` 评审 base...head 范围，整体结构对照
`reviewers/claude.py`：双通道取多（MCP partial 主，JSONL 解析辅，取数量多者，
平局优先 MCP）。

与 Claude 的关键差异：
- 命令：`codex exec "..."`（review 必须在引号内）；用 `--sandbox workspace-write`，
  不用已弃用的 `--full-auto`；不传 `--ask-for-approval`（不是 exec 的参数）；
  不传 `--ignore-user-config`（实测会一起跳过 $CODEX_HOME/config.toml，
  导致 model_provider / MCP server 配置全部丢失，退回默认 openai provider）。
- MCP 接入：模型三件套与 MCP server 全部写进临时 `config.toml`（$CODEX_HOME
  重定向方案 D），MCP server 通过 `env_vars=[...]` 字段继承父进程的
  REVIEW_RESULTS_DIR / REVIEW_INSTANCE_ID。
- finding schema：codex 专用 `{file, start_line, end_line, severity, summary,
  description}`（与 Claude 的 `{file, line, summary, failure_scenario}` 不同），
  由独立 MCP server `mcp_codex_finding_server.py` 定义。
- token 取自 `--json` 的 JSONL 流的 `turn.completed.usage` 事件（codex 承诺
  `--json` 是稳定契约）。

LLM 配置通过环境变量提供：
  CODEX_API_KEY（必填）, CODEX_MODEL（可选）, CODEX_GATEWAY_URL（可选）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from repo_utils import clean_worktree, diff_stat, log, prepare_repo, run_command
from schema import ReviewInstance

# MCP server 启动命令用当前解释器（容器内无 python symlink，只有 python3）
MCP_SERVER_COMMAND = sys.executable

# 显式表示"无 finding"的短语，命中则直接判为空评审
_EMPTY_REVIEW_PATTERNS = [
    r"\bno\s+findings?\b",
    r"\bno\s+issues?\s+found\b",
    r"\bnothing\s+to\s+report\b",
    r"\bno\s+comments?\b",
    r"^\s*\(?\s*none\s*\)?\s*$",
]


# 拼进 codex exec prompt 引号内的 finding 上报说明片段。
# 参数说明按 codex schema 重写（不是 Claude 版的 file/line/summary/failure_scenario）。
# 工具名写 `report`（codex 不带 mcp__ 前缀）。
INLINE_FINDINGS_PROMPT_FRAGMENT = (
    "You have an MCP tool named `report`. The moment you confirm any individual "
    "code-review finding, immediately call this tool once for that single finding "
    "— do not wait until the end or batch them into one final message. The tool is "
    "already bound to the instance under review, so you do NOT pass any instance id. "
    "For every call provide ONLY the finding itself:\n"
    "  - file: path relative to the repository root\n"
    "  - start_line: the 1-based starting line number\n"
    "  - end_line: the 1-based ending line number (for single-line issues, set equal to start_line or omit)\n"
    "  - severity: one of Critical, Major, Minor, Trivial, Info\n"
    "  - summary: a concise one-line title of the issue\n"
    "  - description: detailed explanation including failure scenario and suggested fix\n"
    "Call the tool exactly once per finding, in addition to whatever you normally "
    "print. Any finding not sent through this tool will be lost."
)


def ensure_codex_installed() -> None:
    result = run_command(["codex", "--version"])
    if result.returncode != 0:
        raise SystemExit(
            "codex CLI not found. Install Codex CLI, e.g.: npm i -g @openai/codex"
        )
    log(f"codex available: {result.stdout.strip()}")


def resolve_codex_env() -> Dict[str, str]:
    """从环境变量解析 Codex 配置，缺失则早报错。

    返回的环境 dict 用于注入 codex 子进程。必填 CODEX_API_KEY；可选
    CODEX_MODEL / CODEX_GATEWAY_URL（后者用于写入临时 config.toml 的网关段）。
    """
    missing = config.missing_env_vars(config.CODEX_REQUIRED_ENV_VARS)
    if missing:
        raise SystemExit(
            f"Missing env vars: {', '.join(missing)}. "
            "Run: set -a && source evaluation/.env && set +a"
        )

    env: Dict[str, str] = {
        config.CODEX_API_KEY_VAR: os.environ[config.CODEX_API_KEY_VAR]
    }

    model = os.environ.get(config.CODEX_MODEL_VAR)
    if model:
        env[config.CODEX_MODEL_VAR] = model
        log(f"CODEX_MODEL = {model}")

    gateway_url = os.environ.get(config.CODEX_GATEWAY_URL_VAR)
    if gateway_url:
        env[config.CODEX_GATEWAY_URL_VAR] = gateway_url
        log(f"CODEX_GATEWAY_URL = {gateway_url}")

    return env


def _toml_escape(s: str) -> str:
    """转义 TOML basic string 里的特殊字符（基本字符串用双引号包裹）。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_codex_config(codex_env: Dict[str, str]) -> Path:
    """生成临时 CODEX_HOME 目录及 config.toml，返回该目录路径。

    在 review 阶段开始时一次性生成（所有实例共享），per-instance 的
    REVIEW_RESULTS_DIR / REVIEW_INSTANCE_ID 通过 env_vars 字段从父进程继承，
    不写进 config.toml。

    参考 ~/.codex/config.toml 标准格式：
      - 模型三件套写进 model / model_provider / [model_providers."<id>"]
      - MCP server 写进 [mcp_servers.findings]，用 env_vars 字段继承父进程 env

    可复现性：不能使用 --ignore-user-config（实测会一起跳过 $CODEX_HOME/config.toml，
    导致 model_provider / MCP server 配置全部丢失）。Codex CLI 合并式加载
    ~/.codex/config.toml（base）+ $CODEX_HOME/config.toml（profile overlay），
    用户本地配置不含 [mcp_servers] 段，因此 MCP server 配置不会被遮蔽；
    model / model_provider 字段在叠加时后者覆盖前者，临时 config 优先。
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Codex 拒绝在 /tmp 下创建 sandbox helper 二进制文件（安全策略），
    # Docker 场景下通过 CODEX_HOME_BASE 指定非 /tmp 的持久化目录。
    home_base = os.environ.get("CODEX_HOME_BASE", tempfile.gettempdir())
    tmp_home = Path(home_base) / f"codex_home_{timestamp}"
    tmp_home.mkdir(parents=True, exist_ok=False)
    config_toml = tmp_home / "config.toml"

    pid = config.CODEX_PROVIDER_ID
    gateway_url = codex_env.get(config.CODEX_GATEWAY_URL_VAR)
    model = codex_env.get(config.CODEX_MODEL_VAR)
    mcp_script_abs = str(config.CODEX_MCP_SERVER_SCRIPT.resolve())

    lines: List[str] = []

    # === 模型三件套之二：model_name（顶级 model = "..."）===
    if model:
        lines.append(f'model = "{_toml_escape(model)}"')
        lines.append("")

    # === 模型三件套之三：base_url（自建网关，可选）===
    if gateway_url:
        lines.append(f'model_provider = "{_toml_escape(pid)}"')
        lines.append("")
        lines.append(f'[model_providers."{_toml_escape(pid)}"]')
        lines.append(
            f'name = "{_toml_escape(pid)}"'
        )  # 必填：实测缺此字段 fallback 到默认 provider
        lines.append(f'base_url = "{_toml_escape(gateway_url)}"')
        lines.append('env_key = "CODEX_API_KEY"')  # codex 经此 env var 自动取 api_key
        lines.append("")

    # === MCP server（codex 专用）===
    lines.append(f"[mcp_servers.{config.CODEX_MCP_SERVER_NAME}]")
    lines.append(f'command = "{_toml_escape(MCP_SERVER_COMMAND)}"')
    lines.append(f'args = ["{_toml_escape(mcp_script_abs)}"]')
    lines.append(
        'env_vars = ["REVIEW_RESULTS_DIR", "REVIEW_INSTANCE_ID"]'
    )  # 从父进程继承
    lines.append("required = true")
    # exec 是非交互模式，无 TTY 供用户审批 MCP tool call；
    # 缺此字段时 codex 把待审批的 tool call 当作 "user cancelled" 直接判失败
    # （trace: status=failed / error="user cancelled MCP tool call"），server 的
    # report() 从未执行，partial.json 不生成。必须显式自动批准。
    lines.append('default_tools_approval_mode = "approve"')

    config_toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote codex config.toml -> {config_toml}")
    return tmp_home


def cleanup_codex_home(tmp_home: Optional[Path]) -> None:
    """review 阶段结束后清理临时 CODEX_HOME 目录。

    codex 会在临时目录存 sessions/auth/hooks 状态，清理避免遗留。
    若设置了 CODEX_HOME_BACKUP 环境变量，则将 CODEX_HOME 移动到该目录
    下进行备份（保留 sessions/auth/hooks）；否则直接删除。
    """
    if tmp_home is None:
        return
    backup_root = os.environ.get("CODEX_HOME_BACKUP")
    if backup_root:
        try:
            backup_dir = Path(backup_root)
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / tmp_home.name
            shutil.move(str(tmp_home), str(dest))
            log(f"Backed up tmp CODEX_HOME -> {dest}")
        except OSError as error:
            log(
                f"Warning: could not back up tmp CODEX_HOME "
                f"{tmp_home} -> {backup_root}: {error}"
            )
        return
    try:
        shutil.rmtree(tmp_home, ignore_errors=True)
        log(f"Cleaned up tmp CODEX_HOME: {tmp_home}")
    except OSError as error:
        log(f"Warning: could not remove tmp CODEX_HOME {tmp_home}: {error}")


def build_findings_prompt(
    base_commit: str,
    head_commit: str,
) -> str:
    """构造 codex exec 的 prompt。

    关键约定（详见 PLAN_CODEX_SUPPORT.md §二）：
    - review 动词必须放进 prompt 引号内（`codex exec` 后只接单个提示词位置参数，
      不存在 `exec review` 子命令）。
    - MCP 工具说明以括号形式拼进同一 prompt。
    """
    review_target = f"{base_commit}...{head_commit}"
    return f"/review {review_target} ({INLINE_FINDINGS_PROMPT_FRAGMENT})"


def run_codex_review(
    repo_path: Path,
    base_commit: str,
    head_commit: str,
    instance_id: str,
    results_dir: Path,
    codex_env: Dict[str, str],
    codex_home: Path,
    timeout_minutes: int,
) -> subprocess.CompletedProcess:
    """运行 `codex exec "<prompt>"`，评审 base...head 范围。

    通过环境变量注入：
      - CODEX_HOME=<tmp>             （让 codex 读 pipeline 生成的临时 config.toml）
      - REVIEW_RESULTS_DIR=<absolute> （MCP server 经 env_vars 继承）
      - REVIEW_INSTANCE_ID=<safe_id>   （MCP server 经 env_vars 继承）
      - CODEX_API_KEY=<key>
    """
    prompt = build_findings_prompt(base_commit, head_commit)
    review_target = f"{base_commit}...{head_commit}"

    command = [
        "codex",
        "--sandbox",
        "workspace-write",  # 不用已弃用的 --full-auto
        "exec",
        "review",
        "--json",  # JSONL 事件流（辅通道 + token）
        "--skip-git-repo-check",  # cwd 是 repo_path，避免 git 检查噪声
        prompt,
    ]

    process_timeout = timeout_minutes * 60 + 60

    # REVIEW_RESULTS_DIR 必须绝对路径：codex 启动 MCP server 时 cwd 指向被评审仓库，
    # 相对路径会解析到仓库目录，partial 文件写错位置（与 Claude 同样的坑）。
    absolute_results_dir = Path(results_dir).resolve()
    safe_id_value = config.safe_id(instance_id)

    environment = os.environ.copy()
    environment.update(codex_env)
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            config.MCP_RESULTS_DIR_ENV_VAR: str(absolute_results_dir),
            config.MCP_INSTANCE_ID_ENV_VAR: safe_id_value,
        }
    )

    log(f'$ codex exec "/review {review_target}"  (cwd={repo_path})')
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
        log(f"TIMEOUT: codex process exceeded {process_timeout}s — treating as failed")
        return subprocess.CompletedProcess(
            args=command,
            returncode=-1,
            stdout=timeout_error.stdout or "",
            stderr=f"Process timed out after {process_timeout} seconds",
        )


def looks_like_empty_review(review_text: str) -> bool:
    """文本是否明确表示无 finding（命中则直接判为空评审）。"""
    if not review_text or not review_text.strip():
        return True
    lowered = review_text.lower()
    return any(
        re.search(pattern, lowered, re.MULTILINE) for pattern in _EMPTY_REVIEW_PATTERNS
    )


def extract_findings_from_result(result_text: str) -> Any:
    """从最终文本解析 findings：直接 JSON -> 空评审 -> 原始文本兜底。

    用于 `--json` 流里 `agent_message` 文本的兜底解析（与 Claude 版同构）。
    """
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", result_text, re.DOTALL)
    json_text = fence_match.group(1) if fence_match else result_text
    try:
        parsed = json.loads(json_text)
        count = len(parsed) if isinstance(parsed, list) else "?"
        log(f"Fallback level 1: parsed {count} finding(s) directly from stdout JSON")
        return parsed
    except json.JSONDecodeError:
        if looks_like_empty_review(result_text):
            log(
                "Fallback: review text explicitly reports no findings — treating as empty"
            )
            return []
        log(
            "Fallback level 2 (WORST CASE): storing raw text — needs manual review / rerun"
        )
        return result_text


def extract_from_jsonl(raw_stdout: str) -> Tuple[str, Dict[str, Any]]:
    """从 codex `--json` 的 JSONL 流提取最终文本与 token usage。

    codex 文档明确 `--json` 是稳定契约（逐行 JSONL 事件）。本函数解析：
    - `{"type":"turn.completed","usage":{...}}` → token 统计
      usage 字段：input_tokens / cached_input_tokens / output_tokens /
      reasoning_output_tokens
    - `{"type":"thread.started","thread_id":"..."}` → 主 session id（用于
      回退定位 review subagent 的 session rollout 文件）
    - `{"type":"item.completed","item":{"type":"agent_message","text":"..."}}`
      → 最终文本（兜底解析 finding）

    返回 (result_text, codex_meta)。
    """
    result_text = ""
    codex_meta: Dict[str, Any] = {}
    thread_id: Optional[str] = None

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

        event_type = event.get("type")

        if event_type == "turn.completed":
            usage = event.get("usage") or {}
            token_usage = _extract_codex_usage(usage)
            codex_meta = {
                "token_usage": token_usage,
                "codex_session_id": event.get("session_id"),
            }
        elif event_type == "thread.started":
            tid = event.get("thread_id")
            if tid:
                thread_id = tid
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text") or ""
                if text:
                    result_text = text

    if thread_id:
        codex_meta["thread_id"] = thread_id

    return result_text, codex_meta


def _extract_codex_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """把 codex turn.completed.usage 汇总为统一字段。

    codex 的 usage 字段：
      input_tokens / cached_input_tokens / output_tokens / reasoning_output_tokens

    total_tokens 只累加 input + output（与 Claude 对齐），reasoning tokens
    单列在原始字段保留供分析，但**不累加到 total_tokens**（避免与 Claude 不可比）。
    """
    input_tokens = usage.get("input_tokens", 0) or 0
    cached_input_tokens = usage.get("cached_input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    reasoning_output_tokens = usage.get("reasoning_output_tokens", 0) or 0

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        # 与 Claude 字段命名对齐（供 _extract_usage_from_result 复用 claude 分支）
        "cache_read_input_tokens": cached_input_tokens,
        "cache_creation_input_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }


def _usage_is_zero(usage: Dict[str, Any]) -> bool:
    """token usage 是否全为 0（说明 --json 流没拿到真实用量，需要回退）。"""
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    return sum(int(usage.get(k, 0) or 0) for k in keys) == 0


def _extract_usage_from_session(
    codex_home: Path, thread_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """从 CODEX_HOME 的 session rollout 文件回退提取 token 用量。

    背景：当 ``codex exec`` 把评审委派给 review subagent 时，--json 流的
    ``turn.completed.usage`` 为全 0（真实用量记录在 subagent 自己的 session
    文件里，不上报给主流）。

    链路：
    - --json 流的 ``thread.started.thread_id`` == 主 session 的 ``id``；
    - review subagent session 的 ``session_meta.payload.parent_thread_id`` 指向
      主 session（且 ``thread_source == "subagent"``）。

    本函数扫描 ``<codex_home>/sessions/**/*.jsonl``，对所有 parent_thread_id 命中
    的 subagent session 取其最后一个 ``token_count`` 事件的
    ``total_token_usage``（该字段为 cumulative），多个 subagent 则累加末值。
    """
    if not thread_id:
        return None
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        log(f"No sessions dir at {sessions_dir} — cannot recover token usage")
        return None

    aggregated: Dict[str, int] = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    found = False

    for rollout in sorted(sessions_dir.rglob("*.jsonl")):
        is_child = False
        last_usage: Optional[Dict[str, Any]] = None
        try:
            with rollout.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type == "session_meta":
                        meta = event.get("payload") or {}
                        if meta.get("parent_thread_id") == thread_id:
                            is_child = True
                    elif is_child and event_type == "event_msg":
                        payload = event.get("payload") or {}
                        if payload.get("type") != "token_count":
                            continue
                        info = payload.get("info") or {}
                        usage = info.get("total_token_usage")
                        if usage:
                            last_usage = usage
        except OSError as error:
            log(f"Warning: could not read session rollout {rollout}: {error}")
            continue

        if is_child and last_usage:
            found = True
            for key in aggregated:
                aggregated[key] += int(last_usage.get(key, 0) or 0)
            log(
                f"Recovered token usage from subagent session {rollout.name}: "
                f"in={last_usage.get('input_tokens')}, "
                f"out={last_usage.get('output_tokens')}, "
                f"cached={last_usage.get('cached_input_tokens')}, "
                f"reasoning={last_usage.get('reasoning_output_tokens')}"
            )

    if not found:
        return None
    return aggregated


def _detect_retry_exhausted(
    review_process: subprocess.CompletedProcess,
) -> Optional[Dict[str, Any]]:
    """简化版 retry 耗尽检测：仅看 exit code + stderr 关键词。

    与 Claude 版的差异：不再扫描 stream-json 事件；codex 没有公开的退出码表，
    也无 StopFailure hook 等价事件，靠 exit code != 0 + stderr 关键词兜底。
    """
    if review_process.returncode == 0:
        return None

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


def load_streamed_findings(
    results_dir: Path, instance_id: str
) -> Optional[List[Dict[str, Any]]]:
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
    codex_env: Dict[str, str],
    started_at: str,
    duration_seconds: float,
    codex_home: Optional[Path] = None,
) -> Path:
    """把 codex 评审结果落盘为 <safe_id>.json。

    双通道策略（与 Claude 一致）：
      - MCP partial 为主 finding 来源。
      - JSONL agent_message 文本解析为辅，兜底用。
      取两者中数量较多者（平局优先 MCP）。
    token_usage 从 JSONL turn.completed 事件取（无 --json 或解析失败为全 0）。
    当 review 委派给 subagent 时 turn.completed.usage 为全 0，此时回退到
    CODEX_HOME 下 review subagent 的 session rollout 文件提取真实用量。
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.result_path(results_dir, instance.instance_id)
    raw_stdout = review_process.stdout or ""

    # 解析 JSONL：取最终文本（agent_message）与 token usage（turn.completed）
    result_text = ""
    codex_meta: Dict[str, Any] = {}
    result_text, codex_meta = extract_from_jsonl(raw_stdout)
    if not result_text:
        log(
            "JSONL stream empty or no agent_message — falling back to raw stdout as result text"
        )
        result_text = raw_stdout

    token_usage = codex_meta.get("token_usage") or _extract_codex_usage({})

    # 回退：review subagent 场景下 --json 的 turn.completed.usage 为全 0，
    # 真实用量记录在 subagent 的 session rollout 文件里（按 parent_thread_id
    # 定位，取其 cumulative total_token_usage）
    if _usage_is_zero(token_usage):
        thread_id = codex_meta.get("thread_id")
        session_usage = _extract_usage_from_session(codex_home, thread_id)
        if session_usage:
            token_usage = _extract_codex_usage(session_usage)
            log(
                f"Recovered token usage from session rollout (thread_id={thread_id}): "
                f"in={token_usage['input_tokens']}, out={token_usage['output_tokens']}, "
                f"cached={token_usage['cached_input_tokens']}, "
                f"reasoning={token_usage['reasoning_output_tokens']}"
            )
        else:
            log(
                "Token usage is 0 and no subagent session found — "
                "tokens will be recorded as 0"
            )
    elif codex_meta.get("codex_session_id"):
        log(
            f"JSONL parsed: session={codex_meta['codex_session_id']}, "
            f"tokens: in={token_usage['input_tokens']}, out={token_usage['output_tokens']}, "
            f"cached={token_usage['cached_input_tokens']}, "
            f"reasoning={token_usage['reasoning_output_tokens']}"
        )

    # 双路比较：MCP partial vs JSONL 解析，取数量多者，平局优先 MCP
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
        review_output_source = "jsonl_fallback"
        log("Result: no structured findings from either source — storing RAW TEXT")
    elif mcp_count >= stdout_count:
        review_output = streamed_findings if streamed_findings is not None else []
        review_output_source = "mcp_stream"
        reason = "more findings" if mcp_count > stdout_count else "tie, prefer MCP"
        log(f"Result: using MCP ({mcp_count}) over stdout ({stdout_count}) — {reason}")
    else:
        review_output = stdout_findings
        review_output_source = "jsonl_fallback"
        log(
            f"Result: using stdout ({stdout_count}) over MCP ({mcp_count}) — recovered from text"
        )

    # 检测是否因 API retry 而耗尽（仅 stderr 关键词）
    retry_exhausted = _detect_retry_exhausted(review_process)

    model_name = codex_env.get(config.CODEX_MODEL_VAR)

    payload = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "head_commit": instance.head_commit,
        "reviewer": "codex",
        "model": model_name,
        "started_at": started_at,
        "duration_seconds": round(duration_seconds, 2),
        "codex_exit_code": review_process.returncode,
        "retry_exhausted": retry_exhausted,
        "review_output_source": review_output_source,
        "review_output": review_output,
        "token_usage": token_usage,
        "stderr": review_process.stderr,
        "stdout": raw_stdout,
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
    codex_env: Dict[str, str],
    codex_home: Path,
    timeout_minutes: int = 30,
    preview: bool = False,
) -> Dict[str, Any]:
    """对单条样本执行 codex 评审，返回状态摘要。

    codex_home 由调用方（pipeline）在 review 阶段开始时一次性生成，
    所有实例共享；本函数不创建 / 清理 codex_home。
    """
    log(f"=== [codex] Processing {instance.instance_id} ===")

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
        log("Preview mode: skipping codex call.")
        return {
            "instance_id": instance.instance_id,
            "status": "preview",
            "diff_stat": stat,
        }

    # 清理上一次运行可能残留的 partial 文件
    results_dir.mkdir(parents=True, exist_ok=True)
    stale_partial = config.partial_path(results_dir, instance.instance_id).resolve()
    if stale_partial.exists():
        stale_partial.unlink()

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    review_process = run_codex_review(
        repo_path=repo_path,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
        instance_id=instance.instance_id,
        results_dir=results_dir,
        codex_env=codex_env,
        codex_home=codex_home,
        timeout_minutes=timeout_minutes,
    )
    duration_seconds = time.monotonic() - start_time

    out_path = save_result(
        results_dir=results_dir,
        instance=instance,
        review_process=review_process,
        codex_env=codex_env,
        started_at=started_at,
        duration_seconds=duration_seconds,
        codex_home=codex_home,
    )

    # 丢弃 codex 在仓库里留下的临时文件，保持工作区干净
    clean_worktree(repo_path)

    status = "ok" if review_process.returncode == 0 else "failed"
    log(
        f"--- [codex] {instance.instance_id} {status} (exit={review_process.returncode}) -> {out_path}"
    )
    return {
        "instance_id": instance.instance_id,
        "status": status,
        "exit_code": review_process.returncode,
        "result_path": str(out_path),
    }
