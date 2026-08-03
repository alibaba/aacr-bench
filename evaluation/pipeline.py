"""统一编排 + CLI 入口：load -> review -> evaluate。

把数据加载、执行评审、执行评测三个阶段串成一条可配置的流水线，
通过 --stage 控制只跑某个阶段或全跑，通过 --reviewer 切换评审器。

在 evaluation/ 目录内运行：
    python -m pipeline run --stage all    --reviewer claude --dataset data/<benchmark>.jsonl
    python -m pipeline run --stage review --reviewer ocr    --dataset data/<benchmark>.jsonl --limit 5
    python -m pipeline run --stage eval   --reviewer claude --dataset data/<benchmark>.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config
import evaluate
from repo_utils import RepoError, log
from reviewers import claude as claude_reviewer
from reviewers import codex as codex_reviewer
from reviewers import ocr as ocr_reviewer
from schema import ReviewInstance, load_instances

try:
    from tqdm import tqdm
except ImportError:  # 进度条是可选增强，缺失时退化为无进度
    tqdm = None


def _group_instances_by_repo(
    instances: List[ReviewInstance],
) -> "OrderedDict[str, List[ReviewInstance]]":
    """按 repo 分组并保持各组首次出现的顺序。

    同一 repo 的样本共享同一个本地 clone 工作区（repo/<owner__name>），
    并发执行会因 checkout/clean 互相踩踏，因此必须分到同一组内串行执行。
    """
    groups: "OrderedDict[str, List[ReviewInstance]]" = OrderedDict()
    for instance in instances:
        groups.setdefault(instance.repo, []).append(instance)
    return groups


def _review_one_instance(
    instance: ReviewInstance,
    reviewer: str,
    repo_dir: Path,
    results_dir: Path,
    reviewer_env: Dict[str, str],
    timeout_minutes: int,
    preview: bool,
    ocr_command: str = "ocr",
    max_tools: int = 30,
    codex_home: Optional[Path] = None,
    on_done: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """评审单条样本，返回状态摘要；RepoError 转为 error 状态而非中断。

    on_done(instance_id) 在样本评审结束（无论成败）后调用一次，用于推进进度条。

    reviewer_env 同时承载 claude_env 与 codex_env（按 reviewer 解码）；
    codex_home 仅 codex 评审器需要（pipeline 在 review 阶段开始时一次性生成）。
    """
    try:
        if reviewer == "ocr":
            return ocr_reviewer.review_instance(
                instance=instance,
                repo_dir=repo_dir,
                results_dir=results_dir,
                timeout_minutes=timeout_minutes,
                preview=preview,
                ocr_command=ocr_command,
                max_tools=max_tools,
            )
        if reviewer == "codex":
            return codex_reviewer.review_instance(
                instance=instance,
                repo_dir=repo_dir,
                results_dir=results_dir,
                codex_env=reviewer_env,
                codex_home=codex_home,
                timeout_minutes=timeout_minutes,
                preview=preview,
            )
        return claude_reviewer.review_instance(
            instance=instance,
            repo_dir=repo_dir,
            results_dir=results_dir,
            claude_env=reviewer_env,
            timeout_minutes=timeout_minutes,
            preview=preview,
        )
    except RepoError as error:
        log(f"ERROR processing {instance.instance_id}: {error}")
        return {"instance_id": instance.instance_id, "status": "error", "error": str(error)}
    finally:
        if on_done is not None:
            on_done(instance.instance_id)


def run_review_stage(
    instances: List[ReviewInstance],
    reviewer: str,
    repo_dir: Path,
    results_dir: Path,
    timeout_minutes: int,
    preview: bool,
    concurrency: int = 1,
    ocr_command: str = "ocr",
    max_tools: int = 30,
) -> List[Dict[str, Any]]:
    """执行评审阶段：逐样本评审并落盘结果文件，返回每条状态摘要。

    并发模型：按 repo 分组，组间最多 concurrency 个并发，组内严格串行
    （同一 repo 共享本地工作区，不能并发）。concurrency<=1 时完全串行。
    """
    results_dir.mkdir(parents=True, exist_ok=True)

    reviewer_env: Dict[str, str] = {}
    codex_home: Optional[Path] = None
    if reviewer == "ocr":
        ocr_reviewer.ensure_ocr_installed(ocr_command)
        ocr_reviewer.check_env(preview)
    elif reviewer == "claude":
        claude_reviewer.ensure_claude_installed()
        if not preview:
            reviewer_env = claude_reviewer.resolve_claude_env()
            log(
                f"Using model={reviewer_env[config.CLAUDE_MODEL_VAR]} "
                f"via {reviewer_env[config.CLAUDE_URL_VAR]}"
            )
    elif reviewer == "codex":
        codex_reviewer.ensure_codex_installed()
        if not preview:
            reviewer_env = codex_reviewer.resolve_codex_env()
            # 生成临时 CODEX_HOME + config.toml（所有实例共享一份）
            codex_home = codex_reviewer.write_codex_config(reviewer_env)
            model_desc = reviewer_env.get(config.CODEX_MODEL_VAR, "(codex default)")
            log(f"Using model={model_desc}")
            log(f"CODEX_HOME = {codex_home}")
    else:
        raise ValueError(f"未知的 reviewer: {reviewer}")

    repo_groups = _group_instances_by_repo(instances)
    effective_concurrency = max(1, min(concurrency, len(repo_groups)))
    log(
        f"Reviewing {len(instances)} instance(s) in {len(repo_groups)} repo group(s), "
        f"concurrency={effective_concurrency}"
    )

    # 线程安全的进度条：每完成一条样本推进 1，tqdm 自动给出已用时 / ETA / 速率
    progress_bar = (
        tqdm(total=len(instances), desc=f"Reviewing[{reviewer}]", unit="inst")
        if tqdm is not None
        else None
    )
    progress_lock = threading.Lock()

    def on_instance_done(instance_id: str) -> None:
        if progress_bar is None:
            return
        with progress_lock:
            progress_bar.set_postfix_str(instance_id, refresh=False)
            progress_bar.update(1)

    def review_group(group: List[ReviewInstance]) -> List[Dict[str, Any]]:
        """串行评审同一 repo 的所有样本。"""
        group_summary: List[Dict[str, Any]] = []
        for instance in group:
            group_summary.append(
                _review_one_instance(
                    instance=instance,
                    reviewer=reviewer,
                    repo_dir=repo_dir,
                    results_dir=results_dir,
                    reviewer_env=reviewer_env,
                    timeout_minutes=timeout_minutes,
                    preview=preview,
                    ocr_command=ocr_command,
                    max_tools=max_tools,
                    codex_home=codex_home,
                    on_done=on_instance_done,
                )
            )
        return group_summary

    summary: List[Dict[str, Any]] = []
    try:
        if effective_concurrency == 1:
            for group in repo_groups.values():
                summary.extend(review_group(group))
        else:
            with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
                futures = {
                    executor.submit(review_group, group): repo
                    for repo, group in repo_groups.items()
                }
                for future in as_completed(futures):
                    repo = futures[future]
                    try:
                        summary.extend(future.result())
                    except Exception as error:  # noqa: BLE001 - 单个 repo 组失败不影响其它组
                        log(f"ERROR reviewing repo group {repo}: {error}")
    finally:
        if progress_bar is not None:
            progress_bar.close()
        # codex：清理临时 CODEX_HOME（codex 会在其中存 sessions/auth/hooks 状态）
        if reviewer == "codex" and codex_home is not None:
            codex_reviewer.cleanup_codex_home(codex_home)

    summary_path = results_dir / f"summary_{reviewer}.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    succeeded = sum(1 for item in summary if item.get("status") in ("ok", "preview"))
    log(f"Review done. {succeeded}/{len(summary)} succeeded. Summary -> {summary_path}")
    return summary


def run_eval_stage(
    instances: List[ReviewInstance],
    reviewer: str,
    results_dir: Path,
    metrics_dir: Path,
    line_k: int,
    limit_for_eval: int | None,
    eval_rounds: int = 1,
) -> Dict[str, Any]:
    """执行评测阶段：可多轮评测后求平均指标。

    eval_rounds > 1 时，每轮独立调用 judge 并落盘各自的 metrics 文件，
    最后计算各轮 summary 的平均值并额外写一份 metrics_<reviewer>_average.json。
    控制台打印的汇总为平均值。
    """
    log(f"语义裁判模式: {evaluate.judge_mode_description()}")
    if eval_rounds > 1:
        log(f"评测轮数: {eval_rounds}（各轮独立评测后取平均）")

    all_results: List[Dict[str, Any]] = []
    for round_index in range(eval_rounds):
        if eval_rounds > 1:
            log(f"--- 评测第 {round_index + 1}/{eval_rounds} 轮 ---")
        round_label = f"_round_{round_index + 1}" if eval_rounds > 1 else ""
        result_data = asyncio.run(
            evaluate.evaluate(
                instances=instances,
                results_dir=results_dir,
                reviewer=reviewer,
                output_dir=metrics_dir,
                line_k=line_k,
                limit=limit_for_eval,
                round_label=round_label,
            )
        )
        all_results.append(result_data)

    if eval_rounds == 1:
        evaluate.print_summary(reviewer, all_results[0])
        return all_results[0]

    # 多轮：计算平均 summary 并落盘
    average_summary = _average_summaries([r.get("summary", {}) for r in all_results])
    average_data = {"summary": average_summary, "eval_rounds": eval_rounds}
    avg_path = Path(metrics_dir) / f"metrics_{reviewer}_average.json"
    with avg_path.open("w", encoding="utf-8") as handle:
        json.dump(average_data, handle, ensure_ascii=False, indent=2)
    log(f"多轮平均指标已保存到: {avg_path}")
    _print_average_summary(reviewer, average_summary, eval_rounds)
    return average_data


def _average_summaries(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对多轮 summary 的数值字段求平均。"""
    if not summaries:
        return {}
    numeric_keys = [
        key for key, value in summaries[0].items() if isinstance(value, (int, float))
    ]
    averaged = {}
    for key in numeric_keys:
        values = [s.get(key, 0) for s in summaries]
        averaged[key] = round(sum(values) / len(values), 4)
    return averaged


