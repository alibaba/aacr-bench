"""
Semantic Matching Base Module

Provides common abstractions and utilities for LLM/Embedding semantic matching.
"""
from abc import ABC
from typing import Dict, Any
from pathlib import Path
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Load .env from the correct path
env_path = Path(__file__).parent.parent / 'utils' / '.env'
load_dotenv(env_path)

class SemanticMatchResult:
    """Data class for semantic match result"""

    def __init__(
            self,
            is_similar: bool,
            reason: str = "",
            raw_response: str = None
    ):
        self.is_similar = is_similar
        self.reason = reason
        self.raw_response = raw_response

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_similar": self.is_similar,
            "reason": self.reason,
            "raw_response": self.raw_response
        }

SEMANTIC_COMPARISON_PROMPT_TEMPLATE = """
-Role-

You are an expert code reviewer assistant specialized in analyzing and comparing code review comments.

-Task-

Determine whether two given review comments express the same concern or suggestion. Ignore differences in wording, tone, or formatting—focus solely on semantic equivalence of the underlying issue. If the core intent and technical substance are identical, answer "yes"; otherwise, answer "no".

-Review Comments-

Review Comment 1:
{comment1}

Review Comment 2:
{comment2}

-Task-

Determine whether the two review comments given above express the same concern or suggestion. Ignore differences in wording, tone, or formatting—focus solely on semantic equivalence of the underlying issue. If the core intent and technical substance are identical, answer "yes"; otherwise, answer "no".

Your answer:
"""

def parse_similarity_response(response_text: str) -> bool:
    """
    Parse LLM response to determine similarity.

    Args:
        response_text: Raw response text from LLM

    Returns:
        Whether determined as similar
    """
    text = response_text.strip().lower()

    positive_keywords = ["yes", "similar", "same", "identical", "equivalent"]
    has_positive = any(keyword in text for keyword in positive_keywords)

    if "yes" in text:
        no_before_yes = "no" in text.split("yes")[0]
        return has_positive and not no_before_yes

    return has_positive

class BaseSemanticMatcher(ABC):
    """Abstract base class for semantic matchers"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def _build_prompt(self, comment1: str, comment2: str) -> str:
        """Build comparison prompt"""
        return SEMANTIC_COMPARISON_PROMPT_TEMPLATE.format(
            comment1=comment1,
            comment2=comment2
        )

    async def match(self, comment1: str, comment2: str) -> SemanticMatchResult:
        """
        Compare whether two comments express the same meaning.

        Args:
            comment1: First comment
            comment2: Second comment

        Returns:
            SemanticMatchResult object
        """
        prompt = self._build_prompt(comment1, comment2)

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=40000,
                top_p=0.95,
            )

            result_text = response.choices[0].message.content.strip()
            is_similar = parse_similarity_response(result_text)

            return SemanticMatchResult(
                is_similar=is_similar,
                reason=result_text.lower(),
                raw_response=result_text.lower()
            )
        except Exception as e:
            return SemanticMatchResult(
                is_similar=False,
                reason=f"ERROR: {str(e)}",
                raw_response=None
            )