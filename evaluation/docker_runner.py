"""Docker-backed review execution for the AACR-Bench pipeline.

The ``pipeline run --docker`` path runs only the review stage inside one
container per instance. Evaluation stays on the host and consumes the canonical
run directory produced here, so the existing ``pipeline run --stage eval``
workflow remains unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import config
from repo_utils import log
from schema import ReviewInstance, load_instances
from reviewer_versions import VERSION_KEYS, REVIEWER_VERSION_KEYS, resolve_versions
from docker_contract import read_json, validate_identity, validate_result, write_result, redact_payload

DEFAULT_IMAGE = os.environ.get("AACR_DOCKER_IMAGE", "aacr-review:latest")
DOCKER_COMMAND = ["docker"]
CONTAINER_TIMEOUT_SECONDS = 7200
CONTAINER_PREPARATION_SECONDS = 1800
CONTAINER_CLEANUP_SECONDS = 60
NETWORK_MODE = "bridge"


def _default_container_user() -> Optional[str]:
    """Match non-root Linux callers so default host directories stay writable."""
    if not sys.platform.startswith("linux"):
        return None
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        return None
    return f"{os.getuid()}:{os.getgid()}"

# Only the active reviewer's variables are read from an env file. Judge
# variables are intentionally excluded: evaluation must not run in the
# review container.
ROLE_ENV: Dict[str, Tuple[str, ...]] = {
    "ocr": (
        "OCR_LLM_URL",
        "OCR_LLM_TOKEN",
        "OCR_LLM_MODEL",
        "OCR_USE_ANTHROPIC",
    ),
    "claude": (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_MAX_RETRIES",
    ),
    "codex": (
        "CODEX_API_KEY",
        "CODEX_MODEL",
        "CODEX_GATEWAY_URL",
    ),
}

NON_SECRET_ENV = (
    "HF_ENDPOINT",
    "NETWORK_TIMEOUT_SECONDS",
)

SECRET_ENV_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")


class DockerError(RuntimeError):
    """Raised when the Docker review cannot be prepared or executed."""


@dataclass
class DockerReviewConfig:
    dataset: Path
    reviewer: str
    image: str = DEFAULT_IMAGE
    run_id: Optional[str] = None
    limit: Optional[int] = None
    concurrency: int = 1
    timeout_minutes: int = 30
    max_tools: int = 30
    ocr_command: str = "ocr"
    preview: bool = False
    cpus: float = 1.0
    memory: str = "2g"
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    versions: Dict[str, str] = field(default_factory=lambda: {key: "latest" for key in VERSION_KEYS})

    @classmethod
    def from_args(cls, args: Any) -> "DockerReviewConfig":
        return cls(
            dataset=Path(args.dataset),
            reviewer=args.reviewer,
            image=args.image,
            run_id=args.run_id,
            limit=args.limit,
            concurrency=max(1, int(args.concurrency)),
            timeout_minutes=int(args.timeout_minutes),
            max_tools=int(args.max_tools),
            ocr_command=args.ocr_command,
            preview=bool(args.preview),
            cpus=float(getattr(args, "cpus", 1.0)),
            memory=str(getattr(args, "memory", "2g")),
            versions=resolve_versions(config.ENV_FILE),
        )

    @property
    def dataset_name(self) -> str:
        """Return a dataset filename accepted by benchmark_version_from_dataset."""
        key = Path(self.dataset).stem.lower()
        if "v2" in key:
            return "aacr_bench_v2.jsonl"
        if "v1" in key:
            return "aacr_bench.jsonl"
        return "aacr_bench.jsonl"


class ContainerCancelled(RuntimeError):
    """The user interrupted a review run."""


def _run(
    command: Sequence[str],
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess:
    if cancel_event is None:
        return subprocess.run(list(command), text=True, capture_output=True, env=env, timeout=timeout)
    started = time.monotonic()
    with subprocess.Popen(list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as process:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                cancelled = cancel_event.is_set()
                expired = timeout is not None and time.monotonic() - started >= timeout
                if cancelled or expired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    if cancelled:
                        raise ContainerCancelled("review run interrupted")
                    raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([bkmg]?)", value.lower())
    if not match:
        raise DockerError("--memory must be a positive size such as 512m or 2g")
    return int(match[1]) * {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match[2]]


def _container_timeout_seconds(value: DockerReviewConfig) -> int:
    """Allow preparation, the reviewer's grace period, and cleanup."""
    reviewer_grace = 300 if value.reviewer == "ocr" else 60
    return max(
        CONTAINER_TIMEOUT_SECONDS,
        value.timeout_minutes * 60 + CONTAINER_PREPARATION_SECONDS
        + reviewer_grace + CONTAINER_CLEANUP_SECONDS,
    )


