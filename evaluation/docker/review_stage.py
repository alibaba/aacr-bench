"""Run the upstream review stage inside a fresh, disposable container."""
from __future__ import annotations
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repo_utils
from reviewers import ocr, claude, codex


def preserve_worktree(path):
    repo_utils.log(f"Preserving isolated review worktree: {path}")


class PreservingOS:
    """Keep temporary hook settings instead of physically deleting files."""
    def __getattr__(self, name):
        return getattr(os, name)

    def unlink(self, path, *args, **kwargs):
        repo_utils.log(f"Preserving runtime settings: {path}")


if os.environ.get("AB_PRESERVE_RUNTIME") != "1":
    raise SystemExit("this entry point is only for an isolated review container")

os.environ["OCR_NO_UPDATE"] = "1"
os.environ["DISABLE_AUTOUPDATER"] = "1"
_original_codex_config = codex.write_codex_config


def write_codex_config_without_updates(environment):
    home = _original_codex_config(environment)
    path = home / "config.toml"
    path.write_text("check_for_update_on_startup = false\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    return home


codex.write_codex_config = write_codex_config_without_updates
repo_utils.clean_worktree = preserve_worktree
ocr.clean_worktree = preserve_worktree
claude.clean_worktree = preserve_worktree
codex.clean_worktree = preserve_worktree
codex.cleanup_codex_home = preserve_worktree
claude.os = PreservingOS()
Path.unlink = lambda self, *args, **kwargs: repo_utils.log(f"Preserving runtime output: {self}")
runpy.run_module("pipeline", run_name="__main__")
