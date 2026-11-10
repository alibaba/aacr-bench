"""AACR-Bench v2 JSON -> standard evaluation format converter.

v2 shares the field mapping and download/verification logic with
``converters.aacr_bench``; only the default input/output paths differ:

  benchmark/AACR-Bench-v2/positive_samples.json -> data/aacr_bench_v2.jsonl

The raw data is described by ``positive_samples.meta.json`` (url + sha256) in
the same directory and is downloaded and verified on first use.

Usage (inside evaluation/ with the venv activated):
    python -m converters.aacr_bench_v2 --validate
    python -m converters.aacr_bench_v2 \
        --input benchmark/AACR-Bench-v2/positive_samples.json \
        --output data/aacr_bench_v2.jsonl --validate
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# 允许以脚本方式直接运行：把 evaluation/ 加入 sys.path，使顶层模块可被导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from converters.aacr_bench import convert_file  # noqa: E402
from schema import load_instances  # noqa: E402

BENCHMARK_NAME = "AACR-Bench-v2"
BENCHMARK_KEY = "aacr_bench_v2"
DEFAULT_INPUT = config.benchmark_raw_dir(BENCHMARK_NAME) / "positive_samples.json"
DEFAULT_OUTPUT = config.dataset_path(BENCHMARK_KEY)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AACR-Bench v2 JSON -> 标准格式转换器"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"AACR-Bench v2 原始 JSON 路径（默认 {DEFAULT_INPUT}）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"输出标准格式 JSONL 路径（默认 {DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="乱序后只转换前 N 条（随机抽样）"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="乱序随机种子；指定后随机可复现（默认每次随机）",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="转换后用 schema 重新加载校验，确保产物合法",
    )
    args = parser.parse_args(argv)

    input_path = args.input
    if input_path == DEFAULT_INPUT and not input_path.exists():
        # Before the v2 dataset is merged into alibaba/aacr-bench main, the
        # meta URL is not resolvable yet. Use the repository copy when present.
        repository_copy = (
            Path(__file__).resolve().parents[2] / "dataset" / "v2" / "positive_samples.json"
        )
        if repository_copy.is_file():
            input_path = repository_copy
            print(f"[input] using repository copy: {repository_copy}")

    convert_file(input_path, args.output, args.limit, args.seed)

    if args.validate:
        instances = load_instances(args.output)
        print(f"校验通过：{len(instances)} 条标准格式样本可被 schema 正确加载")
    return 0


if __name__ == "__main__":
    sys.exit(main())