def _validate_options(value: DockerReviewConfig) -> None:
    if value.limit is not None and value.limit <= 0:
        raise DockerError("--limit must be positive")
    if value.cpus <= 0 or value.cpus != value.cpus or value.cpus == float("inf"):
        raise DockerError("--cpus must be finite and positive")
    if _memory_bytes(value.memory) < 128 * 1024 ** 2:
        raise DockerError("--memory must be at least 128m")
    if value.timeout_minutes <= 0:
        raise DockerError("review timeout must be positive")


def _security_arguments(value: DockerReviewConfig) -> List[str]:
    args = [
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--cpus", str(value.cpus), "--memory", value.memory,
        "--memory-swap", value.memory, "--pids-limit", "256",
        "--tmpfs", "/tmp:rw,exec,size=2g,mode=1777",
        "--tmpfs", "/home/aacr:rw,exec,size=256m,mode=1777",
    ]
    user = _default_container_user()
    if user:
        args += ["--user", user]
    return args


def _container_record(engine: Sequence[str], container_id: str) -> Optional[dict]:
    template = '{"id":{{json .Id}},"running":{{.State.Running}},"task":{{json (index .Config.Labels "aacr.task")}},"attempt":{{json (index .Config.Labels "aacr.attempt")}}}'
    result = _run([*engine, "container", "inspect", "--format", template, container_id], timeout=30)
    if result.returncode != 0:
        if "no such container" in (result.stderr + result.stdout).lower():
            return None
        raise DockerError("cannot verify the registered container state")
    record = json.loads(result.stdout)
    if not isinstance(record, dict) or record.get("id") != container_id:
        raise DockerError("container inspect returned a different identity")
    return record


def _stop_container(engine: Sequence[str], cidfile: Path, task: str, attempt: str) -> dict:
    """Stop only a registered full ID after verifying its task and attempt."""
    try:
        if not cidfile.is_file() or cidfile.is_symlink():
            return {"status": "unknown", "error": "container ID was not recorded"}
        container_id = cidfile.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise DockerError("invalid container ID in cidfile")
        record = _container_record(engine, container_id)
        if record is None:
            return {"status": "removed", "container_id": container_id}
        if record.get("task") != task or record.get("attempt") != attempt:
            raise DockerError("container ownership does not match this attempt")
        if record.get("running"):
            stopped = _run([*engine, "stop", "-t", "5", container_id], timeout=30)
            if stopped.returncode != 0:
                raise DockerError("registered container stop failed")
        record = _container_record(engine, container_id)
        if record is not None and record.get("running"):
            raise DockerError("registered container is still running after stop")
        return {"status": "removed" if record is None else "stopped", "container_id": container_id}
    except (OSError, ValueError, DockerError, subprocess.TimeoutExpired) as error:
        return {"status": "failed", "error": str(error)}


def _reviewer_command(config_value: DockerReviewConfig) -> str:
    if config_value.reviewer == "ocr":
        return config_value.ocr_command
    return config_value.reviewer


