"""OCR（OpenCodeReview）评审器。

整合自 eval/run_review.py：调用 `ocr review --from <base> --to <head>` 评审
base..head 的全量 diff，并把结果写成统一的结果文件 <safe_id>.json。

LLM 配置通过环境变量提供（ocr 自身读取）：
  OCR_LLM_URL, OCR_LLM_TOKEN, OCR_LLM_MODEL, OCR_USE_ANTHROPIC
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import config
from repo_utils import clean_worktree, log, prepare_repo, run_command
from schema import ReviewInstance


def ensure_ocr_installed(ocr_command: str = "ocr") -> None:
    result = run_command([ocr_command, "version"])
    if result.returncode != 0:
        raise SystemExit(
            f"ocr CLI not found at '{ocr_command}'. "
            "Install it via: npm install -g @alibaba-group/open-code-review"
        )
    first_line = result.stdout.strip().splitlines()[0] if result.stdout else "ok"
    log(f"ocr available ({ocr_command}): {first_line}")


def check_env(preview: bool) -> None:
    missing = config.missing_env_vars(config.OCR_REQUIRED_ENV_VARS)
    if missing and not preview:
        log(f"WARNING: missing OCR env vars: {', '.join(missing)}")
        log("ocr review will fail to call the model until these are set (see evaluation/.env.example).")


def run_ocr_review(
    repo_path: Path,
    base_commit: str,
    head_commit: str,
    output_format: str,
    concurrency: int,
    timeout_minutes: int,
    preview: bool,
    ocr_command: str = "ocr",
    max_tools: int = 30,
) -> subprocess.CompletedProcess:
    command = [
        ocr_command,
        "review",
        "--repo",
        str(repo_path),
        "--from",
        base_commit,
        "--to",
        head_commit,
        "--format",
        output_format,
        "--audience",
        "agent",
        "--concurrency",
        str(concurrency),
        "--timeout",
        str(timeout_minutes),
        "--max-tools",
        str(max_tools),
    ]
    if preview:
        command.append("--preview")
    process_timeout = timeout_minutes * 60 + 300
    return run_command(command, cwd=repo_path, timeout_seconds=process_timeout)


def save_result(
    results_dir: Path,
    instance: ReviewInstance,
    review_process: subprocess.CompletedProcess,
    output_format: str,
    started_at: str,
    duration_seconds: float,
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.result_path(results_dir, instance.instance_id)

    review_output: Any = review_process.stdout
    if output_format == "json" and review_output:
        try:
            review_output = json.loads(review_output)
        except json.JSONDecodeError:
            # ocr 在 JSON 阶段前报错时可能输出非 JSON，保留原始文本
            pass

    payload = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "head_commit": instance.head_commit,
        "reviewer": "ocr",
        "started_at": started_at,
        "duration_seconds": round(duration_seconds, 2),
        "ocr_exit_code": review_process.returncode,
        "ocr_format": output_format,
        "review": review_output,
        "stderr": review_process.stderr,
    }
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return out_path


def review_instance(
    instance: ReviewInstance,
    repo_dir: Path,
    results_dir: Path,
    output_format: str = "json",
    concurrency: int = 8,
    timeout_minutes: int = 30,
    preview: bool = False,
    ocr_command: str = "ocr",
    max_tools: int = 30,
) -> Dict[str, Any]:
    """对单条样本执行 OCR 评审，返回状态摘要。"""
    log(f"=== [ocr] Processing {instance.instance_id} ===")

    repo_path = prepare_repo(
        repo_dir=repo_dir,
        clone_url=instance.resolved_clone_url,
        repo_full_name=instance.repo,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    review_process = run_ocr_review(
        repo_path=repo_path,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
        output_format=output_format,
        concurrency=concurrency,
        timeout_minutes=timeout_minutes,
        preview=preview,
        ocr_command=ocr_command,
        max_tools=max_tools,
    )
    duration_seconds = time.monotonic() - start_time

    out_path = save_result(
        results_dir=results_dir,
        instance=instance,
        review_process=review_process,
        output_format=output_format,
        started_at=started_at,
        duration_seconds=duration_seconds,
    )

    # 丢弃 ocr 在仓库里留下的临时文件，保持工作区干净
    clean_worktree(repo_path)

    status = "ok" if review_process.returncode == 0 else "failed"
    log(f"--- [ocr] {instance.instance_id} {status} (exit={review_process.returncode}) -> {out_path}")
    return {
        "instance_id": instance.instance_id,
        "status": status,
        "exit_code": review_process.returncode,
        "result_path": str(out_path),
    }
