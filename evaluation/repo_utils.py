"""Git repository operations and lazy repository preparation.

The normal review flow only needs one entry point: ``prepare_repo``.  It
reuses an existing local repository, then a cached snapshot archive, and
finally falls back to Hugging Face or GitHub.  Dataset-level preparation is
deliberately not part of this module.

New code comments are in English.  Existing helpers keep their original
behaviour so the legacy ``--repo-source git`` path remains compatible.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

import config

ARCHIVE_SUFFIX = ".tar.gz"
SHA256SUMS_NAME = "SHA256SUMS"
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_ARCHIVE_MEMBERS = 2_000_000
GIT_TIMEOUT_SECONDS = 1800
CLONE_TIMEOUT_SECONDS = 3600


class RepoError(Exception):
    """单条样本在 git 阶段出现可恢复错误时抛出。"""


class NetworkError(RepoError):
    """A network failure with the exact URL that was attempted."""

    def __init__(self, url: str, detail: str):
        self.url = url
        self.detail = detail
        super().__init__(f"{url}: {detail}")


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def run_command(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    capture_output: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess and return its result without raising on non-zero exit.

    A missing executable is returned as exit code 127 so callers can render a
    structured preflight error instead of a traceback.
    """
    log(f"$ {' '.join(command)}" + (f"  (cwd={cwd})" if cwd else ""))
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            timeout=timeout_seconds,
            capture_output=capture_output,
            text=True,
            check=False,
            env=merged_env,
        )
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def local_repo_path(repo_dir: Path, repo_full_name: str) -> Path:
    """把 'owner/name' 映射为稳定的本地目录 repo/owner__name。"""
    safe_name = repo_full_name.replace("/", "__")
    return Path(repo_dir) / safe_name


def _git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if extra:
        env.update(extra)
    return env


