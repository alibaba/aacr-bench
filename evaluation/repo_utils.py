"""Git 仓库公共操作：clone / fetch / checkout / 清理工作区。

从 eval/run_review.py 与 eval/run_review_claude.py 中抽取的共用逻辑，
供 OCR / Claude 两个评审器复用，避免重复实现。
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional


class RepoError(Exception):
    """单条样本在 git 阶段出现可恢复错误时抛出。"""


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def _kill_tree(pid: int) -> None:
    """杀掉整棵进程树（Windows 下 Popen.kill 只杀直接子进程，cmd/node 孙进程会残留）。"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, check=False,
    )


def run_command(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    capture_output: bool = True,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """运行子进程，不在非零退出码时抛异常（由调用方判断 returncode）。

    用**临时文件**而不是 PIPE 捕获 stdout/stderr（否则像 ocr.cmd 这类内部再起
    worker 的进程，主进程退出后残留 worker 仍握有管道 fd，`communicate()` 会因
    管道永远不到 EOF 而挂死）。文件重定向在子进程结束后即可读到当前内容，不依赖
    管道关闭。同时按 UTF-8 读写，规避 Windows GBK 解码崩溃。

    超时处理：不用 `subprocess.run(timeout=...)`——它在 Windows 上超时后只 kill
    直接子进程再无条件 `communicate()`，孙进程持有重定向句柄时会让 run() 永久挂死
    （实测 ocr/claude 大样本触发）。改为 Popen + wait(timeout)，超时后
    `taskkill /T /F` 杀整棵树并返回 rc=-9 的结果，绝不抛 TimeoutExpired。
    """
    log(f"$ {' '.join(command)}" + (f"  (cwd={cwd})" if cwd else ""))
    if not capture_output:
        proc = subprocess.Popen(
            command, cwd=str(cwd) if cwd else None, env=env,
        )
        try:
            proc.wait(timeout=timeout_seconds)
            return subprocess.CompletedProcess(command, proc.returncode)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return subprocess.CompletedProcess(
                command, proc.returncode if proc.returncode is not None else -9,
            )
    import tempfile

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out, \
         tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=out,
            stderr=err,
            env=env,
        )
        try:
            proc.wait(timeout=timeout_seconds)
            returncode = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            returncode = proc.returncode if proc.returncode is not None else -9
            timed_out = True
        out.seek(0)
        err.seek(0)
        stdout_text = out.read()
        stderr_text = err.read()
        if timed_out:
            stderr_text = (stderr_text or "") + f"\n[run_command] killed process tree after {timeout_seconds}s"
        return subprocess.CompletedProcess(
            command, returncode, stdout=stdout_text, stderr=stderr_text,
        )



def local_repo_path(repo_dir: Path, repo_full_name: str) -> Path:
    """把 'owner/name' 映射为稳定的本地目录 repo/owner__name。"""
    safe_name = repo_full_name.replace("/", "__")
    return Path(repo_dir) / safe_name


def _git_retry(
    command: List[str],
    *,
    cwd: Optional[Path] = None,
    timeout_seconds: int = 1800,
    attempts: int = 4,
) -> subprocess.CompletedProcess:
    """对 git 网络命令做瞬态失败重试（Windows Schannel 在并发下偶发
    `failed to receive handshake, SSL/TLS connection failed`，单次重跑通常即成功）。
    非零退出码按 1s/2s/4s 退避重试，最后一次的结果原样返回。"""
    import time

    last = run_command(command, cwd=cwd, timeout_seconds=timeout_seconds)
    for attempt in range(1, attempts):
        if last.returncode == 0:
            break
        log(
            f"git {' '.join(command[:2])} transient failure (retry {attempt}/{attempts - 1}): "
            f"{last.stderr.strip()[:160]}"
        )
        time.sleep(attempt)
        last = run_command(command, cwd=cwd, timeout_seconds=timeout_seconds)
    return last


