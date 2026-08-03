"""评测阶段：把标准参考数据与评审结果逐样本比对，计算 P/R/F1。

匹配与指标统计由本地的 judge 模块完成（path -> side -> line(k) -> semantic 四阶段）。

裁判模型（LLM as a Judge）与 OCR/Claude 解耦：在 evaluation/.env 用
JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL / JUDGE_USE_MOCK 配置，
judge 模块直接读取这些（由 config 统一定义的）变量名，本模块只负责在
import judge 之前加载 .env 使它们就位。

行号统一规则（便于统一比对）：
- 参考：标准格式已是闭区间 [start_line, end_line]。
- Claude：单个 line 作为起始=终止。
- OCR：start_line / end_line 直接作为起始 / 终止。
- Codex：start_line / end_line 直接作为起始 / 终止（与 OCR 同构）。

note（语义比对文本）映射：
- 参考：text
- OCR：content
- Claude：summary + "\n" + failure_scenario
- Codex：summary + "\n" + description
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from schema import ReviewInstance


def _bootstrap_judge_env() -> None:
    """在 import judge 之前加载 evaluation/.env，使 JUDGE_* 变量就位。

    必须在 import judge 之前调用：judge 的 USE_MOCK_LLM 与 LLM 客户端在导入时
    就根据环境变量固化，导入后再设置将不再生效。judge 直接读取 config 定义的
    JUDGE_* 变量名，无需再做中间映射。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = config.EVALUATION_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)


_bootstrap_judge_env()

# 本地评测核心（在 Judge env 注入之后导入）
from judge import (  # noqa: E402
    USE_MOCK_LLM,
    compute_cr_statistics,
    evaluate_comments,
    print_llm_request_statistics,
)

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


def _normalize_line_range(
    from_line: Optional[int], to_line: Optional[int]
) -> Dict[str, Optional[int]]:
    if from_line is None and to_line is None:
        return {"from_line": None, "to_line": None}
    if from_line is None:
        from_line = to_line
    if to_line is None:
        to_line = from_line
    if from_line > to_line:
        from_line, to_line = to_line, from_line
    return {"from_line": from_line, "to_line": to_line}


def build_reference_comments(instance: ReviewInstance) -> List[Dict[str, Any]]:
    """标准参考评论 -> judge 模块需要的比对格式。"""
    comments = []
    for ref in instance.reference_comments:
        line_range = _normalize_line_range(ref.start_line, ref.end_line)
        comments.append(
            {
                "note": ref.text,
                "path": ref.path,
                "side": ref.side,
                "from_line": line_range["from_line"],
                "to_line": line_range["to_line"],
                "line_match": False,
                "semantic_match": False,
                "matched_note": "",
            }
        )
    return comments


