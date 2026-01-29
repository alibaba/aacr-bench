"""
Semantic Matcher Factory

Creates semantic matchers based on configuration.
"""
from typing import Callable, Awaitable, Dict, Any

from evaluator_runner.utils.config import SemanticMatcherType
from evaluator_runner.core.match_llm import match_llm
from evaluator_runner.core.match_embedding import match_embedding

# Type alias for semantic match function signature
SemanticMatchFunc = Callable[[str, str], Awaitable[Dict[str, Any]]]


def get_semantic_matcher(matcher_type: SemanticMatcherType) -> SemanticMatchFunc:
    """Get semantic matching function by type"""
    matchers = {
        SemanticMatcherType.LLM: match_llm,
        SemanticMatcherType.EMBEDDING: match_embedding,
    }

    if matcher_type not in matchers:
        raise ValueError(f"Unknown semantic matcher type: {matcher_type}")

    return matchers[matcher_type]