def _print_average_summary(reviewer: str, summary: Dict[str, Any], rounds: int) -> None:
    """打印多轮平均汇总。"""
    print("\n" + "=" * 56)
    print(f"评测汇总（{reviewer}）— {rounds} 轮平均")
    print("=" * 56)
    print(f"语义 Precision:  {summary.get('semantic_match_rate')}")
    print(f"语义 Recall:     {summary.get('semantic_recall_rate')}")
    print(f"语义 F1:         {summary.get('semantic_f1')}")
    print(f"行号 Precision:  {summary.get('line_match_rate')}")
    print(f"行号 Recall:     {summary.get('line_recall_rate')}")
    print(f"行号 F1:         {summary.get('line_f1')}")
    print("=" * 56)


def run_pipeline(args) -> int:
    """根据 CLI 参数编排各阶段。"""
    instances = load_instances(args.dataset, args.limit)
    log(f"Loaded {len(instances)} instance(s) from {args.dataset}")
    if not instances:
        log("No instances to process.")
        return 0

    if args.stage in ("all", "review"):
        run_review_stage(
            instances=instances,
            reviewer=args.reviewer,
            repo_dir=args.repo_dir,
            results_dir=args.results_dir,
            timeout_minutes=args.timeout_minutes,
            preview=args.preview,
            concurrency=args.concurrency,
            ocr_command=args.ocr_command,
            max_tools=args.max_tools,
        )

    if args.stage in ("all", "eval"):
        run_eval_stage(
            instances=instances,
            reviewer=args.reviewer,
            results_dir=args.results_dir,
            metrics_dir=args.metrics_dir,
            line_k=args.line_k,
            limit_for_eval=None,
            eval_rounds=args.eval_rounds,
        )

    if args.stage == "eval":
        evaluate.print_llm_request_statistics()

    return 0


