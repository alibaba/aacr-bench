"""
Evaluator Configuration Module

Provides configurable parameters and options for the evaluator.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

class SemanticMatcherType(Enum):
    """Semantic matcher type"""
    LLM = "llm"
    EMBEDDING = "embedding"

class PRCategory(Enum):
    """PR category"""
    BUG_FIX = "Bug Fix"
    REFACTORING = "Code Refactoring / Architectural Improvement"
    NEW_FEATURE = "New Feature Additions"
    PERFORMANCE = "Performance Optimizations"
    SECURITY = "Security Patches / Vulnerability Fixes"
    DOCUMENTATION = "Documentation Update"
    CODE_STYLE = "Code Style, Linting, Formatting Fixes"
    TEST_CI = "Test Suite / CI Enhancements"
    DEPENDENCY = "Dependency Updates & Environment Compatibility"

class ProjectLanguage(Enum):
    """Project main language"""
    PYTHON = "Python"
    JAVA = "Java"
    JAVASCRIPT = "JavaScript"
    TYPESCRIPT = "TypeScript"
    GO = "Go"
    RUST = "Rust"
    C = "C"
    CPP = "C++"
    CSHARP = "C#"
    PHP = "PHP"

class CommentCategory(Enum):
    """Comment category"""
    CODE_DEFECT = "Code Defect"
    MAINTAINABILITY = "Maintainability and Readability"
    PERFORMANCE = "Performance"
    SECURITY = "Security Vulnerability"

class CommentContext(Enum):
    """Comment context level"""
    DIFF_LEVEL = "Diff Level"
    FILE_LEVEL = "File Level"
    REPO_LEVEL = "Repo Level"

@dataclass
class FilterConfig:
    """
    Data filtering configuration.

    Attributes:
        pr_categories: List of PR categories to include, empty means no filtering
        project_languages: List of project languages to include, empty means no filtering
        comment_categories: List of comment categories to include, empty means no filtering
        comment_contexts: List of comment context levels to include, empty means no filtering
    """
    pr_categories: List[str] = field(default_factory=list)
    project_languages: List[str] = field(default_factory=list)
    comment_categories: List[str] = field(default_factory=list)
    comment_contexts: List[str] = field(default_factory=list)

    def has_pr_filter(self) -> bool:
        """Check if there are PR-level filter conditions"""
        return bool(self.pr_categories or self.project_languages)

    def has_comment_filter(self) -> bool:
        """Check if there are comment-level filter conditions"""
        return bool(self.comment_categories or self.comment_contexts)

    def match_pr(self, pr_data: dict) -> bool:
        """Check if PR matches filter conditions"""
        if self.pr_categories:
            if pr_data.get("category") not in self.pr_categories:
                return False
        if self.project_languages:
            if pr_data.get("project_main_language") not in self.project_languages:
                return False
        return True

    def match_comment(self, comment: dict) -> bool:
        """Check if comment matches filter conditions"""
        if self.comment_categories:
            if comment.get("category") not in self.comment_categories:
                return False
        if self.comment_contexts:
            if comment.get("context") not in self.comment_contexts:
                return False
        return True

    def filter_comments(self, comments: list) -> list:
        """Filter comment list"""
        if not self.has_comment_filter():
            return comments
        return [c for c in comments if self.match_comment(c)]

@dataclass
class EvaluatorConfig:
    """
    Evaluator configuration.

    Attributes:
        line_distance_threshold: Line number matching distance threshold
            - 0: Must completely overlap
            - n: Allow up to n lines of distance difference
        semantic_matcher_type: Semantic matcher type (LLM or EMBEDDING)
        enable_semantic_match: Whether to enable semantic matching
            - False: Only perform location matching
        filter_config: Data filtering configuration
    """
    line_distance_threshold: int = 1
    semantic_matcher_type: SemanticMatcherType = SemanticMatcherType.LLM
    enable_semantic_match: bool = True
    filter_config: Optional[FilterConfig] = None

    def __post_init__(self):
        if self.line_distance_threshold < 0:
            raise ValueError("line_distance_threshold must be a non-negative integer")

    @classmethod
    def with_embedding(cls, line_distance_threshold: int = 1) -> "EvaluatorConfig":
        """Create config with Embedding matcher"""
        return cls(
            line_distance_threshold=line_distance_threshold,
            semantic_matcher_type=SemanticMatcherType.EMBEDDING
        )

    @classmethod
    def location_only(cls, line_distance_threshold: int = 1) -> "EvaluatorConfig":
        """Create config for location-only matching"""
        return cls(
            line_distance_threshold=line_distance_threshold,
            enable_semantic_match=False
        )

    @classmethod
    def with_filter(
        cls,
        pr_categories: List[str] = None,
        project_languages: List[str] = None,
        comment_categories: List[str] = None,
        comment_contexts: List[str] = None,
        **kwargs
    ) -> "EvaluatorConfig":
        """Create config with filter conditions"""
        filter_config = FilterConfig(
            pr_categories=pr_categories or [],
            project_languages=project_languages or [],
            comment_categories=comment_categories or [],
            comment_contexts=comment_contexts or []
        )
        return cls(filter_config=filter_config, **kwargs)