def build_target_comments_from_claude(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Claude 结果 review_output -> 待评测评论；line 作为起始=终止。"""
    comments = []
    for finding in result.get("review_output", []) or []:
        if not isinstance(finding, dict):
            continue
        summary = (finding.get("summary") or "").strip()
        failure_scenario = (finding.get("failure_scenario") or "").strip()
        note = "\n".join(part for part in [summary, failure_scenario] if part).strip()
        if not note:
            continue
        line_range = _normalize_line_range(finding.get("line"), finding.get("line"))
        comments.append(
            {
                "note": note,
                "path": finding.get("file", ""),
                # 这三个评审系统均面向 diff 右侧（新代码）进行评审，side 硬编码为 "right"
                "side": "right",
                "from_line": line_range["from_line"],
                "to_line": line_range["to_line"],
            }
        )
    return comments


def build_target_comments_from_codex(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """codex 结果 review_output -> 待评测评论；start_line/end_line 直接用。

    与 Claude 版的差异：note 用 description（而非 failure_scenario）；
    行号读 start_line/end_line 闭区间（而非 line 单值）；severity 忽略（仅存档）。
    """
    comments = []
    for finding in result.get("review_output", []) or []:
        if not isinstance(finding, dict):
            continue
        summary = (finding.get("summary") or "").strip()
        description = (finding.get("description") or "").strip()
        note = "\n".join(part for part in [summary, description] if part).strip()
        if not note:
            continue
        line_range = _normalize_line_range(
            finding.get("start_line"), finding.get("end_line")
        )
        comments.append(
            {
                "note": note,
                "path": finding.get("file", ""),
                # 这三个评审系统均面向 diff 右侧（新代码）进行评审，side 硬编码为 "right"
                "side": "right",
                "from_line": line_range["from_line"],
                "to_line": line_range["to_line"],
            }
        )
    return comments


def build_target_comments_from_ocr(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """OCR 结果 review.comments -> 待评测评论；start_line/end_line 直接用。"""
    review = result.get("review")
    if not isinstance(review, dict):
        return []
    comments = []
    for finding in review.get("comments", []) or []:
        if not isinstance(finding, dict):
            continue
        note = (finding.get("content") or "").strip()
        if not note:
            continue
        line_range = _normalize_line_range(finding.get("start_line"), finding.get("end_line"))
        comments.append(
            {
                "note": note,
                "path": finding.get("path", ""),
                # 这三个评审系统均面向 diff 右侧（新代码）进行评审，side 硬编码为 "right"
                "side": "right",
                "from_line": line_range["from_line"],
                "to_line": line_range["to_line"],
            }
        )
    return comments


def load_target_comments(
    results_dir: Path, reviewer: str, instance_id: str
) -> Optional[List[Dict[str, Any]]]:
    """按 instance_id 精确定位结果文件并构建待评测评论；缺文件返回 None。"""
    path = config.result_path(results_dir, instance_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            result = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        logging.warning("跳过无法解析的结果文件 %s: %s", path.name, error)
        return None
    if not isinstance(result, dict):
        logging.warning("跳过结构异常的结果文件（顶层非对象）: %s", path.name)
        return None
    builder = {
        "claude": build_target_comments_from_claude,
        "codex": build_target_comments_from_codex,  # 独立实现，非别名
    }.get(reviewer, build_target_comments_from_ocr)
    return builder(result)


def _extract_usage_from_result(
    results_dir: Path, reviewer: str, instance_id: str,
) -> Optional[Dict[str, Any]]:
    """从结果文件中提取执行用时和 token 消耗量。

    Claude Code: duration_seconds, token_usage.{input_tokens, output_tokens,
                 cache_read_input_tokens, cache_creation_input_tokens}
                 input_tokens 不含缓存，total_input = 三者之和。
    Codex:       duration_seconds, token_usage.{input_tokens, output_tokens,
                 cached_input_tokens (=cache_read_input_tokens), reasoning_output_tokens}
                 input_tokens 已含 cached（OpenAI 口径），不可再叠 cache_read。
                 reasoning tokens 不累加到 total（与 Claude 不可比，仅存档）。
    OCR:         duration_seconds, review.summary.{input_tokens, output_tokens}
    """
    path = config.result_path(results_dir, instance_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    duration = data.get("duration_seconds", 0) or 0

    # claude 与 codex 共用 token_usage 字段命名，但缓存口径相反，必须分提：
    # - Claude（Anthropic 口径）：input_tokens 不含缓存，需把 cache_read +
    #   cache_creation 叠加进来才是完整输入。
    # - Codex（OpenAI 口径）：input_tokens 已含 cached（reviewers/codex.py 把
    #   cached_input_tokens 镜像成了 cache_read_input_tokens 仅作命名对齐），
    #   再加 cache_read 会把整个 cached 重复计一次。
    if reviewer == "claude":
        token_usage = data.get("token_usage") or {}
        input_tokens = (
            (token_usage.get("input_tokens", 0) or 0)
            + (token_usage.get("cache_read_input_tokens", 0) or 0)
            + (token_usage.get("cache_creation_input_tokens", 0) or 0)
        )
        output_tokens = token_usage.get("output_tokens", 0) or 0
    elif reviewer == "codex":
        token_usage = data.get("token_usage") or {}
        input_tokens = token_usage.get("input_tokens", 0) or 0
        output_tokens = token_usage.get("output_tokens", 0) or 0
    else:
        review = data.get("review") or {}
        summary = review.get("summary") or {}
        input_tokens = summary.get("input_tokens", 0) or 0
        output_tokens = summary.get("output_tokens", 0) or 0

    return {
        "duration_seconds": duration,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _compute_f1(precision: float, recall: float) -> float:
    if (precision + recall) <= 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 3)


def _compute_summary(stats: Dict[str, Any]) -> Dict[str, Any]:
    expected = stats["expected_notes"]
    generated = stats["generated_notes"]
    sem_p = round(stats["matched_semantic_notes"] / generated, 3) if generated else 0.0
    line_p = round(stats["matched_line_notes"] / generated, 3) if generated else 0.0
    sem_r = round(stats["matched_semantic_notes"] / expected, 3) if expected else 0.0
    line_r = round(stats["matched_line_notes"] / expected, 3) if expected else 0.0

    evaluated = stats["evaluated_instances"]
    total_duration = stats.get("total_duration_seconds", 0)
    total_input_tokens = stats.get("total_input_tokens", 0)
    total_output_tokens = stats.get("total_output_tokens", 0)

    return {
        "total_instances": stats["total_instances"],
        "candidate_instances": stats["candidate_instances"],
        "evaluated_instances": evaluated,
        "missing_instances": stats["missing_instances"],
        "expected_notes": expected,
        "generated_notes": generated,
        "matched_line_notes": stats["matched_line_notes"],
        "matched_semantic_notes": stats["matched_semantic_notes"],
        "semantic_match_rate": sem_p,
        "line_match_rate": line_p,
        "semantic_recall_rate": sem_r,
        "line_recall_rate": line_r,
        "semantic_f1": _compute_f1(sem_p, sem_r),
        "line_f1": _compute_f1(line_p, line_r),
        "total_duration_seconds": round(total_duration, 2),
        "avg_duration_seconds": round(total_duration / evaluated, 2) if evaluated else 0,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "avg_input_tokens": round(total_input_tokens / evaluated) if evaluated else 0,
        "avg_output_tokens": round(total_output_tokens / evaluated) if evaluated else 0,
    }


async def evaluate(
    instances: List[ReviewInstance],
    results_dir: Path,
    reviewer: str,
    output_dir: Path,
    line_k: int = 1,
    limit: Optional[int] = None,
    round_label: str = "",
) -> Dict[str, Any]:
    """对某个评审器的结果目录计算评测指标，返回并落盘结果数据。

    round_label: 多轮评测时传入如 "_round_1" 用于区分文件名；单轮为空串。
    """
    if not Path(results_dir).is_dir():
        raise FileNotFoundError(f"结果目录不存在: {results_dir}")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    candidate = instances[:limit] if limit is not None else instances

    stats: Dict[str, Any] = {
        "total_instances": len(instances),
        "candidate_instances": len(candidate),
        "evaluated_instances": 0,
        "missing_instances": 0,
        "expected_notes": 0,
        "generated_notes": 0,
        "matched_line_notes": 0,
        "matched_semantic_notes": 0,
        "total_duration_seconds": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }
    eval_results: List[Dict[str, Any]] = []
    missing_ids: List[str] = []

    for instance in tqdm(candidate, desc=f"评估 {reviewer}", unit="inst"):
        generated = load_target_comments(results_dir, reviewer, instance.instance_id)
        if generated is None:
            missing_ids.append(instance.instance_id)
            continue

        reference = build_reference_comments(instance)
        generated_count = len([c for c in generated if isinstance(c, dict) and c.get("note")])

        if reference:
            await evaluate_comments(reference, generated, k=line_k)

        cr_stats = compute_cr_statistics(reference, generated_count)
        stats["evaluated_instances"] += 1
        stats["expected_notes"] += cr_stats["expected_notes"]
        stats["generated_notes"] += generated_count
        stats["matched_line_notes"] += cr_stats["line_match_count"]
        stats["matched_semantic_notes"] += cr_stats["semantic_match_count"]

        # 累计执行用时和 token 消耗
        usage = _extract_usage_from_result(results_dir, reviewer, instance.instance_id)
        if usage:
            stats["total_duration_seconds"] += usage["duration_seconds"]
            stats["total_input_tokens"] += usage["input_tokens"]
            stats["total_output_tokens"] += usage["output_tokens"]

        eval_results.append(
            {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "comments": reference,
            }
        )

    stats["missing_instances"] = len(missing_ids)
    if missing_ids:
        logging.info(
            "[%s] 候选 %d 条中有 %d 条在结果目录无对应文件（已跳过，不计入指标）",
            reviewer,
            len(candidate),
            len(missing_ids),
        )

    summary = _compute_summary(stats)
    result_data = {
        "ex_info": {
            "start_time": datetime.now().isoformat(),
            "results_dir": str(results_dir),
            "reviewer": reviewer,
            "line_k": line_k,
            "limit": limit,
            "missing_instance_ids": missing_ids,
        },
        "summary": summary,
        "eval_res": eval_results,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(output_dir) / f"metrics_{reviewer}_{timestamp}{round_label}.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(result_data, handle, ensure_ascii=False, indent=2)
    print(f"\n评测结果已保存到: {out_path}")
    return result_data


def print_summary(reviewer: str, result_data: Dict[str, Any]) -> None:
    summary = result_data.get("summary", {})
    print("\n" + "=" * 56)
    print(f"评测汇总（{reviewer}）")
    print("=" * 56)
    print(f"数据集总样本 (total_instances):     {summary.get('total_instances')}")
    print(f"候选样本数   (candidate_instances): {summary.get('candidate_instances')}")
    print(f"实际评测样本 (evaluated_instances): {summary.get('evaluated_instances')}")
    print(f"缺失样本数   (missing_instances):   {summary.get('missing_instances')}")
    print(f"参考评论总数 (expected_notes):      {summary.get('expected_notes')}")
    print(f"生成评论总数 (generated_notes):     {summary.get('generated_notes')}")
    print(f"语义匹配数:                         {summary.get('matched_semantic_notes')}")
    print(f"行号匹配数:                         {summary.get('matched_line_notes')}")
    print("-" * 56)
    print(f"语义 Precision:  {summary.get('semantic_match_rate')}")
    print(f"语义 Recall:     {summary.get('semantic_recall_rate')}")
    print(f"语义 F1:         {summary.get('semantic_f1')}")
    print(f"行号 Precision:  {summary.get('line_match_rate')}")
    print(f"行号 Recall:     {summary.get('line_recall_rate')}")
    print(f"行号 F1:         {summary.get('line_f1')}")
    print("-" * 56)
    total_dur = summary.get("total_duration_seconds", 0)
    avg_dur = summary.get("avg_duration_seconds", 0)
    print(f"总执行用时:      {total_dur}s ({total_dur/60:.1f}min)")
    print(f"平均执行用时:    {avg_dur}s ({avg_dur/60:.1f}min)")
    print(f"总输入 token:    {summary.get('total_input_tokens', 0):,}")
    print(f"总输出 token:    {summary.get('total_output_tokens', 0):,}")
    print(f"总 token:        {summary.get('total_tokens', 0):,}")
    print(f"平均输入 token:  {summary.get('avg_input_tokens', 0):,}")
    print(f"平均输出 token:  {summary.get('avg_output_tokens', 0):,}")
    print("=" * 56)


def judge_mode_description() -> str:
    if USE_MOCK_LLM:
        return "Mock（本地相似度）"
    return f"LLM（真实裁判模型）| 模型: {os.getenv(config.JUDGE_MODEL_VAR, '')}"