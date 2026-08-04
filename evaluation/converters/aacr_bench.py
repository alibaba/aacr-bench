"""AACR-Bench JSON -> 统一框架标准格式 的转换器。

读取原始数据，逐条产出符合 schema.py 的 ReviewInstance，再写成标准 JSONL。

AACR-Bench 原始字段映射：
  githubPrUrl (github.com/<owner>/<name>/pull/N) -> repo（owner/name）
  source_commit                                  -> base_commit
  target_commit                                  -> head_commit
  owner__name@<target前7位>                       -> instance_id（无现成 id，按 repo/commit 推导）
  comments[]                                      -> reference_comments[]（AI / 人工评论全部保留）
    path               -> path
    note               -> text
    from_line/to_line  -> start_line / end_line（行号闭区间）
    side               -> side（"left" / "right"，评论附着在 diff 的哪一侧）

原始数据约定：放在 benchmark/AACR-Bench/ 下；转换产物按 benchmark 命名为 data/aacr_bench.jsonl。
不传 --input / --output 时即采用该约定，无需手填长路径。
约定输入缺失时，按同名 positive_samples.meta.json 描述从远端拉取并校验后缓存到本地。

用法（在 evaluation/ 目录内、已激活 venv）：
    python -m converters.aacr_bench [--limit 30] [--validate]
    # 或显式指定：
    python -m converters.aacr_bench \
        --input benchmark/AACR-Bench/positive_samples.json \
        --output data/aacr_bench.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# 允许以脚本方式直接运行：把 evaluation/ 加入 sys.path，使顶层模块可被导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from schema import ReferenceComment, ReviewInstance, load_instances  # noqa: E402

# 本 benchmark 的约定：原始目录名 + 标准数据集 key
BENCHMARK_NAME = "AACR-Bench"
BENCHMARK_KEY = "aacr_bench"
DEFAULT_INPUT = config.benchmark_raw_dir(BENCHMARK_NAME) / "positive_samples.json"
DEFAULT_OUTPUT = config.dataset_path(BENCHMARK_KEY)

# 从 githubPrUrl 解析 owner/name，如 https://github.com/FreeCAD/FreeCAD/pull/19411 -> FreeCAD/FreeCAD
_GITHUB_PR_PATTERN = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/\d+", re.IGNORECASE)


def _load_meta(meta_path: Path) -> Dict[str, Any]:
    """读取同名 .meta.json；文件不存在返回空 dict，解析失败报错退出。"""
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise SystemExit(f"元数据文件 JSON 解析失败 {meta_path}: {error}")


def _sha256(path: Path) -> str:
    """计算文件 sha256（分块读取，避免大文件一次性载入内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_raw_file(input_path: Path) -> Path:
    """按同名 .meta.json 描述拉取并校验原始数据；返回数据文件的实际路径。

    通过 input_path 的 stem 定位同名 .meta.json；meta 的 filename 字段（可选）是
    数据文件名的权威来源，缺省时回退到 input_path.name —— 据此支持「meta 文件名 ≠
    数据文件名」的场景。其余字段：url（下载地址，仅限 https://）、sha256（完整性校验，可选）。

    本地命中且校验通过（或 meta 无 sha256）即直接复用；否则按 url 下载到目标路径
    （分块流式写盘，先落临时文件再 os.replace 原子替换，避免中断留下半截文件被当成合法缓存），
    再按 sha256 校验完整性。无 meta 且文件缺失时报错退出。
    """
    input_path = Path(input_path)
    meta_path = input_path.with_name(f"{input_path.stem}.meta.json")
    meta = _load_meta(meta_path)

    data_path = input_path.with_name(meta.get("filename") or input_path.name)

    expected_sha = meta.get("sha256")
    if data_path.exists() and (expected_sha is None or _sha256(data_path) == expected_sha):
        return data_path

    url = meta.get("url")
    if not url:
        if data_path.exists():
            return data_path
        raise SystemExit(
            f"输入文件不存在且无元数据可拉取: {data_path}\n"
            f"期望同名 .meta.json: {meta_path}"
        )

    if not url.startswith("https://"):
        raise SystemExit(
            f"仅支持 https:// 下载地址，得到: {url}\n  meta: {meta_path}"
        )

    data_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] 本地缺失或校验未通过 {data_path}，从远端下载：\n  {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            _stream_to_file(response, data_path)
    except urllib.error.HTTPError as error:
        raise SystemExit(
            f"下载失败：HTTP {error.code} {error.reason}\n  URL: {url}"
        )
    except Exception as error:
        raise SystemExit(
            f"下载原始数据失败：{error}\n  URL: {url}\n"
            f"请检查网络，或手动下载数据放置到 {data_path} 后重试。"
        )

    if expected_sha is not None:
        actual_sha = _sha256(data_path)
        if actual_sha != expected_sha:
            raise SystemExit(
                f"下载内容校验失败：\n  期望 sha256={expected_sha}\n"
                f"  实际 sha256={actual_sha}\n  URL: {url}"
            )

    return data_path


def _stream_to_file(response: Any, target: Path) -> None:
    """分块流式写盘：下载到临时文件，完成后 os.replace 原子替换为目标文件。

    urlopen 在 4xx/5xx 时抛 HTTPError（由调用方捕获），走到这里的状态必为 2xx；
    分块读取与 _sha256 一致，避免大文件一次性载入内存。任何异常下清理临时文件。
    """
    tmp_path = target.with_name(f"{target.name}.part")
    try:
        with tmp_path.open("wb") as handle:
            for chunk in iter(lambda: response.read(1 << 16), b""):
                handle.write(chunk)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def parse_repo_from_pr_url(pr_url: Any) -> Optional[str]:
    """从 githubPrUrl 解析出 owner/name；解析失败返回 None。"""
    if not isinstance(pr_url, str):
        return None
    match = _GITHUB_PR_PATTERN.search(pr_url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def convert_record(record: Dict[str, Any]) -> Optional[ReviewInstance]:
    """把一条 AACR-Bench 记录转换为标准 ReviewInstance；缺关键字段则返回 None。"""
    if not isinstance(record, dict):
        return None

    repo = parse_repo_from_pr_url(record.get("githubPrUrl"))
    base_commit = record.get("source_commit")
    head_commit = record.get("target_commit")
    # instance_id：repo 的 / 换成 __，head_commit 取前 7 位短哈希
    instance_id = (
        f"{repo.replace('/', '__')}@{str(head_commit)[:7]}"
        if repo and head_commit
        else None
    )

    if not all([instance_id, repo, base_commit, head_commit]):
        return None

    reference_comments: List[ReferenceComment] = []
    for raw in record.get("comments", []) or []:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        text = raw.get("note")
        if not path or not text or not str(text).strip():
            continue
        # AACR-Bench 用 from_line/to_line 标定参考评论的行号区间
        start_line = raw.get("from_line")
        end_line = raw.get("to_line")
        # AACR-Bench 用 side 标定评论附着在 diff 的哪一侧（"left" / "right"）
        side = raw.get("side")
        if side is not None:
            side = str(side).strip()
        reference_comments.append(
            ReferenceComment(
                path=str(path).strip(),
                text=str(text).strip(),
                start_line=start_line,
                end_line=end_line,
                side=side,
            )
        )

    return ReviewInstance(
        instance_id=str(instance_id).strip(),
        repo=str(repo).strip(),
        base_commit=str(base_commit).strip(),
        head_commit=str(head_commit).strip(),
        reference_comments=reference_comments,
    )


def convert_file(
    input_path: Path,
    output_path: Path,
    limit: Optional[int],
    seed: Optional[int] = None,
) -> int:
    """读取 AACR-Bench JSON 数组，先整体乱序再按 limit 截取，写出标准格式 JSONL。

    先打乱全部原始记录顺序，再应用 limit，确保抽样是随机的而非取前 N 条。
    seed 不为 None 时随机可复现（用于固定评测子集）。
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    input_path = ensure_raw_file(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        records = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"输入文件 JSON 解析失败: {error}")
    if not isinstance(records, list):
        raise SystemExit(
            f"AACR-Bench 原始数据应为 JSON 数组，实际为 {type(records).__name__}"
        )

    rng = random.Random(seed)
    rng.shuffle(records)
    if limit is not None:
        records = records[:limit]

    written = 0
    skipped = 0
    with output_path.open("w", encoding="utf-8") as writer:
        for record in records:
            instance = convert_record(record)
            if instance is None:
                skipped += 1
                continue
            writer.write(json.dumps(instance.to_dict(), ensure_ascii=False) + "\n")
            written += 1

    print(f"转换完成：写出 {written} 条，跳过 {skipped} 条 -> {output_path}")
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AACR-Bench JSON -> 标准格式转换器")
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"AACR-Bench 原始 JSON 路径（默认 {DEFAULT_INPUT}）",
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

    convert_file(args.input, args.output, args.limit, args.seed)

    if args.validate:
        instances = load_instances(args.output)
        print(f"校验通过：{len(instances)} 条标准格式样本可被 schema 正确加载")
    return 0


if __name__ == "__main__":
    sys.exit(main())