def _check_reviewer_cli(config_value: DockerReviewConfig) -> None:
    parent = config.DEFAULT_RESULTS_DIR / ".docker-preflight"
    parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    token = evidence.name
    cidfile = evidence / "container.cid"
    task = "preflight-" + token
    command = [
        *DOCKER_COMMAND, "run", "--rm", "--cidfile", str(cidfile),
        "--label", f"aacr.task={task}", "--label", f"aacr.attempt={token}",
        "--network", "none", *_security_arguments(config_value),
        "--entrypoint", "/bin/sh", config_value.image, "-ec",
        "test \"$(id -u)\" -ne 0; " + shlex.quote(_reviewer_command(config_value)) + (" version" if config_value.reviewer == "ocr" else " --version"),
    ]
    record = {"task": task, "attempt": token, "image_id": config_value.image}
    try:
        result = _run(command, timeout=60)
        record["returncode"] = result.returncode
        if result.returncode != 0:
            raise DockerError(f"image does not provide a working non-root reviewer CLI: {result.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        record["cleanup"] = _stop_container(DOCKER_COMMAND, cidfile, task, token)
        if isinstance(error, KeyboardInterrupt):
            raise
        raise DockerError(f"reviewer preflight failed: {error}; cleanup={record['cleanup']['status']}") from error
    finally:
        if cidfile.is_file():
            record["container_id"] = cidfile.read_text(encoding="utf-8").strip()
        (evidence / "lifecycle.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def preflight(config_value: DockerReviewConfig) -> Dict[str, str]:
    """Check the engine, daemon, and image before starting any container."""
    _validate_options(config_value)
    engine = DOCKER_COMMAND
    label = "docker"

    try:
        version = _run([*engine, "--version"], timeout=30)
    except FileNotFoundError as error:
        raise DockerError(
            f"{label} CLI not found. Install Docker."
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DockerError(f"{label} --version failed: {error}") from error
    if version.returncode != 0:
        raise DockerError(
            f"{label} --version failed: {version.stderr.strip() or version.stdout.strip()}"
        )

    format_value = '{"server_version":{{json .ServerVersion}},"name":{{json .Name}},"id":{{json .ID}},"memory_bytes":{{.MemTotal}},"cpus":{{.NCPU}}}'
    try:
        info = _run([*engine, "info", "--format", format_value], timeout=60)
        host = json.loads(info.stdout) if info.returncode == 0 else None
        if not isinstance(host, dict) or not all(host.get(key) for key in ("server_version", "name", "id", "memory_bytes", "cpus")):
            raise DockerError("daemon preflight returned missing or invalid identity/resource fields")
        if config_value.concurrency * _memory_bytes(config_value.memory) > int(host["memory_bytes"]) // 2:
            raise DockerError("combined container memory limits exceed 50% of daemon memory; reduce concurrency or --memory")
        if config_value.cpus > float(host["cpus"]):
            raise DockerError("--cpus exceeds daemon CPU capacity")
        template = '{"image_id":{{json .Id}},"user":{{json .Config.User}},"volumes":{{json (index .Config "Volumes")}},"versions":{"OCR_VERSION":{{with index .Config "Labels"}}{{json (index . "org.aacr.ocr.version")}}{{else}}null{{end}},"CLAUDE_CODE_VERSION":{{with index .Config "Labels"}}{{json (index . "org.aacr.claude.version")}}{{else}}null{{end}},"CODEX_VERSION":{{with index .Config "Labels"}}{{json (index . "org.aacr.codex.version")}}{{else}}null{{end}}}}'
        inspect = _run([*engine, "image", "inspect", "--format", template, config_value.image], timeout=60)
        image = json.loads(inspect.stdout) if inspect.returncode == 0 else None
        if not isinstance(image, dict) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("image_id", "")):
            raise DockerError("image is unavailable or its immutable ID is invalid")
        user = image.get("user", "")
        if not user or user.split(":")[0] in ("0", "root"):
            raise DockerError("image must specify a non-root user")
        if image.get("volumes"):
            raise DockerError("image declares anonymous volumes; use an image without VOLUME")
        version_key = REVIEWER_VERSION_KEYS[config_value.reviewer]
        requested = config_value.versions.get(version_key, "latest")
        installed = (image.get("versions") or {}).get(version_key)
        if requested != "latest" and requested != installed:
            raise DockerError(f"image version mismatch for {version_key}; rebuild with python docker/build_image.py using the same version configuration")
        image_id = image["image_id"]
        config_value.image = image_id
        _check_reviewer_cli(config_value)
        return {"engine": "docker", "server": host["server_version"], "daemon_id": host["id"], "daemon_name": host["name"], "image_id": image_id}
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
        raise DockerError(f"container preflight failed: {error}") from error


def _prepare_environment(
    config_value: DockerReviewConfig,
) -> Tuple[Dict[str, str], List[str]]:
    """Build the child environment and the list of env names to forward."""
    child_env = os.environ.copy()
    names: List[str] = []
    for name in (*ROLE_ENV.get(config_value.reviewer, ()), *NON_SECRET_ENV):
        if name in child_env:
            names.append(name)
    return child_env, names


def _add_common_env(
    child_env: Dict[str, str],
    names: List[str],
    config_value: DockerReviewConfig,
    instance: ReviewInstance,
    image_id: str,
) -> None:
    values = {
        "AB_REVIEWER": config_value.reviewer,
        "AB_RUN_ID": config_value.run_id or "",
        "AB_OCR_COMMAND": config_value.ocr_command,
        "AB_MAX_TOOLS": str(config_value.max_tools),
        "AB_TIMEOUT_MINUTES": str(config_value.timeout_minutes),
        "AB_DATASET_NAME": config_value.dataset_name,
        "AB_IMAGE_DIGEST": image_id,
        "AB_PREVIEW": "1" if config_value.preview else "0",
        "OCR_NO_UPDATE": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CODEX_SANDBOX_MODE": "danger-full-access",
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "900000",
    }
    for key, value in values.items():
        child_env[key] = value
        if key not in names:
            names.append(key)


def _container_name(instance: ReviewInstance, run_id: str) -> str:
    raw = f"aacr-{instance.safe_id}-{run_id}"
    sanitized = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in raw
    )
    if len(sanitized) <= 120:
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:12]
    prefix = sanitized[:107].rstrip("-._")
    return f"{prefix}-{digest}"[:120]


