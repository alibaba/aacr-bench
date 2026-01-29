"""
Evaluator Module

Provides core functionality for code review comment quality evaluation.
"""
from typing import Any, Dict, List, Tuple, Optional
from dataclasses import dataclass, field
import logging
import re

from evaluator_runner.utils.config import EvaluatorConfig, SemanticMatcherType
from evaluator_runner.core.matcher_factory import get_semantic_matcher, SemanticMatchFunc
from evaluator_runner.core.match_location import (
    extract_comment_location,
    match_location,
    CommentLocation
)

def parse_github_pr_url(url: str) -> Dict[str, str]:
    """Parse repository name and PR number from GitHub PR URL"""
    pattern = r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, url)
    if match:
        return {
            "owner": match.group(1),
            "repo": match.group(2),
            "pr_number": match.group(3)
        }
    return {"owner": "", "repo": "", "pr_number": ""}

def get_evaluation_id(github_pr_url: str) -> str:
    """Generate evaluation ID from GitHub PR URL"""
    parsed = parse_github_pr_url(github_pr_url)
    return f"{parsed['repo']}_{parsed['pr_number']}"

@dataclass
class MatchRecord:
    """Match record for a single comment"""
    generated_comment_index: int
    generated_comment: str
    generated_location: Dict[str, Any]
    line_match: bool = False
    semantic_match: bool = False
    matched_reference_id: Optional[str] = None
    matched_reference_note: Optional[str] = None
    matched_reference_location: Optional[Dict[str, Any]] = None
    matched_reference_details: Dict[str, Any] = field(default_factory=dict)
    location_match_details: Dict[str, Any] = field(default_factory=dict)
    llm_comparison: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_comment_index": self.generated_comment_index,
            "generated_comment": self.generated_comment,
            "generated_location": self.generated_location,
            "line_match": self.line_match,
            "semantic_match": self.semantic_match,
            "matched_reference_id": self.matched_reference_id,
            "matched_reference_note": self.matched_reference_note,
            "matched_reference_location": self.matched_reference_location,
            "matched_reference_details": self.matched_reference_details,
            "location_match_details": self.location_match_details,
            "llm_comparison": self.llm_comparison
        }

@dataclass
class MatchStatistics:
    """Match statistics result"""
    positive_matches: int = 0
    positive_line_matches: int = 0
    unmatched_count: int = 0
    total_generated: int = 0
    total_good: int = 0
    match_details: List[Dict[str, Any]] = field(default_factory=list)

def _extract_reference_details(comment: Dict[str, Any]) -> Dict[str, Any]:
    """Extract reference comment details"""
    return {
        "from_line": comment.get("from_line"),
        "to_line": comment.get("to_line"),
        "category": comment.get("category"),
        "context": comment.get("context"),
        "source_model": comment.get("source_model"),
        "is_ai_comment": comment.get("is_ai_comment")
    }

def _calculate_rate(numerator: int, denominator: int) -> float:
    """Calculate rate"""
    return numerator / denominator if denominator > 0 else 0.0

def _count_valid_comments(comments: List[Dict[str, Any]]) -> int:
    """Count valid comments"""
    return len([c for c in comments if isinstance(c, dict) and c.get("note")])

def parse_generated_comments_file(file_content: str) -> List[Dict[str, Any]]:
    """
    Parse generated comments file (custom tag format).

    Args:
        file_content: File content

    Returns:
        List of comments with path, side, from_line, to_line, note
    """
    comments = []

    if not file_content or not file_content.strip():
        return comments

    blocks = re.split(r'</?notesplit\s*/?>', file_content, flags=re.IGNORECASE)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        try:
            path_match = re.search(r'<path>(.*?)</path>', block, re.DOTALL)
            side_match = re.search(r'<side>(.*?)</side>', block, re.DOTALL)
            from_match = re.search(r'<from>(.*?)</from>', block, re.DOTALL)
            to_match = re.search(r'<to>(.*?)</to>', block, re.DOTALL)
            note_match = re.search(r'<note>(.*?)</note>', block, re.DOTALL)

            if note_match and note_match.group(1).strip():
                from_line = None
                to_line = None

                if from_match:
                    try:
                        from_line = int(from_match.group(1).strip())
                    except ValueError:
                        pass

                if to_match:
                    try:
                        to_line = int(to_match.group(1).strip())
                    except ValueError:
                        pass

                comment = {
                    "path": path_match.group(1).strip() if path_match else "",
                    "side": side_match.group(1).strip() if side_match else "",
                    "from_line": from_line,
                    "to_line": to_line,
                    "note": note_match.group(1).strip()
                }
                comments.append(comment)
        except Exception as e:
            logging.warning(f"Failed to parse comment block: {e}")
            continue

    return comments

