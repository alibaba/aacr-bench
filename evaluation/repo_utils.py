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


def run_command(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    """运行子进程，不在非零退出码时抛异常（由调用方判断 returncode）。"""
    log(f"$ {' '.join(command)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        timeout=timeout_seconds,
        capture_output=capture_output,
        text=True,
        check=False,
    )


def local_repo_path(repo_dir: Path, repo_full_name: str) -> Path:
    """把 'owner/name' 映射为稳定的本地目录 repo/owner__name。"""
    safe_name = repo_full_name.replace("/", "__")
    return Path(repo_dir) / safe_name


def clone_or_update_repo(clone_url: str, target_dir: Path) -> None:
    """仓库不存在则 clone；已存在则 fetch 以确保后续 commit 可用。"""
    target_dir = Path(target_dir)
    if target_dir.exists() and (target_dir / ".git").exists():
        log(f"Repo already cloned, fetching latest: {target_dir}")
        result = run_command(
            ["git", "fetch", "--all", "--tags"], cwd=target_dir, timeout_seconds=1800
        )
        if result.returncode != 0:
            log(f"git fetch warning (continuing): {result.stderr.strip()}")
        return

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {clone_url} -> {target_dir}")
    result = run_command(["git", "clone", clone_url, str(target_dir)], timeout_seconds=3600)
    if result.returncode != 0:
        raise RepoError(f"git clone failed: {result.stderr.strip()}")


def ensure_commit_present(repo_path: Path, commit: str) -> None:
    """确保 commit 在本地存在，必要时直接 fetch 该 commit。"""
    check = run_command(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_path)
    if check.returncode == 0:
        return
    log(f"Commit {commit[:12]} not found locally, fetching directly...")
    fetch = run_command(["git", "fetch", "origin", commit], cwd=repo_path, timeout_seconds=1800)
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
