"""Repository snapshot acquisition and verification for AACR-Bench.

The evaluation pipeline supports two repository sources:

1. Hugging Face snapshot archives (preferred). The archives are shallow,
   non-bare repositories that keep the base/target/merge-base commits and the
   file contents needed by the evaluation pipeline.
2. GitHub partial clones (fallback). The clone starts with ``--filter=tree:0``
   and falls back to blobless/full clones when the server rejects the filter.

Only the Python standard library is used, so the module works in the same
environment as the rest of the evaluation framework.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import config
from repo_utils import log, set_blocked_repositories
from schema import ReviewInstance, load_instances

DEFAULT_SNAPSHOT_REPO = "Alibaba-Aone/aacr-bench-repos"
DEFAULT_SNAPSHOT_REVISION = "main"
DEFAULT_SNAPSHOT_ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")
SNAPSHOT_SUBDIR = "repos"
SHA256SUMS_NAME = "SHA256SUMS"
SNAPSHOT_MANIFEST_NAME = "snapshot_manifest.json"
PARTIAL_MANIFEST_NAME = "partial_clone_manifest.json"
USER_AGENT = "aacr-bench-evaluation/1.0"
MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACT_MEMBERS = 2_000_000
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+__[A-Za-z0-9._-]+$")
_ARCHIVE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+__[A-Za-z0-9._-]+\.tar\.gz$")


class SnapshotError(RuntimeError):
    """Snapshot acquisition or verification failed."""


class SnapshotCoverageError(SnapshotError):
    """The snapshot repository does not cover all repositories in the dataset."""


@dataclass
class SnapshotSource:
    endpoint: str
    repo_id: str
    revision: str
    revision_sha: Optional[str]
    checksums: Dict[str, str] = field(default_factory=dict)


@dataclass
class RepoVerification:
    repo: str
    path: str
    status: str = "ok"
    refs: int = 0
    commits: int = 0
    blobs: int = 0
    gitlinks: int = 0
    has_remote: bool = False
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "path": self.path,
            "status": self.status,
            "refs": self.refs,
            "commits": self.commits,
            "blobs": self.blobs,
            "gitlinks": self.gitlinks,
            "has_remote": self.has_remote,
            "errors": self.errors,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(
    command: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: int = 1800,
    text: bool = True,
    env_extra: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.update(GIT_ENV)
    if env_extra:
        environment.update(env_extra)
    log("$ " + " ".join(str(item) for item in command))
    run_kwargs: Dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "env": environment,
        "capture_output": True,
        "timeout": timeout,
    }
    if text:
        run_kwargs.update(text=True, encoding="utf-8", errors="replace")
    else:
        run_kwargs.update(text=False)
    try:
        return subprocess.run(list(command), **run_kwargs)
    except subprocess.TimeoutExpired as error:
        raise SnapshotError(f"timeout after {timeout}s: {' '.join(str(x) for x in command)}") from error


def _git(
    repo: Path,
    args: Sequence[str],
    timeout: int = 1800,
    text: bool = True,
    env_extra: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], timeout=timeout, text=text, env_extra=env_extra)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _endpoint_candidates(explicit: Optional[str]) -> List[str]:
    values: List[str] = []
    if explicit:
        values.extend(item.strip() for item in explicit.split(",") if item.strip())
    else:
        env_value = os.environ.get("HF_ENDPOINT", "").strip()
        if env_value:
            values.append(env_value)
        else:
            values.extend(DEFAULT_SNAPSHOT_ENDPOINTS)
    for value in values:
        if value.startswith("http://"):
            host = urllib.parse.urlparse(value).hostname
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise SnapshotError(
                    "plain http:// endpoints are only allowed for localhost tests: " + value
                )
    result: List[str] = []
    for value in values:
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        value = value.rstrip("/")
        if value not in result:
            result.append(value)
    return result


def _http_read(url: str, timeout: int = 60, attempts: int = 3) -> bytes:
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as error:  # noqa: BLE001 - retry network errors uniformly
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1.5 ** attempt)
    raise SnapshotError(f"HTTP request failed: {url}: {last_error}")


def _download_file(
    url: str,
    target: Path,
    expected_sha256: Optional[str] = None,
    timeout: int = 1800,
    attempts: int = 3,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part_path = target.with_name(f"{target.name}.part")
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                with part_path.open("wb") as handle:
                    shutil.copyfileobj(response, handle, length=1 << 20)
            if expected_sha256 is not None:
                actual = _sha256_file(part_path)
                if actual != expected_sha256:
                    raise SnapshotError(
                        f"sha256 mismatch for {target.name}: expected {expected_sha256}, got {actual}"
                    )
            os.replace(part_path, target)
            return
        except Exception as error:  # noqa: BLE001 - retry download errors uniformly
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise SnapshotError(f"download failed: {url}: {last_error}")


def parse_sha256sums_text(text: str) -> Dict[str, str]:
    """Parse a SHA256SUMS file into ``{archive-name: sha256}``.

    Both ``repos/foo.tar.gz`` and ``foo.tar.gz`` names are accepted; the
    returned keys are always archive basenames.
    """
    checksums: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        digest = digest.lower()
        name = name.strip().lstrip("*").strip().replace("\\", "/")
        if name.startswith("./"):
            name = name[2:]
        if name.startswith(f"{SNAPSHOT_SUBDIR}/"):
            name = name[len(SNAPSHOT_SUBDIR) + 1 :]
        if not _SHA256_RE.fullmatch(digest) or not _ARCHIVE_NAME_RE.fullmatch(name):
            continue
        checksums[name] = digest
    return checksums


def local_snapshot_source(
    archive_dir: Path,
    repo_id: str,
    revision: str,
) -> SnapshotSource:
    """Build a source from a local ``repos/SHA256SUMS`` without network access."""
    sums_path = Path(archive_dir) / SNAPSHOT_SUBDIR / SHA256SUMS_NAME
    if not sums_path.is_file():
        raise SnapshotError(
            f"local SHA256SUMS is missing: {sums_path}; "
            "run with --download first or place the snapshot archives manually"
        )
    checksums = parse_sha256sums_text(sums_path.read_text(encoding="utf-8"))
    if not checksums:
        raise SnapshotError(f"local SHA256SUMS is empty or invalid: {sums_path}")
    return SnapshotSource(
        endpoint="local",
        repo_id=repo_id,
        revision=revision,
        revision_sha=None,
        checksums=checksums,
    )


def resolve_snapshot_source(
    repo_id: str,
    revision: str,
    endpoint: Optional[str] = None,
    timeout: int = 60,
) -> SnapshotSource:
    """Resolve the snapshot repository and return its checksum manifest."""
    errors: List[str] = []
    for base in _endpoint_candidates(endpoint):
        try:
            sums_url = (
                f"{base}/datasets/{urllib.parse.quote(repo_id, safe='/')}"
                f"/resolve/{urllib.parse.quote(revision, safe='')}/{SNAPSHOT_SUBDIR}/{SHA256SUMS_NAME}"
            )
            payload = _http_read(sums_url, timeout=15, attempts=1)
            checksums = parse_sha256sums_text(payload.decode("utf-8", errors="replace"))
            if not checksums:
                raise SnapshotError(f"empty or invalid SHA256SUMS: {sums_url}")
            revision_sha: Optional[str] = None
            try:
                api_url = (
                    f"{base}/api/datasets/{urllib.parse.quote(repo_id, safe='/')}"
                    f"?revision={urllib.parse.quote(revision, safe='')}"
                )
                api_payload = json.loads(
                    _http_read(api_url, timeout=timeout, attempts=1).decode("utf-8")
                )
                revision_sha = api_payload.get("sha")
            except Exception:  # noqa: BLE001 - revision SHA is informational only
                revision_sha = None
            return SnapshotSource(
                endpoint=base,
                repo_id=repo_id,
                revision=revision,
                revision_sha=revision_sha,
                checksums=checksums,
            )
        except Exception as error:  # noqa: BLE001 - try the next endpoint
            errors.append(f"{base}: {error}")
    raise SnapshotError("could not resolve snapshot repository from any endpoint:\n  " + "\n  ".join(errors))


def _snapshot_url(source: SnapshotSource, name: str) -> str:
    return (
        f"{source.endpoint}/datasets/{urllib.parse.quote(source.repo_id, safe='/')}"
        f"/resolve/{urllib.parse.quote(source.revision, safe='')}"
        f"/{SNAPSHOT_SUBDIR}/{urllib.parse.quote(name, safe='')}"
    )


def verify_archive_set(
    archive_dir: Path,
    checksums: Dict[str, str],
    names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Verify downloaded archives against the checksum manifest."""
    repos_dir = Path(archive_dir) / SNAPSHOT_SUBDIR
    selected = list(names) if names is not None else sorted(checksums)
    missing: List[str] = []
    mismatched: List[Dict[str, str]] = []
    verified = 0
    for name in selected:
        path = repos_dir / name
        expected = checksums.get(name)
        if expected is None:
            missing.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        actual = _sha256_file(path)
        if actual != expected:
            mismatched.append({"name": name, "expected": expected, "actual": actual})
            continue
        verified += 1
    return {
        "expected": len(selected),
        "verified": verified,
        "missing": missing,
        "mismatched": mismatched,
        "status": "ok" if not missing and not mismatched else "failed",
    }