def build_run_command(
    config_value: DockerReviewConfig,
    instance: ReviewInstance,
    pr_dir: Path,
    out_dir: Path,
    env_names: Sequence[str],
    image_id: str,
    attempt: Optional[str] = None,
    cidfile: Optional[Path] = None,
) -> List[str]:
    engine = DOCKER_COMMAND
    command = [
        *engine,
        "run",
        "--rm",
        "--name",
        _container_name(instance, attempt or config_value.run_id or "docker"),
    ]
    if cidfile is not None:
        command += ["--cidfile", str(cidfile)]
    command += ["--network", NETWORK_MODE]
    command += _security_arguments(config_value)
    # Harden the review container. The image is read-only; writable runtime
    # areas are tmpfs, and only the explicit bind mounts below are writable
    # (except the read-only inputs).
    command += [
        "--tmpfs", "/work/repo-runtime:rw,exec,size=16g,mode=1777",
        "--tmpfs", "/work/results-runtime:rw,exec,size=2g,mode=1777",
        "--tmpfs", "/work/home-runtime:rw,exec,size=256m,mode=1777",
        "--tmpfs", "/work/codex-runtime:rw,exec,size=256m,mode=1777",
        "--label", f"aacr.task={config_value.run_id}",
        "--label", f"aacr.attempt={attempt or config_value.run_id}",
    ]
    command += ["--tmpfs", "/work/snapshots:rw,exec,size=8g,mode=1777"]
    command += ["-v", f"{pr_dir}:/work/pr:ro", "-v", f"{out_dir}:/work/out"]
    for name in env_names:
        command += ["-e", name]
    command.append(image_id)
    return command


