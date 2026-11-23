#!/usr/bin/env python3
"""Container entrypoint for one AACR-Bench review instance.

The container performs repository preparation and review only. Evaluation,
ground truth, and judge credentials stay on the host; the container writes
review results and a run manifest to /work/out.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PR_DIR = Path(os.environ.get("AB_PR_DIR", "/work/pr"))
OUT_DIR = Path(os.environ.get("AB_OUT", "/work/out"))
WORK_DIR = Path(os.environ.get("AB_WORK_DIR", "/work"))
EVAL_DIR = Path(
    os.environ.get(
        "AB_EVALUATION_DIR",
        os.environ.get("AACR_EVALUATION_DIR", "/opt/aacr/evaluation"),
    )
)
REPO_LINK = Path("/work/repo")
DATASET_NAME = os.environ.get("AB_DATASET_NAME", "aacr_bench.jsonl")
REVIEWER = os.environ.get("AB_REVIEWER", "ocr")
sys.path.insert(0, str(EVAL_DIR))
from docker_contract import read_json, validate_result, write_result, redact_payload


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def exported_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, 0o755)
    finally:
        os.close(descriptor)


def write_exported_text(path: Path, text: str) -> None:
    directory_flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent = os.open(path.parent, directory_flags)
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o644, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("export must be a regular, unshared file")
        os.fchmod(handle.fileno(), 0o644)
        os.ftruncate(handle.fileno(), 0)
        handle.write(text)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_exported_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(
            src,
            dst,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "*.partial.json", "summary_*.json", "metrics_*.json"
            ),
        )


def run_stage(command: list[str]) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    return subprocess.run(
        command,
        cwd=str(EVAL_DIR),
        text=True,
        capture_output=True,
        env=environment,
    )


def load_instance() -> dict:
    payload = json.loads((PR_DIR / "pr.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pr.json must contain one JSON object")
    for key in ("instance_id", "repo", "base_commit", "head_commit"):
        if not payload.get(key):
            raise ValueError(f"pr.json missing required field: {key}")
    return payload


def count_findings(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    review = result.get("review")
    if isinstance(review, dict):
        comments = review.get("comments")
        if isinstance(comments, list):
            return len(comments)
    review_output = result.get("review_output")
    if isinstance(review_output, list):
        return len(review_output)
    return 0


def materialization_source(output: str) -> str:
    text = output.lower()
    if "repository source: local" in text:
        return "local"
    if "repository source: archive" in text:
        return "archive"
    if "repository source: hf snapshot" in text:
        return "hf"
    if "repository source: github" in text:
        return "github"
    return "unknown"


def main() -> int:
    started = time.monotonic()
    manifest: dict = {
        "schema_version": "aacr-docker-review-instance/v1",
        "status": "failed",
        "started_at": utc_now(),
        "reviewer": REVIEWER,
        "image_digest": os.environ.get("AB_IMAGE_DIGEST", ""),
        "python": sys.version.split()[0],
    }
    exit_code = 2
    try:
        instance = load_instance()
        instance.pop("reference_comments", None)
        os.environ["AB_PRESERVE_RUNTIME"] = "1"
        os.environ["HOME"] = str(WORK_DIR / "home-runtime")
        os.environ["CODEX_HOME_BASE"] = str(WORK_DIR / "codex-runtime")
        for profile in (Path(os.environ["HOME"]), Path(os.environ["CODEX_HOME_BASE"])):
            profile.mkdir(parents=True, exist_ok=True)
        manifest.update(
            {
                "instance_id": instance["instance_id"],
                "repo": instance["repo"],
                "base_commit": instance["base_commit"],
                "head_commit": instance["head_commit"],
            }
        )

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        repo_dir = WORK_DIR / "repo-runtime"
        results_dir = WORK_DIR / "results-runtime"
        for path in (repo_dir, results_dir, WORK_DIR / "snapshots" / "repos"):
            path.mkdir(parents=True, exist_ok=True)

        dataset_path = PR_DIR / DATASET_NAME
        if not dataset_path.is_file():
            dataset_path = WORK_DIR / DATASET_NAME
            with dataset_path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(instance, ensure_ascii=False) + "\n")

        run_id = os.environ.get("AB_RUN_ID", "docker")
        ocr_command = os.environ.get("AB_OCR_COMMAND", "ocr")
        max_tools = os.environ.get("AB_MAX_TOOLS", "30")
        timeout_minutes = os.environ.get("AB_TIMEOUT_MINUTES", "30")

        command = [
            sys.executable,
            str(EVAL_DIR / "docker" / "review_stage.py"),
            "run",
            "--stage",
            "review",
            "--reviewer",
            REVIEWER,
            "--dataset",
            str(dataset_path),
            "--run-id",
            run_id,
            "--repo-dir",
            str(repo_dir),
            "--results-dir",
            str(results_dir),
            "--concurrency",
            "1",
            "--timeout-minutes",
            timeout_minutes,
        ]
        if REVIEWER == "ocr":
            command += ["--ocr-command", ocr_command, "--max-tools", max_tools]
        if os.environ.get("AB_PREVIEW", "0") == "1":
            command.append("--preview")
        manifest["review_command"] = command

        review = run_stage(command)
        manifest["review"] = {
            "returncode": review.returncode,
            "stdout_tail": review.stdout[-4000:],
            "stderr_tail": review.stderr[-4000:],
        }
        manifest["materialization_source"] = materialization_source(review.stdout)

        logs_dir = OUT_DIR / "logs"
        exported_directory(logs_dir)
        secret_values = [value for key, value in os.environ.items() if key.endswith(("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")) and value]
        def redact(text):
            if isinstance(text, bytes):
                text = text.decode("utf-8", errors="replace")
            for secret in secret_values:
                text = text.replace(secret, "<redacted>")
            return text
        manifest["review"] = redact_payload(manifest["review"], secret_values)
        write_exported_text(logs_dir / "review.stdout", redact(review.stdout))
        write_exported_text(logs_dir / "review.stderr", redact(review.stderr))

        summary_path = results_dir / f"summary_{REVIEWER}.json"
        summary = []
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        manifest["instance_status"] = summary[0].get("status") if summary else None
        if review.returncode != 0 or not summary or (
            manifest["instance_status"] not in ("ok", "preview")
        ):
            raise RuntimeError(
                f"review failed (exit={review.returncode}, "
                f"status={manifest['instance_status']})"
            )

        copied = 0
        out_results = OUT_DIR / "results"
        exported_directory(out_results)
        filename = instance["instance_id"].replace("/", "__") + ".json"
        result_path = results_dir / filename
        preview = os.environ.get("AB_PREVIEW", "0") == "1"
        if result_path.exists() or result_path.is_symlink():
            payload = read_json(result_path)
            validate_result(payload, instance, REVIEWER, preview)
            write_result(out_results, filename, redact_payload(payload, secret_values), mode=0o644)
            copied = 1
            manifest["findings"] = count_findings(payload)
        elif not preview:
            raise RuntimeError("review succeeded without the expected result file")
        manifest["result_files"] = copied

        materialized = repo_dir / instance["repo"].replace("/", "__")
        if materialized.is_dir() and not REPO_LINK.exists():
            try:
                REPO_LINK.symlink_to(materialized, target_is_directory=True)
            except OSError:
                pass

        manifest["status"] = "ok"
        exit_code = 0
    except Exception as error:  # noqa: BLE001 - the manifest must always be written
        manifest["error"] = f"{type(error).__name__}: {error}"
        exit_code = 2
    finally:
        try:
            runtime_results = WORK_DIR / "results-runtime"
            if manifest["status"] != "ok" and runtime_results.is_dir():
                failed_dir = OUT_DIR / "failed-results"
                exported_directory(failed_dir)
                safe_id = manifest.get("instance_id", "").replace("/", "__")
                for suffix in (".json", ".partial.json"):
                    source = runtime_results / (safe_id + suffix)
                    if source.is_file() and not source.is_symlink():
                        try:
                            payload = read_json(source)
                            write_result(failed_dir, source.name, redact_payload(payload, locals().get("secret_values", ())), mode=0o644)
                        except (OSError, ValueError):
                            pass
        except OSError:
            pass
        manifest["ended_at"] = utc_now()
        manifest["duration_seconds"] = round(time.monotonic() - started, 2)
        manifest["outputs"] = sorted(
            str(path.relative_to(OUT_DIR))
            for directory in ("results", "failed-results", "logs")
            for path in (OUT_DIR / directory).rglob("*")
            if path.is_file()
        )
        secret_values = [value for key, value in os.environ.items() if key.endswith(("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")) and value]
        write_json(OUT_DIR / "run.json", redact_payload(manifest, secret_values))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
