#!/usr/bin/env python3
"""Build the review image using only version fields from local configuration."""
from __future__ import annotations
import argparse
import shlex
import subprocess
import sys
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALUATION_DIR))
from reviewer_versions import VERSION_KEYS, resolve_versions

BUILD_KEYS = {
    "BASE_IMAGE", "NODE_VERSION", "NODE_DIST_URL", "NPM_REGISTRY",
    "APT_MIRROR", "PIP_INDEX_URL", "PIP_TRUSTED_HOST",
    "INSTALL_OCR", "INSTALL_CLAUDE", "INSTALL_CODEX", "AACR_FRAMEWORK_COMMIT",
}


def build_command(image: str, versions: dict[str, str], docker_command: str = "docker",
                  no_cache: bool = False, build_args=()) -> list[str]:
    command = [*shlex.split(docker_command), "build", "-f", "docker/Dockerfile", "-t", image]
    if no_cache:
        command.append("--no-cache")
    for key in VERSION_KEYS:
        command += ["--build-arg", f"{key}={versions[key]}"]
    for item in build_args:
        key, separator, value = item.partition("=")
        if not separator or key not in BUILD_KEYS:
            raise ValueError(f"unsupported build argument: {key}")
        command += ["--build-arg", item]
    command.append(".")
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--docker-command", default="docker")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--build-arg", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.env_file is not None and not args.env_file.is_file():
        parser.error("specified version environment file does not exist")
    try:
        versions = resolve_versions(args.env_file or EVALUATION_DIR / ".env")
        command = build_command(args.image, versions, args.docker_command, args.no_cache, args.build_arg)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("Reviewer versions: " + ", ".join(f"{key}={versions[key]}" for key in VERSION_KEYS))
    if args.dry_run:
        print("Docker build prepared; no image built.")
        return 0
    return subprocess.run(command, cwd=EVALUATION_DIR).returncode


if __name__ == "__main__":
    raise SystemExit(main())
