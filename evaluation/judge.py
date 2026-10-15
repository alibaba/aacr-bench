"""评测核心：参考评论与生成评论的匹配 + 指标统计 + 语义判定。

本模块自包含，不依赖框架外的任何资源。语义匹配支持两种模式：
- LLM 模式：调用裁判模型判断评论是否表达同一关注点 / 建议；
- Mock 模式：用本地文本相似度近似（零依赖、零成本，仅用于流程验证）。

配置变量名统一由 config 提供（JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL /
JUDGE_USE_MOCK），本模块直接读取，不再经过中间映射：
  JUDGE_USE_MOCK=true 或缺少 JUDGE_API_KEY  -> Mock 模式
  否则                                       -> LLM 模式（JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL）

两条匹配通道：
- evaluate_file_level_semantics（当前默认）：按文件分组，每条生成评论打包同文件
  全部参考评论调用一次裁判模型判断覆盖关系，支持一对多。
- evaluate_comments（Deprecated）：path -> side -> line(k) -> semantic 四阶段
  逐条匹配，仅在 --use-deprecated-score 时使用。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
from collections import defaultdict
from typing import Any, Dict, List

from typing_extensions import deprecated

import config

# 缺少 JUDGE_API_KEY 时自动启用本地 Mock 语义匹配，便于零配置验证流程
USE_MOCK_LLM = (
    os.getenv(config.JUDGE_USE_MOCK_VAR, "").lower() == "true"
    or not os.getenv(config.JUDGE_API_KEY_VAR)
)

# 语义匹配请求计数（含 Mock 与 LLM）
llm_request_count = 0

# 文件级匹配通道用 asyncio.gather 并发调用裁判模型，限制并发避免突发打满；
# 并发度由 JUDGE_CONCURRENCY 配置（默认 3），非法值回退默认
def _judge_concurrency() -> int:
    try:
        return max(1, int(os.getenv(config.JUDGE_CONCURRENCY_VAR, "3")))
    except ValueError:
        return 3


_llm_semaphore = asyncio.Semaphore(_judge_concurrency())

# LLM 客户端延迟初始化，避免 Mock 模式下强依赖网络配置
_llm_client = None


def _get_llm_client():
    global _llm_client
    if _llm_client is None:
        from openai import AsyncOpenAI

        _llm_client = AsyncOpenAI(
            base_url=os.getenv(config.JUDGE_BASE_URL_VAR),
            api_key=os.getenv(config.JUDGE_API_KEY_VAR),
        )
    return _llm_client


def _mock_semantic_match(reference_note: str, generated_note: str) -> bool:
    """本地近似语义匹配：综合序列相似度与词汇重叠度，不依赖外部 LLM。"""
    sequence_ratio = difflib.SequenceMatcher(
        None, reference_note.lower(), generated_note.lower()
    ).ratio()

    reference_words = set(re.findall(r"[a-zA-Z_]{3,}", reference_note.lower()))
    generated_words = set(re.findall(r"[a-zA-Z_]{3,}", generated_note.lower()))
    if reference_words and generated_words:
        jaccard = len(reference_words & generated_words) / len(reference_words | generated_words)
    else:
        jaccard = 0.0

    return sequence_ratio >= 0.4 or jaccard >= 0.3


@deprecated(
    "旧逐条匹配通道的判定函数，仅在 --use-deprecated-score 时使用；"
    "文件级通道用 match_llm_file_level"
)
async def match_semantic(reference_note: str, generated_note: str) -> Dict[str, Any]:
    """判断两条评论是否表达相同的关注点或建议。

    Deprecated: 旧逐条匹配通道的判定函数，仅被 evaluate_comments 使用；
    文件级通道的判定见 match_llm_file_level。
    """
    global llm_request_count
    llm_request_count += 1

    if USE_MOCK_LLM:
        is_similar = _mock_semantic_match(reference_note, generated_note)
        return {"is_similar": is_similar, "reason": f"[mock] matched={is_similar}"}

    prompt = (
        "-Role-\n"
        "You are an expert code reviewer assistant specialized in analyzing and "
        "comparing code review comments.\n\n"
        "-Task-\n"
        "Determine whether two given review comments express the same concern or "
        "suggestion. Ignore differences in wording, tone, or formatting—focus solely "
        "on semantic equivalence of the underlying issue. If the core intent and "
        'technical substance are identical, answer "yes"; otherwise, answer "no".\n\n'
        f"Review Comment 1:\n{reference_note}\n\n"
        f"Review Comment 2:\n{generated_note}\n\n"
        "-Task-\n"
        "Determine whether the two review comments given above express the same concern or "
        "suggestion. Ignore differences in wording, tone, or formatting—focus "
        "solely on semantic equivalence of the underlying issue. If the core intent and "
        'technical substance are identical, answer "yes"; otherwise, answer "no".\n\n'
        "Your answer:"
    )

    try:
        response = await _get_llm_client().chat.completions.create(
            model=os.getenv(config.JUDGE_MODEL_VAR, "multiline_judge_model"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=40000,
            top_p=0.95,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        result_text = response.choices[0].message.content.strip().lower()
        is_similar = (
            "yes" in result_text
            or "similar" in result_text
            or "same" in result_text
            or "identical" in result_text
            or "equivalent" in result_text
        ) and ("no" not in result_text.split("yes")[0] if "yes" in result_text else True)
        return {"is_similar": is_similar, "reason": result_text}
    except Exception as error:  # noqa: BLE001 - 单条判定失败不应中断整体评测
        logging.error("裁判模型调用失败: %s", error)
        return {"is_similar": False, "reason": f"评估过程出错: {error}"}


def diff_location_is_same(
    from_line1: int, from_line2: int, to_line1: int, to_line2: int, k: int = 1
) -> bool:
    """判断两个行号范围是否重叠，或最小距离在 k 范围内。"""
    has_overlap = not (from_line1 > to_line2 or from_line2 < to_line1)
    if has_overlap:
        min_distance = 0
    else:
        min_distance = min(abs(from_line1 - to_line2), abs(from_line2 - to_line1))
    return min_distance <= k


def _normalize_path(path: str) -> str:
    """归一化路径，统一分隔符为 /。"""
    if not path:
        return ""
    return path.replace("\\/", "/").replace("\\", "/")


def _strip_thinking_tags(text: str) -> str:
    """去除评论中的思考过程标签（如 <details>...</details>）。"""
    if not text:
        return ""
    cleaned = re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


@deprecated(
    "旧逐条匹配通道，仅在 --use-deprecated-score 时使用，将在后续版本移除；"
    "请改用 evaluate_file_level_semantics"
)
async def evaluate_comments(
    reference_comments: List[Dict[str, Any]],
    generated_comments: List[Dict[str, Any]],
    k: int = 1,
) -> List[Dict[str, Any]]:
    """对单个样本的参考评论逐条评测。

    Deprecated: 旧逐条匹配通道，仅在 --use-deprecated-score 时使用，将在后续
    版本移除；当前默认使用 evaluate_file_level_semantics。

    遍历每条参考评论，在生成评论中按 path -> side -> line(k) -> semantic 依次过滤，
    原地写入参考评论的 line_match / semantic_match / matched_note 字段。

    去重：每条生成评论最多被行号匹配一次、语义匹配一次。
    """
    matched_gen_ids_by_line: set = set()
    matched_gen_ids_by_semantic: set = set()

    for ref_comment in reference_comments:
        if not isinstance(ref_comment, dict):
            continue
        # 本通道产出的字段由本通道初始化（含无 note 被跳过的评论），构造器不预置
        ref_comment["line_match"] = False
        ref_comment["semantic_match"] = False
        ref_comment["matched_note"] = ""
        ref_note = ref_comment.get("note", "")
        if not ref_note:
            continue
        ref_note_cleaned = _strip_thinking_tags(ref_note)
        if not ref_note_cleaned:
            continue

        ref_path = _normalize_path(ref_comment.get("path", ""))
        ref_side = ref_comment.get("side")
        ref_from_line = ref_comment.get("from_line")
        ref_to_line = ref_comment.get("to_line")

        line_matched = False
        semantic_matched = False
        matched_note_text = ""

        for gen_idx, gen_comment in enumerate(generated_comments):
            if not isinstance(gen_comment, dict):
                continue
            gen_note = gen_comment.get("note", "")
            if not gen_note:
                continue
            gen_note_cleaned = _strip_thinking_tags(gen_note)
            if not gen_note_cleaned:
                continue

            gen_path = _normalize_path(gen_comment.get("path", ""))
            gen_side = gen_comment.get("side")
            gen_from_line = gen_comment.get("from_line")
            gen_to_line = gen_comment.get("to_line")

            # 阶段 1：文件路径匹配
            if ref_path and gen_path and ref_path != gen_path:
                continue

            # 阶段 2：side 匹配
            if ref_side is not None and gen_side is not None and ref_side != gen_side:
                continue

            # 阶段 3：行号匹配 line(k)
            if all(x is not None for x in [ref_from_line, ref_to_line, gen_from_line, gen_to_line]):
                if not diff_location_is_same(
                    ref_from_line, ref_to_line, gen_from_line, gen_to_line, k
                ):
                    continue

            # 行号匹配成功（一条生成评论只能贡献一次行号匹配）
            if not line_matched and gen_idx not in matched_gen_ids_by_line:
                line_matched = True
                matched_gen_ids_by_line.add(gen_idx)

            # 已被语义匹配过的生成评论跳过
            if gen_idx in matched_gen_ids_by_semantic:
                continue

            # 阶段 4：语义匹配
            similarity = await match_semantic(ref_note_cleaned, gen_note_cleaned)
            if similarity.get("is_similar", False):
                semantic_matched = True
                matched_note_text = gen_note
                matched_gen_ids_by_semantic.add(gen_idx)
                break

        ref_comment["line_match"] = line_matched
        ref_comment["semantic_match"] = semantic_matched
        ref_comment["matched_note"] = matched_note_text

    return reference_comments


async def match_llm_file_level(
    gen_note: str,
    gen_lines: str,
    ref_notes: List[str],
    ref_lines: List[str],
    file_path: str,
) -> Dict[str, Any]:
    """文件级语义匹配：一条生成评论对同文件全部参考评论，返回覆盖的参考编号。

    返回 {"matched": [1-based 编号...], "reason": str}；Mock 模式下退化为
    同文件内逐对本地相似度判定，不调用 LLM。
    """
    global llm_request_count
    llm_request_count += 1

    if USE_MOCK_LLM:
        matched = [
            i + 1
            for i, ref_note in enumerate(ref_notes)
            if _mock_semantic_match(ref_note, gen_note)
        ]
        return {"matched": matched, "reason": f"[mock] matched={matched}"}

    numbered_refs = "\n".join(
        f"{i + 1}. [L{ref_lines[i]}] {note}" for i, note in enumerate(ref_notes)
    )

    prompt = f"""