def download_snapshots(
    source: SnapshotSource,
    archive_dir: Path,
    names: Optional[Sequence[str]] = None,
    jobs: int = 4,
) -> Dict[str, Any]:
    """Download all selected archives, reusing files whose SHA256 matches."""
    repos_dir = Path(archive_dir) / SNAPSHOT_SUBDIR
    repos_dir.mkdir(parents=True, exist_ok=True)
    repos_dir = repos_dir.resolve()
    selected = list(names) if names is not None else sorted(source.checksums)

    sums_path = repos_dir / SHA256SUMS_NAME
    sums_text = "".join(
        f"{source.checksums[name]}  {name}\n" for name in sorted(source.checksums)
    )
    if not sums_path.exists() or sums_path.read_text(encoding="utf-8") != sums_text:
        temporary = sums_path.with_name(f"{sums_path.name}.tmp")
        temporary.write_text(sums_text, encoding="utf-8")
        os.replace(temporary, sums_path)

    cached: List[str] = []
    downloaded: List[str] = []
    errors: List[Dict[str, str]] = []

    def fetch(name: str) -> Tuple[str, str]:
        target = (repos_dir / name).resolve()
        if target.parent != repos_dir:
            raise SnapshotError(f"archive name escapes the archive directory: {name}")
        expected = source.checksums[name]
        if target.is_file() and _sha256_file(target) == expected:
            return name, "cached"
        _download_file(_snapshot_url(source, name), target, expected)
        return name, "downloaded"

    effective_jobs = max(1, min(int(jobs or 1), len(selected) or 1))
    with ThreadPoolExecutor(max_workers=effective_jobs) as executor:
        futures = {executor.submit(fetch, name): name for name in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                resolved_name, status = future.result()
                if status == "cached":
                    cached.append(resolved_name)
                else:
                    downloaded.append(resolved_name)
                log(f"[snapshot] {status}: {resolved_name}")
            except Exception as error:  # noqa: BLE001 - collect per-file failures
                errors.append({"name": name, "error": str(error)})
                log(f"[snapshot] FAILED {name}: {error}")

    verification = verify_archive_set(archive_dir, source.checksums, selected)
    return {
        "endpoint": source.endpoint,
        "repo_id": source.repo_id,
        "revision": source.revision,
        "revision_sha": source.revision_sha,
        "expected": len(selected),
        "verified": verification["verified"],
        "cached": len(cached),
        "downloaded": len(downloaded),
        "errors": errors,
        "verification": verification,
        "status": "ok" if verification["status"] == "ok" and not errors else "failed",
    }


def _normalize_member_name(name: str) -> str:
    value = name.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _validate_member(
    member: tarfile.TarInfo,
    repo_name: str,
    symlink_paths: Set[str],
    total_bytes: int,
) -> Tuple[str, int, bool]:
    """Validate one archive member and return (normalized name, size, is_head)."""
    name = _normalize_member_name(member.name)
    if not name or name in (".", "./"):
        return name, total_bytes, False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise SnapshotError(f"unsafe archive path: {member.name}")
    if not path.parts or path.parts[0] != repo_name:
        raise SnapshotError(f"archive member outside {repo_name}/: {member.name}")

    # Reject members that would be written through an earlier symlink.
    for index in range(1, len(path.parts)):
        parent = "/".join(path.parts[:index])
        if parent in symlink_paths:
            raise SnapshotError(f"archive member traverses symlink {parent}: {member.name}")

    if member.issym():
        link = _normalize_member_name(member.linkname)
        if not link or PurePosixPath(link).is_absolute():
            raise SnapshotError(f"unsafe symlink target: {member.name} -> {member.linkname}")
        resolved = os.path.normpath(os.path.join(os.path.dirname(name), link)).replace("\\", "/")
        if not (resolved == repo_name or resolved.startswith(repo_name + "/")):
            raise SnapshotError(f"symlink escapes repository: {member.name} -> {member.linkname}")
        symlink_paths.add(name)
    elif member.islnk():
        link = _normalize_member_name(member.linkname)
        link = os.path.normpath(link).replace("\\", "/")
        if not link or PurePosixPath(link).is_absolute():
            raise SnapshotError(f"unsafe hardlink target: {member.name} -> {member.linkname}")
        if link == ".." or link.startswith("../") or "/../" in link:
            raise SnapshotError(f"hardlink escapes repository: {member.name} -> {member.linkname}")
        if not (link == repo_name or link.startswith(repo_name + "/")):
            raise SnapshotError(f"hardlink escapes repository: {member.name} -> {member.linkname}")

    if member.isdev() or member.isfifo():
        raise SnapshotError(f"unsupported special file in archive: {member.name}")

    total_bytes += int(member.size or 0)
    if total_bytes > MAX_EXTRACT_BYTES:
        raise SnapshotError(
            f"archive exceeds the {MAX_EXTRACT_BYTES} byte extraction limit: {member.name}"
        )
    return name, total_bytes, name == f"{repo_name}/.git/HEAD"


def _check_symlinks(root: Path) -> None:
    root = root.resolve()
    for current, directories, files in os.walk(root, followlinks=False):
        for name in list(directories) + list(files):
            path = Path(current) / name
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(root)
                except ValueError as error:
                    raise SnapshotError(f"symlink escapes repository: {path} -> {target}") from error


def extract_archive(
    archive_path: Path,
    repo_dir: Path,
    repo_name: str,
    expected_sha256: Optional[str] = None,
) -> str:
    """Safely extract one snapshot archive into ``repo_dir/<repo_name>``.

    When ``expected_sha256`` is provided, the hash is computed on the same open
    file descriptor that is handed to ``tarfile``, so verification and
    extraction cannot be separated by a concurrent replacement.
    """
    target = Path(repo_dir) / repo_name
    if target.exists():
        if (target / ".git").exists():
            return "exists"
        raise SnapshotError(
            f"target exists but is not a git repository: {target}; move it away before retrying"
        )

    staging_root = (
        Path(repo_dir)
        / ".extract"
        / f"{repo_name}-{os.getpid()}-{time.time_ns()}"
    )
    staging_root.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    member_count = 0
    saw_head = False
    symlink_paths: Set[str] = set()

    try:
        with archive_path.open("rb") as handle:
            if expected_sha256 is not None:
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
                actual = digest.hexdigest()
                if actual != expected_sha256:
                    raise SnapshotError(
                        f"sha256 mismatch for {archive_path.name}: "
                        f"expected {expected_sha256}, got {actual}"
                    )
                handle.seek(0)
            with tarfile.open(fileobj=handle, mode="r:gz") as archive:
                for member in archive:
                    member_count += 1
                    if member_count > MAX_EXTRACT_MEMBERS:
                        raise SnapshotError(
                            f"archive exceeds the {MAX_EXTRACT_MEMBERS} member limit: {archive_path}"
                        )
                    name, total_bytes, is_head = _validate_member(
                        member, repo_name, symlink_paths, total_bytes
                    )
                    if is_head:
                        saw_head = True
                    if not name or name in (".", "./"):
                        continue
                    extract_options: Dict[str, Any] = {"set_attrs": False}
                    if hasattr(tarfile, "fully_trusted_filter"):
                        # Members were validated above; keep the original symlinks
                        # and modes while suppressing Python 3.12+ deprecation noise.
                        extract_options["filter"] = "fully_trusted"
                    archive.extract(member, path=staging_root, **extract_options)
        if not saw_head:
            raise SnapshotError(f"archive does not contain {repo_name}/.git/HEAD: {archive_path}")
        extracted = staging_root / repo_name
        if not extracted.is_dir():
            raise SnapshotError(f"archive did not create {repo_name}/: {archive_path}")
        _check_symlinks(extracted)
        os.replace(extracted, target)
        return "extracted"
    except SnapshotError:
        raise
    except Exception as error:  # noqa: BLE001 - wrap archive errors with the file name
        raise SnapshotError(f"failed to extract {archive_path}: {error}") from error


def extract_snapshots(
    archive_dir: Path,
    repo_dir: Path,
    names: Sequence[str],
    jobs: int = 2,
    checksums: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Extract all selected snapshot archives."""
    repos_dir = Path(archive_dir) / SNAPSHOT_SUBDIR
    Path(repo_dir).mkdir(parents=True, exist_ok=True)
    results: Dict[str, str] = {}
    errors: List[Dict[str, str]] = []
    effective_jobs = max(1, min(int(jobs or 1), len(names) or 1))

    def extract_one(name: str) -> Tuple[str, str]:
        repo_name = name[: -len(".tar.gz")]
        if not _REPO_NAME_RE.fullmatch(repo_name):
            raise SnapshotError(f"unexpected archive name: {name}")
        expected = checksums.get(name) if checksums else None
        status = extract_archive(
            repos_dir / name, Path(repo_dir), repo_name, expected_sha256=expected
        )
        return repo_name, status

    with ThreadPoolExecutor(max_workers=effective_jobs) as executor:
        futures = {executor.submit(extract_one, name): name for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                repo_name, status = future.result()
                results[repo_name] = status
                log(f"[snapshot] extract {status}: {repo_name}")
            except Exception as error:  # noqa: BLE001 - collect per-archive failures
                errors.append({"name": name, "error": str(error)})
                log(f"[snapshot] EXTRACT FAILED {name}: {error}")

    return {
        "expected": len(names),
        "extracted": sum(1 for value in results.values() if value == "extracted"),
        "existing": sum(1 for value in results.values() if value == "exists"),
        "errors": errors,
        "status": "ok" if not errors else "failed",
    }


def _parse_ls_tree(output: bytes) -> Tuple[Set[str], int]:
    blobs: Set[str] = set()
    gitlinks = 0
    for entry in output.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, _path = entry.split(b"\t", 1)
            _mode, object_type, object_id = metadata.decode("ascii").split()
        except ValueError:
            continue
        if object_type == "blob":
            blobs.add(object_id)
        elif object_type == "commit":
            gitlinks += 1
    return blobs, gitlinks


def _check_blobs(repo: Path, blobs: Iterable[str], timeout: int = 1800) -> List[str]:
    """Verify that every blob id exists and is a blob, in bounded batches."""
    errors: List[str] = []
    unique = sorted(set(blobs))

    # Use a dedicated subprocess so stdin can carry the batch.
    for start in range(0, len(unique), 20000):
        chunk = unique[start : start + 20000]
        process = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check"],
            input="\n".join(chunk) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, **GIT_ENV, "GIT_NO_LAZY_FETCH": "1"},
        )
        if process.returncode != 0:
            errors.append(f"git cat-file --batch-check failed: {process.stderr.strip()[:300]}")
            break
        for line in process.stdout.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[1] != "blob":
                errors.append(f"missing or non-blob object: {line.strip()}")
                if len(errors) >= 10:
                    return errors
    return errors


def verify_commit_tree(
    repo: Path,
    commit: str,
    check_blobs: bool = True,
    timeout: int = 1800,
) -> Tuple[int, int, List[str]]:
    """Verify a commit's full tree; return (blob count, gitlink count, errors)."""
    errors: List[str] = []
    exists = _git(
        repo,
        ["cat-file", "-e", f"{commit}^{{commit}}"],
        timeout=120,
        env_extra={"GIT_NO_LAZY_FETCH": "1"},
    )
    if exists.returncode != 0:
        return 0, 0, [f"commit unavailable: {commit}"]

    listing = _git(repo, ["ls-tree", "-r", "-z", commit], timeout=timeout, text=False)
    if listing.returncode != 0:
        return 0, 0, [f"ls-tree failed for {commit}: {listing.stderr.decode('utf-8', 'replace')[:300]}"]
    blobs, gitlinks = _parse_ls_tree(listing.stdout or b"")
    if check_blobs:
        errors.extend(_check_blobs(repo, blobs, timeout=timeout))
    return len(blobs), gitlinks, errors


def verify_repository_refs(
    repo_path: Path,
    check_blobs: bool = True,
    timeout: int = 1800,
) -> RepoVerification:
    """Verify all ``refs/heads/{base,target,merge_base}/*`` in a snapshot."""
    repo_name = repo_path.name
    verification = RepoVerification(repo=repo_name, path=str(repo_path))
    if not (repo_path / ".git").exists():
        verification.status = "failed"
        verification.errors.append("missing .git directory")
        return verification

    inside = _git(repo_path, ["rev-parse", "--is-inside-work-tree"], timeout=60)
    bare = _git(repo_path, ["rev-parse", "--is-bare-repository"], timeout=60)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        verification.status = "failed"
        verification.errors.append("not a git working tree")
    if bare.returncode == 0 and bare.stdout.strip() == "true":
        verification.status = "failed"
        verification.errors.append("bare repository")

    refs = _git(
        repo_path,
        ["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"],
        timeout=120,
    )
    if refs.returncode != 0:
        verification.status = "failed"
        verification.errors.append(f"for-each-ref failed: {refs.stderr.strip()[:300]}")
        return verification

    commits: Set[str] = set()
    for line in refs.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        refname, object_id = fields
        if not any(refname.startswith(f"refs/heads/{prefix}/") for prefix in ("base", "target", "merge_base")):
            continue
        verification.refs += 1
        suffix = refname.rsplit("/", 1)[-1]
        if suffix and not object_id.startswith(suffix):
            verification.errors.append(
                f"ref suffix does not match object id: {refname} -> {object_id}"
            )
        commits.add(object_id)

    for commit in sorted(commits):
        blob_count, gitlinks, errors = verify_commit_tree(
            repo_path, commit, check_blobs=check_blobs, timeout=timeout
        )
        verification.blobs += blob_count
        verification.gitlinks += gitlinks
        verification.commits += 1
        verification.errors.extend(errors)

    remote = _git(repo_path, ["remote"], timeout=60)
    verification.has_remote = bool(remote.stdout.strip()) if remote.returncode == 0 else False
    if verification.refs == 0:
        verification.errors.append("no refs/heads/{base,target,merge_base}/* refs found")
    if verification.errors:
        verification.status = "failed"
    return verification


def _repo_archive_name(repo: str) -> str:
    return repo.replace("/", "__") + ".tar.gz"


def _comment_commit(instance: ReviewInstance, side: Optional[str]) -> str:
    return instance.base_commit if side == "left" else instance.head_commit


def verify_dataset_instances(
    instances: Sequence[ReviewInstance],
    repo_dir: Path,
    check_blobs: bool = True,
    timeout: int = 1800,
) -> Dict[str, Any]:
    """Verify that the extracted snapshots cover every dataset instance."""
    commit_cache: Dict[Tuple[str, str], List[str]] = {}
    comment_path_cache: Dict[Tuple[str, str], str] = {}
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    def commit_errors(repo_path: Path, commit: str) -> List[str]:
        key = (str(repo_path), commit)
        if key not in commit_cache:
            _blobs, _gitlinks, item_errors = verify_commit_tree(
                repo_path, commit, check_blobs=check_blobs, timeout=timeout
            )
            commit_cache[key] = item_errors
        return commit_cache[key]

    for instance in instances:
        repo_path = Path(repo_dir) / instance.repo.replace("/", "__")
        item: Dict[str, Any] = {
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
            "head_commit": instance.head_commit,
            "status": "ok",
            "errors": [],
        }
        if not (repo_path / ".git").exists():
            item["errors"].append(f"repository missing: {repo_path}")
        else:
            for label, commit in (("base", instance.base_commit), ("head", instance.head_commit)):
                for error in commit_errors(repo_path, commit):
                    item["errors"].append(f"{label}: {error}")
            merge_base = _git(
                repo_path,
                ["merge-base", instance.base_commit, instance.head_commit],
                timeout=300,
                env_extra={"GIT_NO_LAZY_FETCH": "1"},
            )
            if merge_base.returncode != 0 or not merge_base.stdout.strip():
                item["errors"].append(
                    f"merge-base failed: {merge_base.stderr.strip()[:300] or 'no common ancestor'}"
                )
            diff = _git(
                repo_path,
                ["diff", f"{instance.base_commit}...{instance.head_commit}", "--stat"],
                timeout=600,
                env_extra={"GIT_NO_LAZY_FETCH": "1"},
            )
            if diff.returncode != 0:
                item["errors"].append(f"diff base...head failed: {diff.stderr.strip()[:300]}")
            for comment in instance.reference_comments:
                commit = _comment_commit(instance, comment.side)
                path_key = (commit, comment.path)
                if path_key in comment_path_cache:
                    if comment_path_cache[path_key]:
                        item["errors"].append(comment_path_cache[path_key])
                    continue
                probe = _git(
                    repo_path,
                    ["cat-file", "-e", f"{commit}:{comment.path}"],
                    timeout=120,
                    env_extra={"GIT_NO_LAZY_FETCH": "1"},
                )
                if probe.returncode != 0:
                    message = f"reference path unavailable at {commit[:12]}: {comment.path}"
                    comment_path_cache[path_key] = message
                    item["errors"].append(message)
                else:
                    comment_path_cache[path_key] = ""
        if item["errors"]:
            item["status"] = "failed"
            errors.append(item)
        results.append(item)

    return {
        "instances": len(instances),
        "ok": sum(1 for item in results if item["status"] == "ok"),
        "failed": len(errors),
        "errors": errors[:50],
        "status": "ok" if not errors else "failed",
        "details": results,
    }


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ensure_snapshots(
    repo_dir: Path,
    archive_dir: Path,
    repo_id: str = DEFAULT_SNAPSHOT_REPO,
    revision: str = DEFAULT_SNAPSHOT_REVISION,
    endpoint: Optional[str] = None,
    jobs: int = 4,
    download: bool = True,
    extract: bool = True,
    required_repos: Optional[Iterable[str]] = None,
    instances: Optional[Sequence[ReviewInstance]] = None,
    scope: str = "all",
    check_blobs: bool = True,
    verify_only: bool = False,
) -> Dict[str, Any]:
    """Download, verify, extract, and validate the snapshot repository."""
    archive_dir = Path(archive_dir)
    repo_dir = Path(repo_dir)
    if verify_only or not download:
        source = local_snapshot_source(archive_dir, repo_id, revision)
    else:
        source = resolve_snapshot_source(repo_id, revision, endpoint=endpoint)

    required = sorted(set(required_repos or []))
    if required:
        available = {name[: -len(".tar.gz")] for name in source.checksums}
        missing = [repo for repo in required if repo.replace("/", "__") not in available]
        if missing:
            raise SnapshotCoverageError(
                "snapshot repository does not cover: " + ", ".join(missing)
            )

    names = sorted(source.checksums)
    if scope == "dataset":
        wanted = {_repo_archive_name(repo) for repo in required}
        names = [name for name in names if name in wanted]
    elif scope not in ("all", "dataset"):
        raise SnapshotError(f"unknown snapshot scope: {scope}")

    download_result: Dict[str, Any] = {
        "status": "skipped",
        "expected": len(names),
        "verified": 0,
        "missing": [],
        "mismatched": [],
    }
    if verify_only:
        if not (archive_dir / SNAPSHOT_SUBDIR / SHA256SUMS_NAME).is_file():
            raise SnapshotError(f"verify-only requested but SHA256SUMS is missing under {archive_dir}")
        download_result = verify_archive_set(archive_dir, source.checksums, names)
    elif download:
        download_result = download_snapshots(source, archive_dir, names=names, jobs=jobs)
        if download_result["status"] != "ok":
            raise SnapshotError(f"snapshot download failed: {download_result}")
    else:
        download_result = verify_archive_set(archive_dir, source.checksums, names)
        if download_result["status"] != "ok":
            raise SnapshotError(f"snapshot archive verification failed: {download_result}")

    extraction_result: Dict[str, Any] = {
        "status": "skipped",
        "expected": len(names),
        "extracted": 0,
        "existing": 0,
        "errors": [],
    }
    if verify_only:
        extraction_result["existing"] = sum(
            1 for name in names if (repo_dir / name[: -len(".tar.gz")]).exists()
        )
    elif extract:
        extraction_result = extract_snapshots(
            archive_dir, repo_dir, names, jobs=max(1, min(jobs, 2)), checksums=source.checksums
        )
        if extraction_result["status"] != "ok":
            raise SnapshotError(f"snapshot extraction failed: {extraction_result}")

    repo_checks: List[Dict[str, Any]] = []
    check_repositories = verify_only or extract
    if check_repositories:
        for name in names:
            repo_name = name[: -len(".tar.gz")]
            path = repo_dir / repo_name
            if not path.exists():
                repo_checks.append(
                    RepoVerification(
                        repo=repo_name, path=str(path), status="failed", errors=["repository not extracted"]
                    ).to_dict()
                )
                continue
            repo_checks.append(verify_repository_refs(path, check_blobs=check_blobs).to_dict())

    dataset_result: Dict[str, Any] = {"status": "skipped"}
    if instances is not None and check_repositories:
        dataset_result = verify_dataset_instances(
            instances, repo_dir, check_blobs=check_blobs
        )
    elif instances is not None:
        dataset_result = {"status": "skipped", "reason": "repository extraction disabled"}

    failed_repos = [item["repo"] for item in repo_checks if item["status"] != "ok"]
    status = "ok"
    if download_result.get("status") == "failed" or extraction_result.get("status") == "failed":
        status = "failed"
    if failed_repos or dataset_result.get("status") == "failed":
        status = "failed"

    payload = {
        "generated_at": _now_iso(),
        "source": {
            "endpoint": source.endpoint,
            "repo_id": source.repo_id,
            "revision": source.revision,
            "revision_sha": source.revision_sha,
        },
        "scope": scope,
        "archives": download_result,
        "extraction": extraction_result,
        "repositories": {
            "expected": len(names),
            "ok": sum(1 for item in repo_checks if item["status"] == "ok"),
            "failed": failed_repos,
            "details": repo_checks,
        },
        "dataset": dataset_result,
        "status": status,
    }
    manifest_name = SNAPSHOT_MANIFEST_NAME if scope == "all" else f"snapshot_manifest-{scope}.json"
    manifest_path = archive_dir / manifest_name
    _write_json_atomic(manifest_path, payload)
    payload["manifest_path"] = str(manifest_path)
    return payload


def _has_commit(repo: Path, commit: str, timeout: int = 120) -> bool:
    process = _git(
        repo,
        ["cat-file", "-e", f"{commit}^{{commit}}"],
        timeout=timeout,
        env_extra={"GIT_NO_LAZY_FETCH": "1"},
    )
    return process.returncode == 0


def _pr_fetch_ref(pr_url: Optional[str]) -> Optional[str]:
    """Return ``refs/pull/<n>/head`` for a GitHub PR URL."""
    if not pr_url:
        return None
    match = re.search(r"github\.com/[^/]+/[^/]+/pull/(\d+)", pr_url, re.IGNORECASE)
    if not match:
        return None
    return f"refs/pull/{match.group(1)}/head"


def _ensure_commit(repo: Path, commit: str, pr_url: Optional[str], timeout: int) -> None:
    if _has_commit(repo, commit):
        return
    attempts = [["fetch", "--no-tags", "origin", commit]]
    pr_ref = _pr_fetch_ref(pr_url)
    if pr_ref:
        attempts.append(["fetch", "--no-tags", "origin", pr_ref])
    attempts.append(["fetch", "--no-tags", "origin"])
    errors: List[str] = []
    for args in attempts:
        log(f"[partial] {repo.name}: git {' '.join(args)}")
        process = _git(repo, args, timeout=timeout)
        if process.returncode == 0 and _has_commit(repo, commit):
            return
        errors.append(f"{' '.join(args)}: {process.stderr.strip()[:300]}")
    raise SnapshotError(f"commit unavailable after fetch: {commit} | " + " | ".join(errors))


def _clone_modes(value: str) -> List[str]:
    modes = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in modes if item not in ("tree0", "blobless", "full")]
    if unknown or not modes:
        raise SnapshotError(f"invalid partial clone modes: {value}")
    return modes


def _clone_args(mode: str) -> List[str]:
    if mode == "tree0":
        return ["--filter=tree:0", "--no-checkout", "--no-tags"]
    if mode == "blobless":
        return ["--filter=blob:none", "--no-checkout", "--no-tags"]
    return ["--no-checkout", "--no-tags"]


def _clone_partial_repo(
    repo: str,
    repo_dir: Path,
    clone_url: str,
    modes: List[str],
    timeout: int,
) -> Tuple[Path, str]:
    destination = Path(repo_dir) / repo.replace("/", "__")
    if (destination / ".git").exists():
        remote = _git(destination, ["remote"], timeout=60)
        remotes = remote.stdout.split() if remote.returncode == 0 else []
        if "origin" not in remotes:
            added = _git(destination, ["remote", "add", "origin", clone_url], timeout=60)
            if added.returncode != 0:
                raise SnapshotError(
                    f"could not add origin to existing repository {destination}: "
                    f"{added.stderr.strip()[:300]}"
                )
        return destination, "reuse"
    if destination.exists():
        raise SnapshotError(
            f"target exists but is not a git repository: {destination}; move it away before retrying"
        )
    attempts_dir = Path(repo_dir) / ".clone-attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []
    for mode in modes:
        staging = attempts_dir / f"{repo.replace('/', '__')}-{mode}-{os.getpid()}-{time.time_ns()}"
        log(f"[partial] {repo}: clone mode {mode} -> {staging}")
        process = _run(["git", "clone", *_clone_args(mode), clone_url, str(staging)], timeout=timeout)
        if process.returncode == 0:
            os.replace(staging, destination)
            return destination, mode
        errors.append(f"{mode}: {process.stderr.strip()[:400]}")
    raise SnapshotError("all partial clone modes failed: " + " | ".join(errors))


def prepare_partial_clones(
    instances: Sequence[ReviewInstance],
    repo_dir: Path,
    modes: str = "tree0,blobless,full",
    timeout: int = 1800,
    jobs: int = 2,
) -> Dict[str, Any]:
    """Prepare repositories from public GitHub clones with endpoint commits."""
    mode_list = _clone_modes(modes)
    groups: Dict[str, List[ReviewInstance]] = {}
    for instance in instances:
        groups.setdefault(instance.repo, []).append(instance)

    results: Dict[str, Dict[str, Any]] = {}
    blocked: List[Dict[str, Any]] = []

    def prepare_one(repo: str, group: Sequence[ReviewInstance]) -> Dict[str, Any]:
        clone_urls = sorted({instance.resolved_clone_url for instance in group})
        if len(clone_urls) != 1:
            return {
                "repo": repo,
                "status": "failed",
                "clone_mode": None,
                "error": f"conflicting clone_url values: {clone_urls}",
                "instances": len(group),
            }
        try:
            repo_path, clone_mode = _clone_partial_repo(
                repo, Path(repo_dir), clone_urls[0], mode_list, timeout
            )
        except Exception as error:  # noqa: BLE001 - report per-repo failures
            return {
                "repo": repo,
                "status": "failed",
                "clone_mode": None,
                "error": str(error),
                "instances": len(group),
            }

        case_errors: List[str] = []
        failed_instance_ids: Set[str] = set()
        endpoints: Set[str] = set()
        for instance in group:
            try:
                _ensure_commit(repo_path, instance.base_commit, instance.pr_url, timeout)
                _ensure_commit(repo_path, instance.head_commit, instance.pr_url, timeout)
                merge_base = _git(
                    repo_path,
                    ["merge-base", instance.base_commit, instance.head_commit],
                    timeout=300,
                )
                if merge_base.returncode != 0 or not merge_base.stdout.strip():
                    raise SnapshotError(
                        "merge-base failed: "
                        + (merge_base.stderr.strip()[:300] or "no common ancestor")
                    )
                endpoints.update(
                    (instance.base_commit, instance.head_commit, merge_base.stdout.strip())
                )
            except Exception as error:  # noqa: BLE001 - collect instance failures
                case_errors.append(f"{instance.instance_id}: {error}")
                failed_instance_ids.add(instance.instance_id)

        checkout_errors: List[str] = []
        for commit in sorted(endpoints):
            process = _git(repo_path, ["checkout", "-f", "--detach", commit], timeout=timeout)
            if process.returncode != 0:
                checkout_errors.append(f"{commit[:12]}: {process.stderr.strip()[:200]}")
        diff_errors: List[str] = []
        for instance in group:
            if instance.instance_id in failed_instance_ids:
                continue
            process = _git(
                repo_path,
                ["diff", f"{instance.base_commit}...{instance.head_commit}", "--stat"],
                timeout=600,
            )
            if process.returncode != 0:
                diff_errors.append(f"{instance.instance_id}: {process.stderr.strip()[:200]}")

        errors = case_errors + checkout_errors + diff_errors
        return {
            "repo": repo,
            "status": "ok" if not errors else "failed",
            "clone_mode": clone_mode,
            "path": str(repo_path),
            "instances": len(group),
            "errors": errors[:20],
        }

    effective_jobs = max(1, min(int(jobs or 1), len(groups) or 1))
    with ThreadPoolExecutor(max_workers=effective_jobs) as executor:
        futures = {
            executor.submit(prepare_one, repo, group): repo
            for repo, group in sorted(groups.items())
        }
        for future in as_completed(futures):
            repo = futures[future]
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001 - collect unexpected failures
                result = {"repo": repo, "status": "failed", "errors": [str(error)]}
            results[repo] = result
            if result["status"] != "ok":
                blocked.append(result)
            log(f"[partial] {repo}: {result['status']}")

    payload = {
        "generated_at": _now_iso(),
        "modes": mode_list,
        "repos": results,
        "blocked": blocked,
        "status": "ok" if not blocked else "failed",
    }
    manifest_path = Path(repo_dir) / PARTIAL_MANIFEST_NAME
    _write_json_atomic(manifest_path, payload)
    payload["manifest_path"] = str(manifest_path)
    return payload


def repos_ready(instances: Sequence[ReviewInstance], repo_dir: Path) -> Tuple[bool, List[str]]:
    """Quick check that existing repositories already cover the dataset."""
    errors: List[str] = []
    groups: Dict[str, List[ReviewInstance]] = {}
    for instance in instances:
        groups.setdefault(instance.repo, []).append(instance)
    for repo, group in groups.items():
        repo_path = Path(repo_dir) / repo.replace("/", "__")
        if not (repo_path / ".git").exists():
            errors.append(f"{repo}: repository missing")
            continue
        for instance in group:
            for commit in (instance.base_commit, instance.head_commit):
                if not _has_commit(repo_path, commit):
                    errors.append(f"{repo}: commit missing {commit[:12]}")
            merge_base = _git(
                repo_path,
                ["merge-base", instance.base_commit, instance.head_commit],
                timeout=300,
                env_extra={"GIT_NO_LAZY_FETCH": "1"},
            )
            if merge_base.returncode != 0 or not merge_base.stdout.strip():
                errors.append(f"{repo}: merge-base unavailable for {instance.instance_id}")
            diff = _git(
                repo_path,
                ["diff", f"{instance.base_commit}...{instance.head_commit}", "--stat"],
                timeout=600,
                env_extra={"GIT_NO_LAZY_FETCH": "1"},
            )
            if diff.returncode != 0:
                errors.append(f"{repo}: diff unavailable for {instance.instance_id}")
    return (not errors, errors[:50])


def _requires_lfs_skip(instances: Sequence[ReviewInstance], repo_dir: Path) -> bool:
    """Return True when repositories are shallow or partial clones.

    Snapshot archives keep LFS pointer files but not LFS objects, and partial
    clones may omit blobs. Checkout must therefore skip the LFS smudge filter.
    """
    for repo in sorted({instance.repo for instance in instances}):
        path = Path(repo_dir) / repo.replace("/", "__")
        if not (path / ".git").exists():
            continue
        if (path / ".git" / "shallow").exists():
            return True
        for key in ("remote.origin.promisor", "remote.origin.partialclonefilter"):
            process = _git(path, ["config", "--get", key], timeout=60)
            if process.returncode == 0 and process.stdout.strip():
                return True
    return False


def prepare_repo_source(instances: Sequence[ReviewInstance], args: Any) -> Dict[str, Any]:
    """Prepare the repository source selected by ``--repo-source``."""
    set_blocked_repositories([])
    repo_source = getattr(args, "repo_source", "auto")
    repo_dir = Path(args.repo_dir)
    required = sorted({instance.repo for instance in instances})

    if repo_source == "git":
        log("[repo] repo-source=git: using the legacy full clone path")
        return {"source": "git"}

    if repo_source == "auto":
        ready, errors = repos_ready(instances, repo_dir)
        if ready:
            if _requires_lfs_skip(instances, repo_dir):
                os.environ.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
            log(f"[repo] repo-source=auto: existing repositories cover {len(required)} repo(s)")
            return {"source": "existing", "repos": len(required)}
        if errors:
            log(f"[repo] existing repositories are incomplete ({len(errors)} issue(s)); preparing")
    elif repo_source == "partial":
        os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
        os.environ.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
        result = prepare_partial_clones(
            instances,
            repo_dir,
            modes=getattr(args, "partial_clone_modes", "tree0,blobless,full"),
            timeout=getattr(args, "repo_timeout", 1800),
            jobs=getattr(args, "partial_jobs", 2),
        )
        ready = sum(1 for item in result.get("repos", {}).values() if item.get("status") == "ok")
        set_blocked_repositories(
            item.get("repo") for item in result.get("blocked", []) if item.get("repo")
        )
        if result["status"] != "ok":
            if ready == 0:
                raise SnapshotError(f"partial clone preparation failed: {result['blocked']}")
            log(
                f"[repo] partial clone incomplete: {ready} repo(s) ready, "
                f"{len(result['blocked'])} blocked; affected instances will report errors"
            )
        return {"source": "partial", "repos": len(required), "manifest": result.get("manifest_path")}

    if repo_source not in ("auto", "snapshot"):
        raise SnapshotError(f"unknown repo source: {repo_source}")

    try:
        result = ensure_snapshots(
            repo_dir=repo_dir,
            archive_dir=Path(getattr(args, "snapshot_archive_dir", config.EVALUATION_DIR / "snapshots")),
            repo_id=getattr(args, "snapshot_repo_id", DEFAULT_SNAPSHOT_REPO),
            revision=getattr(args, "snapshot_revision", DEFAULT_SNAPSHOT_REVISION),
            endpoint=getattr(args, "snapshot_endpoint", None),
            jobs=getattr(args, "snapshot_jobs", 4),
            download=not getattr(args, "snapshot_no_download", False),
            extract=not getattr(args, "snapshot_no_extract", False),
            required_repos=required,
            instances=instances,
            scope=getattr(args, "snapshot_scope", "all"),
            check_blobs=not getattr(args, "snapshot_no_blob_check", False),
        )
        if result["status"] != "ok":
            raise SnapshotError(f"snapshot verification failed: {result}")
        ready, errors = repos_ready(instances, repo_dir)
        if not ready:
            raise SnapshotError(
                "snapshot extraction is disabled or incomplete; repository cache does not cover "
                f"the dataset ({len(errors)} issue(s))"
            )
        os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
        os.environ.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
        return {"source": "snapshot", "repos": len(required), "manifest": result.get("manifest_path")}
    except SnapshotCoverageError as error:
        if repo_source == "snapshot" or not getattr(args, "fallback_partial", True):
            raise
        if getattr(args, "snapshot_no_download", False) or getattr(args, "snapshot_no_extract", False):
            raise SnapshotError(
                "snapshot preparation failed while --snapshot-no-download/--snapshot-no-extract "
                "was set; refusing to fall back to a GitHub partial clone"
            ) from error
        log(f"[repo] snapshot coverage is incomplete: {error}")
    except SnapshotError as error:
        if repo_source == "snapshot" or not getattr(args, "fallback_partial", True):
            raise
        if getattr(args, "snapshot_no_download", False) or getattr(args, "snapshot_no_extract", False):
            raise SnapshotError(
                "snapshot preparation failed while --snapshot-no-download/--snapshot-no-extract "
                "was set; refusing to fall back to a GitHub partial clone"
            ) from error
        log(f"[repo] snapshot preparation failed, falling back to partial clone: {error}")

    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    os.environ.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    partial = prepare_partial_clones(
        instances,
        repo_dir,
        modes=getattr(args, "partial_clone_modes", "tree0,blobless,full"),
        timeout=getattr(args, "repo_timeout", 1800),
        jobs=getattr(args, "partial_jobs", 2),
    )
    ready = sum(1 for item in partial.get("repos", {}).values() if item.get("status") == "ok")
    set_blocked_repositories(
        item.get("repo") for item in partial.get("blocked", []) if item.get("repo")
    )
    if partial["status"] != "ok":
        if ready == 0:
            raise SnapshotError(
                f"both snapshot and partial clone preparation failed; blocked: {partial['blocked']}"
            )
        log(
            f"[repo] snapshot unavailable and partial clone incomplete: {ready} repo(s) ready, "
            f"{len(partial['blocked'])} blocked; affected instances will report errors"
        )
    return {"source": "partial", "repos": len(required), "manifest": partial.get("manifest_path")}


def run_snapshot_command(args: Any) -> int:
    """CLI entry for ``python -m pipeline snapshot``."""
    instances: Optional[Sequence[ReviewInstance]] = None
    if getattr(args, "dataset", None):
        instances = load_instances(Path(args.dataset))
    required = None
    if getattr(args, "scope", "all") == "dataset" and not getattr(args, "dataset", None):
        log("[snapshot] FAILED: --scope dataset requires --dataset")
        return 1
    if instances is not None and getattr(args, "scope", "all") == "dataset":
        required = sorted({instance.repo for instance in instances})
    try:
        result = ensure_snapshots(
            repo_dir=Path(args.repo_dir),
            archive_dir=Path(args.archive_dir),
            repo_id=args.repo_id,
            revision=args.revision,
            endpoint=args.endpoint,
            jobs=args.jobs,
            download=not args.no_download,
            extract=not args.no_extract,
            required_repos=required,
            instances=instances,
            scope=args.scope,
            check_blobs=not args.no_blob_check,
            verify_only=args.verify_only,
        )
    except SnapshotError as error:
        log(f"[snapshot] FAILED: {error}")
        return 1

    print(
        json.dumps(
            {
                "status": result["status"],
                "archives": {
                    "expected": result["archives"].get("expected"),
                    "verified": result["archives"].get("verified"),
                },
                "extraction": {
                    "expected": result["extraction"].get("expected"),
                    "extracted": result["extraction"].get("extracted"),
                    "existing": result["extraction"].get("existing"),
                },
                "repositories": {
                    "expected": result["repositories"].get("expected"),
                    "ok": result["repositories"].get("ok"),
                    "failed": result["repositories"].get("failed"),
                },
                "dataset": {
                    "instances": result["dataset"].get("instances"),
                    "ok": result["dataset"].get("ok"),
                    "failed": result["dataset"].get("failed"),
                },
                "manifest": result.get("manifest_path"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["status"] == "ok" else 1