def clone_or_update_repo(clone_url: str, target_dir: Path) -> None:
    """仓库不存在则 clone；已存在则 fetch 以确保后续 commit 可用。"""
    target_dir = Path(target_dir)
    if target_dir.exists() and (target_dir / ".git").exists():
        log(f"Repo already cloned, fetching latest: {target_dir}")
        result = _git_retry(
            ["git", "fetch", "--all", "--tags"], cwd=target_dir, timeout_seconds=1800
        )
        if result.returncode != 0:
            log(f"git fetch warning (continuing): {result.stderr.strip()}")
        return

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {clone_url} -> {target_dir}")
    result = _git_retry(
        ["git", "clone", clone_url, str(target_dir)],
        timeout_seconds=3600,
        attempts=5,
    )
    if result.returncode != 0:
        raise RepoError(
            f"git clone failed after retries: {result.stderr.strip()} "
            f"(clone_url={clone_url})"
        )


def ensure_commit_present(repo_path: Path, commit: str) -> None:
    """确保 commit 在本地存在，必要时直接 fetch 该 commit。"""
    check = run_command(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_path)
    if check.returncode == 0:
        return
    log(f"Commit {commit[:12]} not found locally, fetching directly...")
    fetch = _git_retry(["git", "fetch", "origin", commit], cwd=repo_path, timeout_seconds=1800)
    if fetch.returncode != 0:
        raise RepoError(f"commit {commit} unavailable after fetch: {fetch.stderr.strip()}")


def clean_worktree(repo_path: Path) -> None:
    """把工作区恢复干净状态，丢弃所有本地改动与未跟踪文件。

    评审器（尤其 Claude Code 的 acceptEdits）可能在仓库里留下临时文件或改动
    已跟踪文件。`git checkout -f` 只覆盖已跟踪文件、会遗留未跟踪文件，长期累积
    会与后续 checkout 冲突或污染下一次评审。评审前后都执行此清理：

      git reset --hard   -> 还原已跟踪文件的修改
      git clean -fdx      -> 删除未跟踪文件/目录（含被 ignore 的）
    """
    repo_path = Path(repo_path)
    if not (repo_path / ".git").exists():
        return
    reset = run_command(["git", "reset", "--hard"], cwd=repo_path, timeout_seconds=300)
    if reset.returncode != 0:
        log(f"git reset --hard warning (continuing): {reset.stderr.strip()}")
    clean = run_command(["git", "clean", "-fdx"], cwd=repo_path, timeout_seconds=300)
    if clean.returncode != 0:
        log(f"git clean -fdx warning (continuing): {clean.stderr.strip()}")


def checkout_commit(repo_path: Path, commit: str) -> None:
    """切换到指定 commit；切换前先清理工作区避免残留改动阻塞 checkout。"""
    ensure_commit_present(repo_path, commit)
    clean_worktree(repo_path)
    result = run_command(["git", "checkout", "-f", commit], cwd=repo_path)
    if result.returncode != 0:
        raise RepoError(f"git checkout {commit} failed: {result.stderr.strip()}")


def diff_stat(repo_path: Path, base_commit: str, head_commit: str) -> str:
    """轻量 diff 摘要，仅用于日志（评审器自行探索仓库）。"""
    result = run_command(
        ["git", "diff", "--stat", base_commit, head_commit],
        cwd=repo_path,
        timeout_seconds=120,
    )
    if result.returncode != 0:
        return "(diff --stat unavailable)"
    lines = result.stdout.strip().splitlines()
    return lines[-1].strip() if lines else "(no changes)"


def prepare_repo(
    repo_dir: Path,
    clone_url: str,
    repo_full_name: str,
    base_commit: str,
    head_commit: str,
) -> Path:
    """完成单条样本评审前的仓库准备：clone -> 确保 commit -> checkout head。

    返回本地仓库路径。
    """
    repo_path = local_repo_path(repo_dir, repo_full_name)
    clone_or_update_repo(clone_url, repo_path)
    ensure_commit_present(repo_path, base_commit)
    checkout_commit(repo_path, head_commit)
    return repo_path