-Role-

You are an expert code reviewer comparing review comments for semantic equivalence.

-Task-

Given a Generated Comment and a numbered list of Reference Comments (all from the same file: {file_path}), determine which Reference Comments express the same concern or suggestion as the Generated Comment.

Focus solely on semantic equivalence of the underlying issue — ignore differences in wording, tone, formatting, or exact line numbers. If the Generated Comment addresses the same core problem as a Reference Comment, include that Reference Comment's number.

-Generated Comment-
[L{gen_lines}] {gen_note}

-Reference Comments-
{numbered_refs}

-Output-

For each Reference Comment, briefly state whether it matches and why (one line each).
Then return a JSON array of matched numbers on the LAST line. If none match, return [].

Example:
1. Match — both flag the uninitialized variable risk
2. No match — ref discusses naming, generated discusses null check
3. Match — same concern about missing error handling

[1, 3]

Your answer:
"""

    async with _llm_semaphore:
        try:
            response = await _get_llm_client().chat.completions.create(
                model=os.getenv(config.JUDGE_MODEL_VAR, "file_level_judge_model"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=3000,
                top_p=0.95,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            result_text = (response.choices[0].message.content or "").strip()
            matches = re.findall(r"\[[\d,\s]*\]", result_text)
            if matches:
                indices = json.loads(matches[-1])
                valid_indices = [
                    i
                    for i in indices
                    if isinstance(i, int) and 1 <= i <= len(ref_notes)
                ]
                return {"matched": valid_indices, "reason": result_text}
            return {"matched": [], "reason": result_text}
        except Exception as error:  # noqa: BLE001 - 单条判定失败不应中断整体评测
            logging.error("文件级裁判模型调用失败: %s", error)
            return {"matched": [], "reason": f"评估过程出错: {error}"}


async def evaluate_file_level_semantics(
    reference_comments: List[Dict[str, Any]],
    generated_comments: List[Dict[str, Any]],
) -> tuple[int, int, List[Dict[str, Any]]]:
    """文件级语义匹配通道（当前默认指标体系）。

    按文件路径分组，每条生成评论打包同文件全部参考评论调用一次裁判模型判断
    覆盖关系，支持一对多（一条生成评论可覆盖多条参考评论）。原地写入参考评论
    的 file_semantic_match 字段。

    Returns:
        (被覆盖的参考评论总数（去重）, 命中未被覆盖 GT 的生成评论数,
        生成评论命中记录列表)。每条记录包含 note / path / from_line / to_line /
        matched_gt_indices / judge_reason / hit_new_gt，matched_gt_indices 指向
        reference_comments 的下标；hit_new_gt 为 False 表示其命中的 GT 已全部被
        此前（按生成顺序）的评论覆盖，属重复命中，不计入第二个返回值。落在无参考
        评论文件下的生成评论也会记录（matched_gt_indices 为空，不调用裁判模型）；
        无 note 或无 path 的生成评论不参与评测也不记录。
    """
    ref_by_path: Dict[str, List] = defaultdict(list)
    for idx, ref in enumerate(reference_comments):
        if not isinstance(ref, dict):
            continue
        ref_note = ref.get("note", "")
        if not ref_note:
            continue
        ref_note_cleaned = _strip_thinking_tags(ref_note)
        if not ref_note_cleaned:
            continue
        path = _normalize_path(ref.get("path", ""))
        if not path:
            continue
        from_line = ref.get("from_line")
        to_line = ref.get("to_line")
        line_str = f"{from_line}-{to_line}" if from_line and to_line else "?"
        ref_by_path[path].append((idx, ref_note_cleaned, line_str))

    gen_by_path: Dict[str, List] = defaultdict(list)
    for gen in generated_comments:
        if not isinstance(gen, dict):
            continue
        gen_note = gen.get("note", "")
        if not gen_note:
            continue
        gen_note_cleaned = _strip_thinking_tags(gen_note)
        if not gen_note_cleaned:
            continue
        path = _normalize_path(gen.get("path", ""))
        if not path:
            continue
        from_line = gen.get("from_line")
        to_line = gen.get("to_line")
        line_str = f"{from_line}-{to_line}" if from_line and to_line else "?"
        gen_by_path[path].append(
            {
                "note_cleaned": gen_note_cleaned,
                "line_str": line_str,
                "record": {
                    "note": gen_note,
                    "path": path,
                    "from_line": from_line,
                    "to_line": to_line,
                    "matched_gt_indices": [],
                    "judge_reason": "",
                },
            }
        )

    matched_ref_indices: set = set()

    async def _file_level_task(entry: Dict[str, Any], ref_list: List, path: str):
        result = await match_llm_file_level(
            entry["note_cleaned"],
            entry["line_str"],
            [note for _, note, _ in ref_list],
            [lines for _, _, lines in ref_list],
            path,
        )
        # LLM 返回的编号是 ref_list 内的 1-based 序号，映射回 reference_comments 下标
        matched = [ref_list[num - 1][0] for num in result["matched"]]
        return entry["record"], matched, str(result.get("reason", ""))

    tasks = []
    for path, ref_list in ref_by_path.items():
        for entry in gen_by_path.get(path, []):
            tasks.append(_file_level_task(entry, ref_list, path))

    for record, matched, reason in await asyncio.gather(*tasks):
        record["matched_gt_indices"] = matched
        record["judge_reason"] = reason[:500]
        matched_ref_indices.update(matched)

    for idx, ref in enumerate(reference_comments):
        if isinstance(ref, dict):
            ref["file_semantic_match"] = idx in matched_ref_indices

    gen_records = [
        entry["record"] for entries in gen_by_path.values() for entry in entries
    ]

    # 贪心去重：按生成顺序，评论命中的 GT 若已全部被此前评论覆盖则为重复命中，
    # 不计入命中数（precision 分子），避免重复命中虚高
    covered_gt_indices: set = set()
    matched_generated_count = 0
    for record in gen_records:
        new_hits = set(record["matched_gt_indices"]) - covered_gt_indices
        record["hit_new_gt"] = bool(new_hits)
        if new_hits:
            matched_generated_count += 1
            covered_gt_indices.update(record["matched_gt_indices"])

    return len(matched_ref_indices), matched_generated_count, gen_records


def compute_cr_statistics(
    comments: List[Dict[str, Any]],
    generated_count: int,
    matched_generated_count: int = 0,
) -> Dict[str, Any]:
    """根据评测后的 comments 计算单个样本的统计指标。

    file_semantic_* 为当前默认指标（文件级语义匹配）：准确率 = 命中未被覆盖 GT 的
    生成评论数 / 生成评论总数（重复命中不计，分子由 matched_generated_count 传入），
    召回率 = 被覆盖的 GT 数 / GT 总数（file_semantic_match_count）。
    Deprecated: line_* / semantic_* 字段属于旧逐条匹配指标，仅在
    --use-deprecated-score 模式下由 evaluate_comments 写入后才有意义。
    """
    total_expected = len([c for c in comments if isinstance(c, dict) and c.get("note")])
    line_match_count = sum(
        1 for c in comments if isinstance(c, dict) and c.get("line_match", False)
    )
    semantic_match_count = sum(
        1 for c in comments if isinstance(c, dict) and c.get("semantic_match", False)
    )
    file_semantic_match_count = sum(
        1
        for c in comments
        if isinstance(c, dict) and c.get("file_semantic_match", False)
    )
    return {
        "expected_notes": total_expected,
        "generated_notes": generated_count,
        "line_match_count": line_match_count,
        "semantic_match_count": semantic_match_count,
        "file_semantic_match_count": file_semantic_match_count,
        "line_match_rate": round(line_match_count / generated_count, 3) if generated_count else 0.0,
        "semantic_match_rate": round(semantic_match_count / generated_count, 3)
        if generated_count
        else 0.0,
        "file_semantic_match_rate": round(matched_generated_count / generated_count, 3)
        if generated_count
        else 0.0,
        "line_recall_rate": round(line_match_count / total_expected, 3) if total_expected else 0.0,
        "semantic_recall_rate": round(semantic_match_count / total_expected, 3)
        if total_expected
        else 0.0,
        "file_semantic_recall_rate": round(file_semantic_match_count / total_expected, 3)
        if total_expected
        else 0.0,
    }


def print_llm_request_statistics() -> None:
    """打印语义匹配请求统计信息。"""
    print("\n" + "=" * 50)
    print("语义匹配请求统计")
    print("=" * 50)
    print(f"模式: {'Mock（本地相似度）' if USE_MOCK_LLM else 'LLM（真实裁判模型）'}")
    print(f"总请求数: {llm_request_count}")
    print("=" * 50 + "\n")
