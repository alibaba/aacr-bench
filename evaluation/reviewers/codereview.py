"""codereview-ai 评审器：外壳调用 codereview-ai 的无头 CLI（OCR 式契约）。

对每条样本：`prepare_repo` 拿到 aacr 已 checkout 的本地工作区 → 跑
`CODEREVIEW_COMMAND review --repo <ws> --base <base> --head <head>` → 把 CLI stdout 的
findings JSON 存进统一结果文件 `<safe_id>.json` 的 `review` 字段 → `clean_worktree`。

- LLM/平台配置由 **codereview-ai 自身**读取（`CR_LLM_MODEL` / `CR_LLM_API_KEY` /
  `CR_LLM_BASE_URL`），经父进程环境变量继承传递，本模块不重复解析。
- 调用命令由 `CODEREVIEW_COMMAND` 指定，例如
  `uv --directory /path/to/codereview-ai run python -m codereview_ai.bench_review`。
- Windows 下必须显式 `encoding="utf-8"` 读子进程 stdout（默认 ANSI 代码页会打乱中文正文，
  导致 JSON 解析失败）。

CLI stdout 契约（codereview-ai 侧，成功即 `status:"ok"`）：
    {"findings":[{file,content,line,old_line,side,category,severity,source}],
     "token_usage":{input_tokens,output_tokens}, "status":"ok", "error":""}
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import config
from repo_utils import clean_worktree, log, prepare_repo, run_command
from schema import ReviewInstance


def resolve_command(preview: bool = False) -> str:
    """返回 CODEREVIEW_COMMAND；缺省且非 preview 时 SystemExit 给指引。"""
    command = (os.environ.get(config.CODEREVIEW_COMMAND_VAR) or "").strip()
    if not command:
        if preview:
            return ""
        raise SystemExit(
            f"{config.CODEREVIEW_COMMAND_VAR} 未设置，codereview 评审无法运行。"
            f"（参考 evaluation/.env.example 配置，如 "
            f"uv --directory /path/to/codereview-ai run python -m codereview_ai.bench_review）"
        )
    return command


def ensure_codereview_installed(command: str) -> None:
    if not command:
        return
    result = run_command(shlex.split(command) + ["--version"])
    if result.returncode != 0:
        raise SystemExit(
            f"codereview-ai CLI 不可用（{config.CODEREVIEW_COMMAND_VAR}={command}）。"
            f"请确认 codereview-ai 已安装、命令可解析：{result.stderr.strip()[:200]}"
        )
    first_line = result.stdout.strip().splitlines()[0] if result.stdout else "ok"
    log(f"codereview available ({command}): {first_line}")


def check_env(preview: bool) -> None:
    if not (os.environ.get(config.CODEREVIEW_COMMAND_VAR) or "").strip() and not preview:
        log(
            f"WARNING: {config.CODEREVIEW_COMMAND_VAR} 未设置，codereview 评审将无法运行"
            f"（见 evaluation/.env.example）。"
        )


def default_cache_dir() -> Path:
    """agent bare-clone 缓存根；CODEREVIEW_CACHE_DIR 可覆盖，缺省 evaluation/.codereview_cache。"""
    env = (os.environ.get(config.CODEREVIEW_CACHE_DIR_VAR) or "").strip()
    return Path(env) if env else config.DEFAULT_CODEREVIEW_CACHE_DIR


def run_codereview_review(
    command: str,
    repo_path: Path,
    base_commit: str,
    head_commit: str,
    repo_full_name: str,
    clone_url: str,
    timeout_minutes: int,
    preview: bool,
) -> subprocess.CompletedProcess:
    cache_dir = default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    args = shlex.split(command) + [
        "review",
        "--repo", str(repo_path),
        "--base", base_commit,
        "--head", head_commit,
        "--repo-full-name", repo_full_name,
        "--cache-dir", str(cache_dir),
        "--format", "json",
    ]
    if clone_url:
        args += ["--clone-url", clone_url]
    if preview:
        args += ["--preview"]

    process_timeout = timeout_minutes * 60 + 300
    env = os.environ.copy()
    # 保证子进程 stdout 是 UTF-8（Windows 默认 ANSI 代码页会打乱中文正文）
    env["PYTHONIOENCODING"] = "utf-8"
    log(f"$ {' '.join(args)}  (cwd={repo_path})")
    log(f"cache-dir: {cache_dir}")
    # 复用 repo_utils.run_command：它用文件重定向捕获（非 PIPE）+ 超时 taskkill /T 杀整棵树，
    # 规避 Windows 下 uv->python 孙进程持有 PIPE 导致 communicate 永久阻塞的死等。
    return run_command(args, cwd=repo_path, timeout_seconds=process_timeout, env=env)


def save_result(
    results_dir: Path,
    instance: ReviewInstance,
    review_process: subprocess.CompletedProcess,
    started_at: str,
    duration_seconds: float,
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.result_path(results_dir, instance.instance_id)

    review_output: Any = review_process.stdout
    if review_output:
        try:
            review_output = json.loads(review_output)
        except json.JSONDecodeError:
            # CLI 在输出 JSON 前可能混入第三方横幅（如 litellm 异常提示）。契约是
            # 最后一行恒为一个完整 JSON 对象（成功或 error），倒序找第一行可解析的。
            for line in reversed(review_output.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        review_output = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            # 仍非 JSON 时保留原始文本供排查
            pass

    payload = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "head_commit": instance.head_commit,
        "reviewer": "codereview",
        "started_at": started_at,
        "duration_seconds": round(duration_seconds, 2),
        "codereview_exit_code": review_process.returncode,
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
    codereview_command: str = "",
    timeout_minutes: int = 30,
    preview: bool = False,
) -> Dict[str, Any]:
    """对单条样本执行 codereview-ai 评审，返回状态摘要。"""
    command = codereview_command or resolve_command(preview=preview)
    log(f"=== [codereview] Processing {instance.instance_id} ===")

    repo_path = prepare_repo(
        repo_dir=repo_dir,
        clone_url=instance.resolved_clone_url,
        repo_full_name=instance.repo,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()
    review_process = run_codereview_review(
        command=command,
        repo_path=repo_path,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
        repo_full_name=instance.repo,
        clone_url=instance.resolved_clone_url,
        timeout_minutes=timeout_minutes,
        preview=preview,
    )
    duration_seconds = time.monotonic() - start_time

    out_path = save_result(
        results_dir=results_dir,
        instance=instance,
        review_process=review_process,
        started_at=started_at,
        duration_seconds=duration_seconds,
    )

    # 丢弃评审过程中可能产生的临时文件，保持工作区干净
    clean_worktree(repo_path)

    status = "ok" if review_process.returncode == 0 else "failed"
    log(
        f"--- [codereview] {instance.instance_id} {status} "
        f"(exit={review_process.returncode}) -> {out_path}"
    )
    return {
        "instance_id": instance.instance_id,
        "status": status,
        "exit_code": review_process.returncode,
        "result_path": str(out_path),
    }