def load_generated_comments_from_file(file_path: str) -> List[Dict[str, Any]]:
    """Load generated comments from file"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return parse_generated_comments_file(content)

async def _try_match_with_reference(
        gen_note: str,
        gen_loc: CommentLocation,
        good_comment: Dict[str, Any],
        line_distance_threshold: int,
        matched_good_ids: set,
        matched_good_ids_by_line: set,
        match_record: MatchRecord,
        semantic_match_func: Optional[SemanticMatchFunc] = None
) -> Tuple[bool, bool]:
    """Try to match generated comment with a reference comment"""
    comment_id = good_comment.get("id")
    reference_note = good_comment.get("note", "")

    if not reference_note:
        return False, False

    ref_loc = extract_comment_location(good_comment, is_generated=False)
    location_result = match_location(gen_loc, ref_loc, comment_id, line_distance_threshold)

    if not location_result.is_match:
        return False, False

    line_matched = False
    semantic_matched = False

    if comment_id not in matched_good_ids_by_line:
        matched_good_ids_by_line.add(comment_id)
        line_matched = True

        match_record.line_match = True
        match_record.matched_reference_id = comment_id
        match_record.matched_reference_note = reference_note
        match_record.matched_reference_location = ref_loc.to_dict()
        match_record.matched_reference_details = _extract_reference_details(good_comment)
        match_record.location_match_details = location_result.to_dict()["details"]

    if semantic_match_func is not None and comment_id not in matched_good_ids:
        similarity_result = await semantic_match_func(gen_note, reference_note)

        if similarity_result.get("is_similar", False):
            matched_good_ids.add(comment_id)
            semantic_matched = True

            match_record.semantic_match = True
            match_record.matched_reference_id = comment_id
            match_record.matched_reference_note = reference_note
            match_record.matched_reference_location = ref_loc.to_dict()
            match_record.matched_reference_details = _extract_reference_details(good_comment)
            match_record.location_match_details = location_result.to_dict()["details"]
            match_record.llm_comparison = {
                "is_similar": similarity_result.get("is_similar"),
                "reason": similarity_result.get("reason"),
                "raw_response": similarity_result.get("raw_response")
            }

    return line_matched, semantic_matched

async def _match_all_comments(
        generated_comments: List[Dict[str, Any]],
        good_comments: List[Dict[str, Any]],
        config: EvaluatorConfig
) -> MatchStatistics:
    """Execute matching for all comments"""
    stats = MatchStatistics()

    if not generated_comments:
        stats.total_good = len(good_comments)
        return stats

    semantic_match_func = None
    if config.enable_semantic_match:
        semantic_match_func = get_semantic_matcher(config.semantic_matcher_type)

    matched_good_ids = set()
    matched_good_ids_by_line = set()

    for idx, gen_comment in enumerate(generated_comments, 1):
        if not isinstance(gen_comment, dict) or not gen_comment.get("note"):
            continue

        gen_note = gen_comment.get("note", "")
        gen_loc = extract_comment_location(gen_comment, is_generated=True)

        match_record = MatchRecord(
            generated_comment_index=idx,
            generated_comment=gen_note,
            generated_location=gen_loc.to_dict()
        )

        matched = False
        line_matched = False

        for good_comment in good_comments:
            line_match_result, semantic_match_result = await _try_match_with_reference(
                gen_note, gen_loc, good_comment,
                config.line_distance_threshold,
                matched_good_ids, matched_good_ids_by_line,
                match_record,
                semantic_match_func
            )

            if line_match_result and not line_matched:
                stats.positive_line_matches += 1
                line_matched = True

            if semantic_match_result:
                stats.positive_matches += 1
                matched = True
                break

        if not matched and config.enable_semantic_match:
            stats.unmatched_count += 1

        stats.match_details.append(match_record.to_dict())

    stats.total_generated = _count_valid_comments(generated_comments)
    stats.total_good = _count_valid_comments(good_comments)

    return stats

def _extract_matched_references(match_details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract matched reference comments from match details"""
    matched_references = []

    for detail in match_details:
        if detail.get("semantic_match"):
            matched_references.append({
                "reference_comment_id": detail.get("matched_reference_id"),
                "reference_note": detail.get("matched_reference_note"),
                "reference_location": detail.get("matched_reference_location"),
                "reference_details": detail.get("matched_reference_details"),
                "matched_by_generated_index": detail.get("generated_comment_index"),
                "matched_by_generated_note": detail.get("generated_comment")
            })

    return matched_references

