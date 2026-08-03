#!/usr/bin/env python3
"""StopFailure hook：当 Claude Code 内置 API retry 耗尽仍失败时，记录到日志。

Claude Code 在 turn 因 API 错误终止（所有内置重试耗尽）后触发 StopFailure 事件，
通过 stdin 传入事件 JSON。本脚本将失败信息追加写入 retry_exhausted.jsonl。

环境变量（由 claude.py 的 hook settings 注入）：
  REVIEW_RESULTS_DIR  — 结果目录，日志写入该目录下的 retry_exhausted.jsonl
  REVIEW_INSTANCE_ID  — 当前评审实例 ID
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    results_dir = os.environ.get("REVIEW_RESULTS_DIR", ".")
    instance_id = os.environ.get("REVIEW_INSTANCE_ID", "unknown")
    log_path = Path(results_dir) / "retry_exhausted.jsonl"

    try:
        raw_input = sys.stdin.read()
        event = json.loads(raw_input) if raw_input.strip() else {}
    except (json.JSONDecodeError, OSError):
        event = {}

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instance_id": instance_id,
        "error": event.get("stop_hook_error", event.get("error", "unknown")),
        "error_status": event.get("error_status"),
        "attempt": event.get("attempt"),
        "max_retries": event.get("max_retries"),
        "raw_event": event,
    }

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