def _path_arg(value: str) -> Path:
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="统一代码审查评测框架（数据加载 / 执行评审 / 执行评测）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行评审 / 评测流水线")
    run_parser.add_argument(
        "--stage",
        choices=["all", "review", "eval"],
        default="all",
        help="执行哪个阶段：all（评审+评测）/ review（仅评审）/ eval（仅评测）",
    )
    run_parser.add_argument(
        "--reviewer",
        choices=["ocr", "claude", "codex"],
        required=True,
        help="使用哪个评审器",
    )
    run_parser.add_argument(
        "--dataset",
        type=_path_arg,
        required=True,
        help="标准格式数据集（JSONL）路径，如 data/<benchmark>.jsonl",
    )
    run_parser.add_argument(
        "--repo-dir",
        type=_path_arg,
        default=config.DEFAULT_REPO_DIR,
        help="仓库 clone 目录",
    )
    run_parser.add_argument(
        "--results-dir",
        type=_path_arg,
        default=None,
        help=argparse.SUPPRESS,  # 高级用法，直接指定目录，绕过 run 机制
    )
    run_parser.add_argument(
        "--run-id",
        default=None,
        help="本次 run 的目录名（如 baseline、v2）；"
        "review 不传则自动用时间戳命名；"
        "eval 阶段必须显式指定（用于定位要评测的 run 目录）",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只处理标准数据顺序的前 N 条样本",
    )
    run_parser.add_argument(
        "--ocr-command",
        default="ocr",
        help="OCR 评审器可执行文件路径（默认 ocr）；可指定本地 release 包路径，如 ./ocr-v2.0.0",
    )
    run_parser.add_argument(
        "--max-tools",
        type=int,
        default=30,
        help="OCR 每个文件最大工具调用轮数（默认 30）；仅 ocr 评审器有效",
    )
    run_parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=None,
        help="单条样本评审超时（分钟）；缺省 ocr=10 / claude=20 / codex=20",
    )
    run_parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="评审阶段并发的 repo 组数（默认 1=完全串行）；"
        "按 repo 分组，组间并发、组内串行（同一 repo 共享工作区不能并发）",
    )
    run_parser.add_argument("--line-k", type=int, default=1, help="评测行号匹配容差 k")
    run_parser.add_argument(
        "--eval-rounds",
        type=int,
        default=1,
        help="评测阶段执行轮数（默认 1）；多轮时各轮独立评测后取数值平均作为最终指标",
    )
    run_parser.add_argument(
        "--preview",
        action="store_true",
        help="预览模式：只 clone/checkout，不真正调用评审 LLM",
    )
    return parser