def _extract_llm_comparisons(match_details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract LLM comparison info from match details"""
    llm_comparisons = []

    for detail in match_details:
        if detail.get("llm_comparison"):
            llm_comparisons.append({
                "generated_comment_index": detail.get("generated_comment_index"),
                "generated_note": detail.get("generated_comment"),
                "reference_comment_id": detail.get("matched_reference_id"),
                "reference_note": detail.get("matched_reference_note"),
                "is_similar": detail.get("llm_comparison", {}).get("is_similar"),
                "llm_response": detail.get("llm_comparison", {}).get("raw_response")
            })

    return llm_comparisons

async def get_evaluator_ans_from_json(
        github_pr_url: str,
        generated_comments: List[Dict[str, Any]],
        good_comments: List[Dict[str, Any]],
        config: EvaluatorConfig = None,
        pr_metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Evaluate generated review comment quality.

    Args:
        github_pr_url: GitHub PR URL
        generated_comments: List of generated comments
        good_comments: List of reference comments
        config: Evaluator configuration
        pr_metadata: PR metadata (contains category, project_main_language, etc.)

    Returns:
        Dictionary containing evaluation results
    """
    try:
        if config is None:
            config = EvaluatorConfig()

        parsed_url = parse_github_pr_url(github_pr_url)
        evaluation_id = get_evaluation_id(github_pr_url)

        filtered_good_comments = good_comments
        filter_applied = False

        if config.filter_config:
            if pr_metadata and config.filter_config.has_pr_filter():
                if not config.filter_config.match_pr(pr_metadata):
                    return {
                        "github_pr_url": github_pr_url,
                        "evaluation_id": evaluation_id,
                        "skipped": True,
                        "skip_reason": "PR does not match filter criteria",
                        "filter_config": {
                            "pr_categories": config.filter_config.pr_categories,
                            "project_languages": config.filter_config.project_languages
                        }
                    }

            if config.filter_config.has_comment_filter():
                filtered_good_comments = config.filter_config.filter_comments(good_comments)
                filter_applied = True

        stats = await _match_all_comments(generated_comments, filtered_good_comments, config)

        positive_expected_nums = stats.total_good

        result = {
            "github_pr_url": github_pr_url,
            "owner": parsed_url["owner"],
            "repo": parsed_url["repo"],
            "pr_number": parsed_url["pr_number"],
            "evaluation_id": evaluation_id,
            "config": {
                "line_distance_threshold": config.line_distance_threshold,
                "semantic_matcher_type": config.semantic_matcher_type.value,
                "enable_semantic_match": config.enable_semantic_match
            },
            "positive_expected_nums": positive_expected_nums,
            "total_generated_nums": stats.total_generated,
            "positive_match_nums": stats.positive_matches,
            "positive_line_match_nums": stats.positive_line_matches,
            "unmatched_nums": stats.unmatched_count,
            "positive_match_rate": round(_calculate_rate(stats.positive_matches, stats.total_generated), 3),
            "positive_recall_rate": round(_calculate_rate(stats.positive_matches, positive_expected_nums), 3),
            "positive_line_match_rate": round(_calculate_rate(stats.positive_line_matches, stats.total_generated), 3),
            "positive_line_recall_rate": round(_calculate_rate(stats.positive_line_matches, positive_expected_nums), 3),
            "unmatched_rate": round(_calculate_rate(stats.unmatched_count, stats.total_generated), 3),
            "match_details": stats.match_details,
            "matched_reference_comments": _extract_matched_references(stats.match_details),
            "llm_comparisons": _extract_llm_comparisons(stats.match_details)
        }

        if config.filter_config:
            result["filter_config"] = {
                "pr_categories": config.filter_config.pr_categories,
                "project_languages": config.filter_config.project_languages,
                "comment_categories": config.filter_config.comment_categories,
                "comment_contexts": config.filter_config.comment_contexts
            }
            if filter_applied:
                result["original_good_comments_count"] = len(good_comments)
                result["filtered_good_comments_count"] = len(filtered_good_comments)

        return result
    except Exception as e:
        logging.error(f"Evaluation error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return {"error": str(e)}

__all__ = [
    'get_evaluator_ans_from_json',
    'EvaluatorConfig',
    'SemanticMatcherType',
    'parse_github_pr_url',
    'get_evaluation_id',
    'load_generated_comments_from_file'
]