def _host_diagnostic_path(out_dir: Path, relative: str, safe_id: str) -> str:
    """Map an exported diagnostic after checking each path without following links."""
    allowed = {
        f"diagnostics/{safe_id}.claude_stream.jsonl",
        f"diagnostics/{safe_id}.claude_stderr.log",
        f"diagnostics/.collector/{safe_id}.hook.jsonl",
        f"diagnostics/.collector/{safe_id}.hook.meta.json",
        "diagnostics/.collector/collector_errors.jsonl",
    }
    if not isinstance(relative, str) or relative not in allowed:
        raise ValueError("invalid exported diagnostic path")
    parts = relative.split("/")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(out_dir, directory_flags)
    try:
        for name in parts[:-1]:
            previous = directory
            directory = os.open(name, directory_flags, dir_fd=previous)
            os.close(previous)
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("diagnostic must be a regular, unshared file")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    return str(Path(out_dir).absolute().joinpath(*parts))


def _map_diagnostic_paths(payload: dict, out_dir: Path, instance: ReviewInstance) -> None:
    """Point final Claude result metadata at the retained host diagnostics."""
    for name in ("claude_stream_log", "claude_stderr_log"):
        if payload.get(name) is not None:
            payload[name] = _host_diagnostic_path(out_dir, payload[name], instance.safe_id)
    metadata = payload.get("channel_meta")
    if isinstance(metadata, dict):
        findings = metadata.get("report_findings")
        if isinstance(findings, dict) and findings.get("meta_path") is not None:
            findings["meta_path"] = _host_diagnostic_path(
                out_dir, findings["meta_path"], instance.safe_id
            )


def _copy_results(out_dir: Path, run_dir: Path, instance: ReviewInstance,
                  reviewer: str, preview: bool = False, secret_values=()) -> int:
    filename = instance.safe_id + ".json"
    source = out_dir / "results" / filename
    if preview and not source.exists() and not source.is_symlink():
        return 0
    payload = read_json(source)
    validate_result(payload, instance.to_dict(), reviewer, preview)
    if reviewer == "claude":
        _map_diagnostic_paths(payload, out_dir, instance)
    write_result(run_dir, filename, redact_payload(payload, secret_values), overwrite=True)
    return 1


