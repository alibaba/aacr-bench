from typing import Any, Dict, Optional
import logging

"""
Location Matching Module

Provides location-based matching logic for code review comments.
"""

DEFAULT_LINE_DISTANCE_THRESHOLD = 1

def is_line_range_overlapping(
    range1_start: int,
    range1_end: int,
    range2_start: int,
    range2_end: int,
    max_distance: int = DEFAULT_LINE_DISTANCE_THRESHOLD
) -> bool:
    """
    Check if two line ranges overlap or are within threshold distance.

    Args:
        range1_start: Start line of first range
        range1_end: End line of first range
        range2_start: Start line of second range
        range2_end: End line of second range
        max_distance: Maximum allowed distance threshold

    Returns:
        Whether the two ranges overlap or are within threshold
    """
    has_overlap = not (range1_start > range2_end or range1_end < range2_start)

    if has_overlap:
        return True

    min_distance = min(
        abs(range1_start - range2_end),
        abs(range1_end - range2_start)
    )
    return min_distance <= max_distance

def normalize_path(path: Optional[str]) -> str:
    """
    Normalize file path to use forward slashes.

    Args:
        path: Original path string

    Returns:
        Normalized path, empty string for null values
    """
    if not path:
        return ""
    return path.replace("\\/", "/").replace("\\", "/")

class CommentLocation:
    """Data class for comment location information"""

    def __init__(
        self,
        path: str = "",
        from_line: Optional[int] = None,
        to_line: Optional[int] = None,
        side: Optional[str] = None
    ):
        self.path = path
        self.from_line = from_line
        self.to_line = to_line
        self.side = side

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "from_line": self.from_line,
            "to_line": self.to_line,
            "side": self.side
        }

    def has_complete_line_info(self) -> bool:
        """Check if line information is complete"""
        return self.from_line is not None and self.to_line is not None

def extract_comment_location(comment: Dict[str, Any], is_generated: bool = False) -> CommentLocation:
    """
    Extract location information from comment dictionary.

    Args:
        comment: Comment dictionary
        is_generated: Whether this is a generated comment

    Returns:
        CommentLocation object
    """
    line_range = comment.get("originLineRange", {})

    from_line = comment.get("from_line") or line_range.get("from_line")
    to_line = comment.get("to_line") or line_range.get("to_line")

    return CommentLocation(
        path=normalize_path(comment.get("path", "")),
        from_line=from_line,
        to_line=to_line,
        side=comment.get("side")
    )

class LocationMatchResult:
    """Data class for location match result"""

    def __init__(
        self,
        is_match: bool,
        path_match: bool = True,
        side_match: bool = True,
        line_overlap: bool = True
    ):
        self.is_match = is_match
        self.path_match = path_match
        self.side_match = side_match
        self.line_overlap = line_overlap

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_match": self.is_match,
            "details": {
                "path_match": self.path_match,
                "side_match": self.side_match,
                "line_overlap": self.line_overlap
            }
        }

def match_location(
    generated_loc: CommentLocation,
    reference_loc: CommentLocation,
    comment_id: str,
    line_distance_threshold: int = DEFAULT_LINE_DISTANCE_THRESHOLD
) -> LocationMatchResult:
    """
    Determine if two comment locations match.

    Matching rules (checked in order):
    1. File paths must be identical (if both exist)
    2. Side fields must be identical (if both exist)
    3. Line ranges must overlap or be within threshold distance

    Args:
        generated_loc: Generated comment location
        reference_loc: Reference comment location
        comment_id: Reference comment ID (for logging)
        line_distance_threshold: Line matching distance threshold

    Returns:
        LocationMatchResult object
    """
    if generated_loc.path and reference_loc.path:
        if generated_loc.path != reference_loc.path:
            logging.debug(f"Skip {comment_id}: path mismatch")
            return LocationMatchResult(is_match=False, path_match=False)

    if generated_loc.side is not None and reference_loc.side is not None:
        if generated_loc.side != reference_loc.side:
            logging.debug(f"Skip {comment_id}: side mismatch")
            return LocationMatchResult(is_match=False, side_match=False)

    if generated_loc.has_complete_line_info() and reference_loc.has_complete_line_info():
        if not is_line_range_overlapping(
            generated_loc.from_line, generated_loc.to_line,
            reference_loc.from_line, reference_loc.to_line,
            line_distance_threshold
        ):
            logging.debug(f"Skip {comment_id}: line range mismatch")
            return LocationMatchResult(is_match=False, line_overlap=False)

    return LocationMatchResult(is_match=True)

# ============ 兼容性别名（保持向后兼容） ============
def _normalize_path(path: str) -> str:
    """向后兼容的别名"""
    return normalize_path(path)


def _extract_comment_location(comment: Dict[str, Any], is_generated: bool = False) -> Dict[str, Any]:
    """向后兼容的别名，返回字典格式"""
    return extract_comment_location(comment, is_generated).to_dict()


def _is_location_match(
    gen_loc: Dict[str, Any],
    ref_loc: Dict[str, Any],
    comment_id: str,
    k: int = 1
) -> bool:
    """向后兼容的别名"""
    gen = CommentLocation(**gen_loc)
    ref = CommentLocation(**ref_loc)
    return match_location(gen, ref, comment_id, k).is_match


def _is_location_match_with_details(
    gen_loc: Dict[str, Any],
    ref_loc: Dict[str, Any],
    comment_id: str,
    k: int = 1
) -> Dict[str, Any]:
    """向后兼容的别名，返回详细匹配信息"""
    gen = CommentLocation(**gen_loc)
    ref = CommentLocation(**ref_loc)
    return match_location(gen, ref, comment_id, k).to_dict()