def _ensure_ca_bundle() -> None:
    """Use the system CA bundle when the venv's default bundle is missing."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if Path(candidate).is_file():
            os.environ["SSL_CERT_FILE"] = candidate
            os.environ.setdefault("REQUESTS_CA_BUNDLE", candidate)
            return


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hf_endpoint() -> str:
    endpoint = os.environ.get(config.HF_ENDPOINT_VAR, config.DEFAULT_HF_ENDPOINT)
    endpoint = endpoint.strip().rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    is_local_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if parsed.scheme != "https" and not is_local_http:
        raise RepoError(
            f"invalid {config.HF_ENDPOINT_VAR}: only https:// is allowed (got {endpoint})"
        )
    return endpoint


def _snapshot_file_url(filename: str) -> str:
    quoted = urllib.parse.quote(filename, safe="/")
    return (
        f"{_hf_endpoint()}/datasets/{config.SNAPSHOT_REPO_ID}"
        f"/resolve/{config.SNAPSHOT_REVISION}/{quoted}"
    )


def _http_download(url: str, destination: Path, timeout: int) -> None:
    _ensure_ca_bundle()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": "aacr-bench-evaluation/0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
        os.replace(partial, destination)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        detail = (
            f"HTTP {error.code} {error.reason}"
            if isinstance(error, urllib.error.HTTPError)
            else str(error)
        )
        raise NetworkError(url, detail) from error


def _hf_download(
    filename: str,
    destination: Path,
    timeout: int,
    force_download: bool = False,
) -> None:
    destination = Path(destination)
    url = _snapshot_file_url(filename)
    _ensure_ca_bundle()
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        _http_download(url, destination, timeout)
        return

    kwargs = {
        "repo_id": config.SNAPSHOT_REPO_ID,
        "filename": filename,
        "repo_type": "dataset",
        "revision": config.SNAPSHOT_REVISION,
        "local_dir": str(destination.parent.parent),
        "force_download": force_download,
    }
    parameters = inspect.signature(hf_hub_download).parameters
    if "etag_timeout" in parameters:
        kwargs["etag_timeout"] = timeout
    if "max_retries" in parameters:
        kwargs["max_retries"] = 0
    if "endpoint" in parameters:
        kwargs["endpoint"] = _hf_endpoint()
    try:
        downloaded = Path(hf_hub_download(**kwargs))
        if downloaded.resolve() != destination.resolve():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(downloaded, destination)
    except Exception as error:  # noqa: BLE001
        detail = f"{type(error).__name__}: {error}"
        cause = getattr(error, "__cause__", None)
        if cause is not None:
            detail = f"{detail} ({type(cause).__name__}: {cause})"
        try:
            _http_download(url, destination, timeout)
            return
        except Exception as fallback_error:  # noqa: BLE001
            detail = f"{detail}; resolve fallback failed: {fallback_error}"
        raise NetworkError(url, detail) from error


def parse_sha256sums_text(text: str) -> Dict[str, str]:
    checksums: Dict[str, str] = {}
    pattern = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        digest, raw_name = match.group(1).lower(), match.group(2).replace("\\", "/")
        name = raw_name[2:] if raw_name.startswith("./") else raw_name
        if name.startswith("repos/"):
            name = name[len("repos/"):]
        elif "/" in name:
            continue
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or not name.endswith(ARCHIVE_SUFFIX)
        ):
            continue
        checksums[name] = digest
    return checksums


def _parse_sha256sums(path: Path) -> Dict[str, str]:
    return parse_sha256sums_text(Path(path).read_text(encoding="utf-8"))


def _archive_name(repo: str) -> str:
    if repo.count("/") != 1 or any(part in {"", ".", ".."} for part in repo.split("/")):
        raise RepoError(f"invalid repository name: {repo}")
    return repo.replace("/", "__") + ARCHIVE_SUFFIX


def _repo_ok(repo_path: Path) -> bool:
    return (Path(repo_path) / ".git" / "HEAD").exists()


def _quarantine(repo_path: Path, repo_dir: Path) -> None:
    failed_dir = Path(repo_dir).resolve() / ".failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = failed_dir / f"{Path(repo_path).name}-{suffix}"
    if target.exists():
        target = failed_dir / f"{Path(repo_path).name}-{suffix}-{os.getpid()}"
    os.rename(repo_path, target)
    log(f"Quarantined incomplete repository: {repo_path} -> {target}")


def _validate_tar_members(members: List[tarfile.TarInfo]) -> None:
    if not members:
        raise RepoError("archive is empty")
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise RepoError(f"archive has too many members: {len(members)}")
    total_size = 0
    for member in members:
        name = member.name.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute():
            raise RepoError(f"absolute path in archive: {member.name}")
        parts = [part for part in path.parts if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            raise RepoError(f"unsafe path in archive: {member.name}")
        if member.issym() or member.islnk():
            link = member.linkname.replace("\\", "/")
            link_path = PurePosixPath(link)
            if link_path.is_absolute():
                raise RepoError(f"absolute link target in archive: {member.name}")
            base = PurePosixPath(*parts[:-1])
            combined = posixpath.normpath(str(base / link_path))
            if combined == ".." or combined.startswith("../"):
                raise RepoError(f"link escapes archive: {member.name} -> {member.linkname}")
        if member.isdev() or member.isfifo() or (hasattr(member, "issock") and member.issock()):
            raise RepoError(f"special file in archive: {member.name}")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise RepoError(f"unsupported archive member: {member.name}")
        if member.isfile():
            total_size += member.size
            if total_size > MAX_ARCHIVE_BYTES:
                raise RepoError("archive exceeds the uncompressed size limit")


def _extract_archive(archive_path: Path, repo_dir: Path, repo: str) -> Path:
    archive_path = Path(archive_path).resolve()
    repo_dir = Path(repo_dir).resolve()
    expected_name = repo.replace("/", "__")
    destination = repo_dir / expected_name
    if destination.exists():
        if _repo_ok(destination):
            return destination
        raise RepoError(f"destination exists but is not a git repository: {destination}")

    staging_root = repo_dir / ".extract"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{expected_name}-", dir=staging_root))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            _validate_tar_members(members)
            try:
                archive.extractall(path=staging, members=members, filter="data")
            except TypeError:
                archive.extractall(path=staging, members=members)
    except (tarfile.TarError, OSError) as error:
        raise RepoError(f"failed to extract {archive_path}: {error}") from error

    children = list(staging.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise RepoError(f"archive must contain exactly one top-level directory: {archive_path}")
    extracted = children[0]
    if not _repo_ok(extracted):
        raise RepoError(f"archive does not contain a git worktree: {archive_path}")
    if destination.exists():
        raise RepoError(f"destination appeared during extraction: {destination}")
    os.rename(extracted, destination)
    return destination


def _commit_present(repo_path: Path, commit: str) -> bool:
    result = run_command(
        ["git", "-C", str(repo_path), "cat-file", "-e", f"{commit}^{{commit}}"],
        env=_git_env({"GIT_NO_LAZY_FETCH": "1"}),
        timeout_seconds=120,
    )
    return result.returncode == 0


def _merge_base(repo_path: Path, base: str, target: str) -> str:
    result = run_command(
        ["git", "-C", str(repo_path), "merge-base", base, target],
        env=_git_env({"GIT_NO_LAZY_FETCH": "1"}),
        timeout_seconds=300,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RepoError(f"git merge-base failed for {base}...{target}: {result.stderr.strip()}")
    return result.stdout.strip()


def _materialize(repo_path: Path, base: str, target: str) -> None:
    """Checkout base/merge-base/target, then verify without lazy fetching."""
    repo_path = Path(repo_path).resolve()
    if not _commit_present(repo_path, base):
        raise RepoError(f"base commit is missing: {base}")
    if not _commit_present(repo_path, target):
        raise RepoError(f"target commit is missing: {target}")
    merge_base = _merge_base(repo_path, base, target)

    for commit in dict.fromkeys((base, merge_base, target)):
        checkout_commit(repo_path, commit, allow_fetch=False)

    no_lazy = {"GIT_NO_LAZY_FETCH": "1"}
    diff = run_command(
        ["git", "-C", str(repo_path), "diff", "--quiet", f"{base}...{target}"],
        env=_git_env(no_lazy),
        timeout_seconds=600,
    )
    if diff.returncode not in (0, 1):
        raise RepoError(f"git diff failed for {base}...{target}: {diff.stderr.strip()}")


class RepoPreparer:
    """Lazy, per-repository preparation with run-level network caches."""

    def __init__(
        self,
        repo_dir: Path,
        archive_dir: Path,
        dataset_path: Optional[Path] = None,
    ):
        self.repo_dir = Path(repo_dir).resolve()
        self.archive_dir = Path(archive_dir).resolve()
        self.dataset_path = Path(dataset_path) if dataset_path else None
        self.network_timeout_seconds = config.network_timeout_seconds()
        self._hf_checked = False
        self._hf_ok = False
        self._hf_lock = threading.Lock()
        self._checksums: Optional[Dict[str, str]] = None
        self._github_checked = False
        self._github_ok = False
        self._github_lock = threading.Lock()
        self._github_error: Optional[NetworkError] = None

    def _local_checksum(self, archive_name: str) -> Optional[str]:
        checksum_path = self.archive_dir / "repos" / SHA256SUMS_NAME
        if not checksum_path.exists():
            return None
        try:
            return _parse_sha256sums(checksum_path).get(archive_name)
        except OSError:
            return None

    def _local_archive(self, repo: str) -> Optional[Path]:
        archive = self.archive_dir / "repos" / _archive_name(repo)
        if not archive.exists():
            return None
        expected = self._local_checksum(archive.name)
        if expected is not None and _sha256_file(archive) != expected:
            log(f"Cached archive checksum mismatch, ignoring: {archive}")
            return None
        return archive

    def _hf_ready(self) -> bool:
        with self._hf_lock:
            if self._hf_checked:
                return self._hf_ok
            self._hf_checked = True
            checksum_path = self.archive_dir / "repos" / SHA256SUMS_NAME
            try:
                _hf_download(
                    f"repos/{SHA256SUMS_NAME}",
                    checksum_path,
                    timeout=self.network_timeout_seconds,
                    force_download=True,
                )
                self._checksums = _parse_sha256sums(checksum_path)
                self._hf_ok = True
            except NetworkError as error:
                log(f"[network] HF unavailable: {error.url}: {error.detail}")
                self._hf_ok = False
            return self._hf_ok

    def _download_archive(self, repo: str) -> Optional[Path]:
        archive_name = _archive_name(repo)
        expected = (self._checksums or {}).get(archive_name)
        if expected is None:
            log(f"Snapshot does not cover {repo} ({archive_name})")
            return None
        destination = self.archive_dir / "repos" / archive_name
        if destination.exists() and _sha256_file(destination) == expected:
            return destination
        last_error = ""
        for attempt in range(2):
            try:
                _hf_download(
                    f"repos/{archive_name}",
                    destination,
                    timeout=self.network_timeout_seconds,
                    force_download=(attempt > 0 or destination.exists()),
                )
            except NetworkError as error:
                last_error = str(error)
                continue
            actual = _sha256_file(destination)
            if actual == expected:
                return destination
            last_error = f"checksum mismatch: {actual} != {expected}"
        log(f"Snapshot download failed for {repo}: {last_error}")
        return None

    def _github_ready(self) -> bool:
        with self._github_lock:
            if self._github_checked:
                return self._github_ok
            self._github_checked = True
            if self.dataset_path is None:
                self._github_ok = True
                return True
            try:
                _github_preflight(
                    self.dataset_path, timeout=self.network_timeout_seconds
                )
                self._github_ok = True
            except NetworkError as error:
                self._github_error = error
                self._github_ok = False
                log(f"[network] GitHub unavailable: {error.url}: {error.detail}")
            return self._github_ok

    def _partial_clone(
        self,
        repo: str,
        clone_url: str,
        base: str,
        target: str,
    ) -> Path:
        destination = local_repo_path(self.repo_dir, repo).resolve()
        partial_root = self.repo_dir / ".partial"
        partial_root.mkdir(parents=True, exist_ok=True)
        modes = (
            ("tree0", ["--filter=tree:0"]),
            ("blobless", ["--filter=blob:none"]),
            ("full", []),
        )
        temp_repo: Optional[Path] = None
        last_error = ""
        for mode, extra in modes:
            candidate = partial_root / (
                f"{repo.replace('/', '__')}-{mode}-{os.getpid()}-{int(time.time())}"
            )
            result = run_command(
                ["git", "clone", "--no-checkout", "--no-tags", *extra, clone_url, str(candidate)],
                env=_git_env(),
                timeout_seconds=CLONE_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                temp_repo = candidate
                break
            last_error = result.stderr.strip()
        if temp_repo is None:
            raise RepoError(f"git clone failed for {repo}: {last_error}")

        for commit in dict.fromkeys((base, target)):
            if not _commit_present(temp_repo, commit):
                fetched = run_command(
                    ["git", "-C", str(temp_repo), "fetch", "origin", commit],
                    env=_git_env(),
                    timeout_seconds=GIT_TIMEOUT_SECONDS,
                )
                if fetched.returncode != 0:
                    run_command(
                        ["git", "-C", str(temp_repo), "fetch", "--tags", "origin"],
                        env=_git_env(),
                        timeout_seconds=GIT_TIMEOUT_SECONDS,
                    )
                if not _commit_present(temp_repo, commit):
                    raise RepoError(f"commit {commit} is not available from GitHub")
        _materialize(temp_repo, base, target)
        if destination.exists():
            raise RepoError(f"destination appeared during partial clone: {destination}")
        os.rename(temp_repo, destination)
        return destination

    def prepare(
        self,
        repo: str,
        clone_url: str,
        base: str,
        target: str,
    ) -> Path:
        """Prepare one repository for one instance, reusing every cache level."""
        repo_path = local_repo_path(self.repo_dir, repo).resolve()
        if _repo_ok(repo_path):
            try:
                _materialize(repo_path, base, target)
                return repo_path
            except RepoError as error:
                log(f"Cached repository is unusable ({error}); quarantining")
                _quarantine(repo_path, self.repo_dir)

        archive = self._local_archive(repo)
        if archive is not None:
            try:
                _extract_archive(archive, self.repo_dir, repo)
                _materialize(repo_path, base, target)
                return repo_path
            except RepoError as error:
                log(f"Cached snapshot is unusable ({error}); trying next source")
                if _repo_ok(repo_path):
                    _quarantine(repo_path, self.repo_dir)

        if self._hf_ready():
            archive = self._download_archive(repo)
            if archive is not None:
                try:
                    _extract_archive(archive, self.repo_dir, repo)
                    _materialize(repo_path, base, target)
                    return repo_path
                except RepoError as error:
                    log(f"Snapshot repository is unusable ({error}); trying GitHub")
                    if _repo_ok(repo_path):
                        _quarantine(repo_path, self.repo_dir)

        if not self._github_ready():
            if self._github_error is not None:
                raise RepoError(
                    f"GitHub unavailable: {self._github_error.url}: "
                    f"{self._github_error.detail}"
                )
            raise RepoError(f"GitHub unavailable for {repo}")
        self._partial_clone(repo, clone_url, base, target)
        return repo_path


def _github_preflight(dataset_path: Path, timeout: Optional[int] = None) -> str:
    _ensure_ca_bundle()
    if timeout is None:
        timeout = config.network_timeout_seconds()
    version = config.benchmark_version_from_dataset(Path(dataset_path))
    meta_path = config.benchmark_meta_path(version)
    if not meta_path.exists():
        raise NetworkError(str(meta_path), "meta file does not exist")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        raise NetworkError(str(meta_path), f"invalid meta JSON: {error}") from error
    url = meta.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise NetworkError(str(meta_path), "meta file has no valid url")
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "aacr-bench-evaluation/0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return url
    except urllib.error.HTTPError as error:
        if error.code not in {403, 405}:
            raise NetworkError(url, f"HTTP {error.code} {error.reason}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise NetworkError(url, str(error)) from error
    request = urllib.request.Request(
        url,
        headers={"Range": "bytes=0-0", "User-Agent": "aacr-bench-evaluation/0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return url
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        detail = (
            f"HTTP {error.code} {error.reason}"
            if isinstance(error, urllib.error.HTTPError)
            else str(error)
        )
        raise NetworkError(url, detail) from error


_ACTIVE_PREPARER: Optional[RepoPreparer] = None


def set_active_repo_preparer(preparer: Optional[RepoPreparer]) -> None:
    global _ACTIVE_PREPARER
    _ACTIVE_PREPARER = preparer


def clear_active_repo_preparer() -> None:
    global _ACTIVE_PREPARER
    _ACTIVE_PREPARER = None


def ensure_commit_present(repo_path: Path, commit: str, allow_fetch: bool = True) -> None:
    """Ensure a commit exists locally.

    Snapshot/partial preparation has already materialized the required
    commits.  allow_fetch=False turns a missing object into a hard error.
    """
    check = run_command(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_path
    )
    if check.returncode == 0:
        return
    if not allow_fetch:
        raise RepoError(f"commit {commit} is missing from prepared repository {repo_path}")
    log(f"Commit {commit[:12]} not found locally, fetching directly...")
    fetch = run_command(
        ["git", "fetch", "origin", commit], cwd=repo_path, timeout_seconds=1800
    )
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


def checkout_commit(repo_path: Path, commit: str, allow_fetch: bool = True) -> None:
    """Checkout a commit after cleaning the worktree."""
    ensure_commit_present(repo_path, commit, allow_fetch=allow_fetch)
    clean_worktree(repo_path)
    result = run_command(["git", "checkout", "-f", commit], cwd=repo_path)
    if result.returncode != 0:
        expected = run_command(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=repo_path
        )
        actual = run_command(["git", "rev-parse", "HEAD"], cwd=repo_path)
        if (
            expected.returncode == 0
            and actual.returncode == 0
            and expected.stdout.strip() == actual.stdout.strip()
        ):
            log(
                "git checkout warning (HEAD is correct, hook failed): "
                f"{result.stderr.strip()}"
            )
            return
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
    """Prepare one instance's repository and return an absolute path.

    Pipeline runs install one active RepoPreparer for the whole review.
    Standalone callers get an equivalent auto-mode preparer.
    """
    preparer = _ACTIVE_PREPARER
    if preparer is None:
        preparer = RepoPreparer(
            repo_dir=repo_dir,
            archive_dir=config.default_snapshot_archive_dir(repo_dir),
        )
    return preparer.prepare(
        repo=repo_full_name,
        clone_url=clone_url,
        base=base_commit,
        target=head_commit,
    )