def _run_one(
    config_value: DockerReviewConfig,
    instance: ReviewInstance,
    run_root: Path,
    run_dir: Path,
    image_id: str,
) -> Dict[str, Any]:
    if config_value.cancel_event.is_set():
        return {"instance_id": instance.instance_id, "status": "cancelled", "error": "cancelled before container creation"}
    sid = instance.safe_id
    attempt_token = f"{time.time_ns()}-{os.getpid()}"
    attempt = f"{sid}-{attempt_token}"
    instance_root = run_root / "instances" / attempt
    pr_dir = instance_root / "pr"
    out_dir = instance_root / "out"
    logs_dir = instance_root / "logs"
    for path in (pr_dir, out_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    sample = instance.to_dict()
    sample.pop("reference_comments", None)
    (pr_dir / "pr.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pr_dir / config_value.dataset_name).write_text(
        json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    for path in (pr_dir / "pr.json", pr_dir / config_value.dataset_name):
        os.chmod(path, 0o644)

    child_env, env_names = _prepare_environment(config_value)
    _add_common_env(child_env, env_names, config_value, instance, image_id)
    cidfile = logs_dir / "container.cid"
    command = build_run_command(
        config_value,
        instance,
        pr_dir,
        out_dir,
        env_names,
        image_id,
        attempt=attempt_token,
        cidfile=cidfile,
    )
    (logs_dir / "command.json").write_text(
        json.dumps(
            {
                "command": command,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "env_names": list(env_names),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    started = time.monotonic()
    timed_out = False
    cleanup = None
    stdout = stderr = ""
    returncode = -1
    error_message = None
    try:
        completed = _run(command, env=child_env, timeout=_container_timeout_seconds(config_value),
                         cancel_event=config_value.cancel_event)
        stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        if returncode != 0:
            cleanup = _stop_container(DOCKER_COMMAND, cidfile, config_value.run_id or "", attempt_token)
    except (OSError, subprocess.TimeoutExpired, ContainerCancelled) as error:
        timed_out = isinstance(error, subprocess.TimeoutExpired)
        error_message = str(error)
        stdout, stderr = getattr(error, "stdout", "") or "", getattr(error, "stderr", "") or ""
        cleanup = _stop_container(DOCKER_COMMAND, cidfile, config_value.run_id or "", attempt_token)

    secret_values = [value for key, value in child_env.items() if key in env_names and key.endswith(SECRET_ENV_SUFFIXES) and value]
    def redact(value: str) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        for secret in secret_values:
            value = value.replace(secret, "<redacted>")
        return value

    (logs_dir / "container.log").write_text(
        f"returncode={returncode}\n--- stdout ---\n{redact(stdout)}\n--- stderr ---\n{redact(stderr)}\n", encoding="utf-8")
    container_id = cidfile.read_text(encoding="utf-8").strip() if cidfile.is_file() and not cidfile.is_symlink() else None
    manifest = None
    copied = 0
    status = "failed"
    try:
        manifest = read_json(out_dir / "run.json")
        validate_identity(manifest, instance.to_dict())
        if "diagnostics" in manifest:
            if not isinstance(manifest["diagnostics"], list):
                raise ValueError("manifest diagnostics must be a list")
            manifest["diagnostics"] = [
                _host_diagnostic_path(out_dir, path, instance.safe_id)
                for path in manifest["diagnostics"]
            ]
        if returncode != 0 or timed_out or error_message or manifest.get("status") != "ok":
            raise DockerError(manifest.get("error") or error_message or f"container failed with exit={returncode}")
        if manifest.get("reviewer") != config_value.reviewer or manifest.get("image_digest") != image_id:
            raise DockerError("container manifest reviewer/image identity mismatch")
        expected_status = ("ok", "preview") if config_value.preview else ("ok",)
        if manifest.get("instance_status") not in expected_status:
            raise DockerError("container manifest has no successful reviewer status")
        copied = _copy_results(out_dir, run_dir, instance, config_value.reviewer, config_value.preview, secret_values)
        status = "ok"
    except (OSError, ValueError, DockerError) as error:
        error_message = redact(str(error))
    manifest = redact_payload(manifest, secret_values)
    record = {"task": config_value.run_id, "attempt": attempt_token, "image_id": image_id,
              "container_id": container_id, "returncode": returncode, "cleanup": cleanup}
    (logs_dir / "lifecycle.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {
        "instance_id": instance.instance_id, "status": status, "returncode": returncode,
        "timed_out": timed_out, "container_id": container_id, "cleanup": cleanup,
        "duration_seconds": _elapsed(started), "result_files": copied,
        "manifest": manifest, "error": None if status == "ok" else error_message,
        "attempt_dir": str(instance_root),
    }


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 2)



def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_from_args(args: Any) -> int:
    """Compatibility entry point for review-only orchestration callers."""
    config_value = DockerReviewConfig.from_args(args)
    _validate_options(config_value)
    try:
        instances = load_instances(config_value.dataset, None)
    except Exception as error:
        raise DockerError(f"failed to load dataset: {error}") from error
    if config_value.limit is not None:
        instances = instances[:max(0, config_value.limit)]
    benchmark = config.benchmark_name_from_dataset(config_value.dataset)
    config_value.run_id = config.make_run_id(config_value.run_id)
    return run_review_stage(instances, config.run_dir(benchmark, config_value.reviewer, config_value.run_id), config_value)


def run_review_stage(
    instances: List[ReviewInstance],
    results_dir: Path,
    config_value: DockerReviewConfig,
) -> int:
    """Complete the supplied review selection before returning to evaluation."""
    _validate_options(config_value)
    if not instances:
        log("No instances to process.")
        return 0
    run_id = config.make_run_id(config_value.run_id)
    if run_id in (".", ".."):
        raise DockerError("run-id must identify a new run directory")
    config_value.run_id = run_id
    run_dir = Path(results_dir).expanduser().absolute()
    if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
        raise DockerError(f"results path must be a real directory: {run_dir}")
    if run_dir.exists():
        log(f"WARNING: review directory exists; selected results and summary will be overwritten: {run_dir}")
    if len({instance.safe_id for instance in instances}) != len(instances):
        raise DockerError("instance IDs collide after filename normalization")

    metadata = preflight(config_value)

    run_dir.mkdir(parents=True, exist_ok=True)
    run_root = run_dir / "docker-logs"
    run_root.mkdir(parents=True, exist_ok=True)

    log(
        f"Docker review: engine={metadata['engine']} image={config_value.image} "
        f"id={metadata['image_id']} instances={len(instances)} "
        f"concurrency={config_value.concurrency}"
    )

    results: List[Dict[str, Any]] = []
    executor = ThreadPoolExecutor(max_workers=config_value.concurrency)
    interrupted = False
    futures = {}
    try:
        for instance in instances:
            future = executor.submit(
                _run_one,
                config_value,
                instance,
                run_root,
                run_dir,
                metadata["image_id"],
            )
            futures[future] = instance
        for future in as_completed(futures):
            instance = futures[future]
            try:
                results.append(future.result())
            except Exception as error:  # noqa: BLE001 - one instance must not stop the run
                results.append(
                    {
                        "instance_id": instance.instance_id,
                        "status": "failed",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

    except KeyboardInterrupt:
        interrupted = True
        config_value.cancel_event.set()
    finally:
        executor.shutdown(wait=True, cancel_futures=interrupted)
    if interrupted:
        results = []
        for future, instance in futures.items():
            if future.cancelled():
                results.append({"instance_id": instance.instance_id, "status": "cancelled"})
            else:
                try:
                    results.append(future.result())
                except Exception as error:
                    results.append({"instance_id": instance.instance_id, "status": "failed", "error": str(error)})

    by_id = {instance.instance_id: instance for instance in instances}
    for outcome in results:
        if outcome["status"] == "ok":
            continue
        instance = by_id[outcome["instance_id"]]
        failure = instance.to_dict()
        failure.pop("reference_comments", None)
        failure.update({"reviewer": {"ocr":"ocr", "claude":"claude-code/code-review", "codex":"codex"}[config_value.reviewer],
                        f"{config_value.reviewer}_exit_code": outcome.get("returncode") or -1,
                        "status": "failed", "error": outcome.get("error", "review failed")})
        if config_value.reviewer == "ocr":
            failure["review"] = {"comments": []}
        else:
            failure["review_output"] = []
        write_result(run_dir, instance.safe_id + ".json", failure, overwrite=True)
    summary = sorted(results, key=lambda item: item["instance_id"])
    try:
        write_result(run_dir, f"summary_{config_value.reviewer}.json", summary, overwrite=True)
    except OSError as error:
        raise DockerError("cannot write review summary") from error
    manifest = {
        "schema_version": "aacr-docker-review/v1",
        "dataset": str(config_value.dataset.resolve()),
        "dataset_sha256": _sha256(config_value.dataset),
        "run_id": run_id,
        "reviewer": config_value.reviewer,
        "image": config_value.image,
        "image_id": metadata["image_id"],
        "engine": metadata["engine"],
        "server": metadata["server"],
        "daemon_id": metadata["daemon_id"],
        "daemon_name": metadata["daemon_name"],
        "cpus_per_container": config_value.cpus,
        "memory_per_container": config_value.memory,
        "concurrency": config_value.concurrency,
        "instances": len(instances),
        "instance_ids": [instance.instance_id for instance in instances],
        "limit": config_value.limit,
        "interrupted": interrupted,
        "counts": {
            status: sum(1 for item in results if item["status"] == status)
            for status in sorted({item["status"] for item in results})
        },
    }
    try:
        write_result(run_dir, "docker_run.json", manifest, overwrite=True)
    except OSError as error:
        raise DockerError("cannot write run manifest") from error

    succeeded = sum(1 for item in results if item["status"] == "ok")
    log(f"Docker review done. {succeeded}/{len(results)} succeeded. Results -> {run_dir}")
    if interrupted:
        return 130
    return 0