def apply_defaults(args: argparse.Namespace) -> None:
    """补齐依赖于 reviewer / dataset 的默认值。

    产物目录采用 run 机制，多次评测互不覆盖：
      results/<benchmark>/<reviewer>/<run_id>/   # 评审结果
      metrics/<benchmark>/<reviewer>/<run_id>/   # 评测指标

    --run-id 一个参数贯穿 review 和 eval：
      - 传了 --run-id：review 创建该目录，eval 读取该目录
      - 不传：review 自动用时间戳命名；eval 阶段必须显式指定
    """
    if args.timeout_minutes is None:
        args.timeout_minutes = 30
    if args.concurrency < 1:
        args.concurrency = 1

    benchmark_name = config.benchmark_name_from_dataset(args.dataset)

    if args.results_dir is None:
        run_id, run_path = _resolve_run_dir(args, benchmark_name)
        args.results_dir = run_path
        args.metrics_dir = config.metrics_run_dir(benchmark_name, args.reviewer, run_id)
    else:
        # --results-dir 显式覆盖：metrics 也放同目录（用户自行管理）
        args.metrics_dir = args.results_dir


def _resolve_run_dir(args: argparse.Namespace, benchmark_name: str) -> tuple:
    """根据 --run-id 和 stage 确定 run 目录，返回 (run_id, run_path)。"""
    has_review = args.stage in ("all", "review")

    if has_review:
        run_id = config.make_run_id(args.run_id)
        run_path = config.run_dir(benchmark_name, args.reviewer, run_id)
        log(f"Run directory: {run_path}")
        return run_id, run_path

    # 仅 eval 阶段：必须显式指定 --run-id
    if not args.run_id:
        raise SystemExit(
            "仅 eval 阶段时必须通过 --run-id 指定要评测的 run 目录名，"
            "例如：--run-id baseline"
        )
    run_id = args.run_id
    run_path = config.run_dir(benchmark_name, args.reviewer, run_id)
    if not run_path.is_dir():
        raise SystemExit(f"指定的 run 目录不存在: {run_path}")
    log(f"Evaluating run: {run_path}")
    return run_id, run_path


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # 静音 httpx/openai 每次成功请求的 INFO 日志，仅出错时打印
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        apply_defaults(args)
        return run_pipeline(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
