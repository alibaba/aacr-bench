"""
Embedding Semantic Matching Module
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from evaluator_runner.core.match_base import BaseSemanticMatcher

# Load .env from the correct path
env_path = Path(__file__).parent.parent / 'utils' / '.env'
load_dotenv(env_path)

class EmbeddingMatcher(BaseSemanticMatcher):
    """Embedding-based semantic matcher"""

    def __init__(self):
        super().__init__(
            base_url=os.getenv('EMBEDDING_MODEL_URL'),
            api_key=os.getenv('EMBEDDING_API_KEY'),
            model=os.getenv('EMBEDDING_MODEL')
        )

_matcher_instance = None

def _get_matcher() -> EmbeddingMatcher:
    """Get matcher singleton"""
    global _matcher_instance
    if _matcher_instance is None:
        _matcher_instance = EmbeddingMatcher()
    return _matcher_instance

async def match_embedding(str1: str, str2: str) -> dict:
    """
    Compare two comments using Embedding model.

    Args:
        str1: First comment
        str2: Second comment

    Returns:
        Dict containing is_similar, reason, raw_response
    """
    matcher = _get_matcher()
    result = await matcher.match(str1, str2)
    return result.to_dict()