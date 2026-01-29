"""
Example: Custom Directory Evaluation

A simple and configurable evaluation script for batch processing.
"""

import asyncio
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from evaluator_runner import (
    get_evaluator_ans_from_json,
    load_generated_comments_from_file,
    EvaluatorConfig,
    FilterConfig,
    SemanticMatcherType,
)

# ============================================================================
# Configuration - Modify these settings as needed
# ============================================================================

# TODO Input directory containing generated comment files (.txt) Currently, this is a demo for claude-code-demo
INPUT_DIR = "../claude-code-demo/comments"

# TODO Output file for evaluation results
OUTPUT_FILE = "./results/evaluation_results.json"

# File pattern to match
FILE_PATTERN = "*.txt"

# Reference data file (positive samples)
REFERENCE_DATA_FILE = "../dataset/positive_samples.json"

# ============================================================================
# Evaluation Configuration
# ============================================================================

# Line distance threshold (0 = must overlap, 1 = allow 1 line difference)
LINE_DISTANCE_THRESHOLD = 1

# Enable semantic matching (True/False)
ENABLE_SEMANTIC_MATCH = True

# TODO Semantic matcher type: "llm" or "embedding"
SEMANTIC_MATCHER_TYPE = "llm"

# ============================================================================
# Filter Configuration (Optional) - Set to None to disable filtering
# ============================================================================

# TODO PR categories to include (e.g., ["Bug Fix", "Performance Optimizations"])
PR_CATEGORIES = None

# TODO Project languages to include (e.g., ["Python", "Java"])
PROJECT_LANGUAGES = None

# TODO Comment categories to include (e.g., ["Code Defect", "Security Vulnerability"])
COMMENT_CATEGORIES = None

# TODO Comment contexts to include (e.g., ["Diff Level", "File Level"])
COMMENT_CONTEXTS = None


# ============================================================================
# Helper Functions
# ============================================================================

def load_reference_data(file_path: str) -> List[Dict[str, Any]]:
    """Load reference data from JSON file."""
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def find_reference_by_url(
    pr_url: str,
    reference_data: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Find reference data by PR URL."""
    for item in reference_data:
        if item.get("githubPrUrl") == pr_url:
            return item
    return None


def infer_pr_url_from_filename(
    filename: str,
    reference_data: List[Dict[str, Any]]
) -> str:
    """
    Infer PR URL from filename.

    Expected filename format: comments_{repo}_{pr_number}.txt

    Example: comments_cherry-studio_5540.txt
    """
    stem = Path(filename).stem
    
    if stem.startswith("comments_"):
        parts = stem[9:].rsplit("_", 1)
        if len(parts) == 2:
            repo_name, pr_number = parts
            for item in reference_data:
                url = item.get("githubPrUrl", "")
                if repo_name.lower() in url.lower() and f"/pull/{pr_number}" in url:
                    return url
    
    # Fallback: search by any matching pattern
    for item in reference_data:
        pr_url = item.get("githubPrUrl", "")
        parts = pr_url.split("/")
        if len(parts) >= 7:
            repo_name = parts[-3]
            pr_number = parts[-1]
            if repo_name.lower() in stem.lower() and pr_number in stem:
                return pr_url
    
    return None


def build_config() -> EvaluatorConfig:
    """Build evaluator configuration from settings."""
    filter_config = None
    if any([PR_CATEGORIES, PROJECT_LANGUAGES, COMMENT_CATEGORIES, COMMENT_CONTEXTS]):
        filter_config = FilterConfig(
            pr_categories=PR_CATEGORIES or [],
            project_languages=PROJECT_LANGUAGES or [],
            comment_categories=COMMENT_CATEGORIES or [],
            comment_contexts=COMMENT_CONTEXTS or [],
        )
    
    matcher_type = (
        SemanticMatcherType.EMBEDDING
        if SEMANTIC_MATCHER_TYPE == "embedding"
        else SemanticMatcherType.LLM
    )
    
    return EvaluatorConfig(
        line_distance_threshold=LINE_DISTANCE_THRESHOLD,
        semantic_matcher_type=matcher_type,
        enable_semantic_match=ENABLE_SEMANTIC_MATCH,
        filter_config=filter_config,
    )


# ============================================================================
# Main Evaluation Logic
# ============================================================================

async def evaluate_directory() -> Dict[str, Any]:
    """
    Evaluate all comment files in the input directory.
    """
    input_path = Path(INPUT_DIR)
    
    if not input_path.exists():
        raise FileNotFoundError(f"Input directory not found: {INPUT_DIR}")
    
    # Load reference data
    if not Path(REFERENCE_DATA_FILE).exists():
        raise FileNotFoundError(f"Reference data file not found: {REFERENCE_DATA_FILE}")
    
    reference_data = load_reference_data(REFERENCE_DATA_FILE)
    print(f"Loaded {len(reference_data)} reference PRs")
    
    # Find all comment files
    files = list(input_path.glob(FILE_PATTERN))
    if not files:
        raise FileNotFoundError(f"No files matching '{FILE_PATTERN}' in {INPUT_DIR}")
    
    print(f"Found {len(files)} files to evaluate")
    
    # Build configuration
    config = build_config()
    print(f"Configuration: line_threshold={LINE_DISTANCE_THRESHOLD}, "
          f"semantic_match={ENABLE_SEMANTIC_MATCH}")
    
    # Evaluate each file
    results = []
    total_generated = 0
    total_reference = 0
    total_line_matches = 0
    total_semantic_matches = 0
    
    for file_path in files:
        print(f"\nProcessing: {file_path.name}")
        
        # Infer PR URL
        pr_url = infer_pr_url_from_filename(file_path.name, reference_data)
        if not pr_url:
            print(f"  Skipped: Cannot match PR URL")
            continue
        
        # Find reference comments
        ref_item = find_reference_by_url(pr_url, reference_data)
        if not ref_item:
            print(f"  Skipped: No reference data found")
            continue
        
        reference_comments = ref_item.get("comments", [])
        
        # Load generated comments
        try:
            generated_comments = load_generated_comments_from_file(str(file_path))
        except Exception as e:
            print(f"  Skipped: Failed to parse file ({e})")
            continue
        
        if not generated_comments:
            print(f"  Skipped: No valid comments found")
            continue
        
        # Run evaluation
        pr_metadata = {
            "category": ref_item.get("category"),
            "project_main_language": ref_item.get("project_main_language"),
        }
        
        result = await get_evaluator_ans_from_json(
            github_pr_url=pr_url,
            generated_comments=generated_comments,
            good_comments=reference_comments,
            config=config,
            pr_metadata=pr_metadata,
        )
        
        if result.get("skipped"):
            print(f"  Skipped: {result.get('skip_reason')}")
            continue
        
        results.append(result)
        
        # Accumulate statistics
        total_generated += result.get("total_generated_nums", 0)
        total_reference += result.get("positive_expected_nums", 0)
        total_line_matches += result.get("positive_line_match_nums", 0)
        total_semantic_matches += result.get("positive_match_nums", 0)
        
        print(f"  Generated: {result.get('total_generated_nums')}, "
              f"Reference: {result.get('positive_expected_nums')}, "
              f"Line Match: {result.get('positive_line_match_nums')}, "
              f"Semantic Match: {result.get('positive_match_nums')}")
    
    # Calculate overall metrics
    overall_line_match_rate = (
        total_line_matches / total_generated if total_generated > 0 else 0
    )
    overall_semantic_match_rate = (
        total_semantic_matches / total_generated if total_generated > 0 else 0
    )
    overall_line_recall = (
        total_line_matches / total_reference if total_reference > 0 else 0
    )
    overall_semantic_recall = (
        total_semantic_matches / total_reference if total_reference > 0 else 0
    )
    
    summary = {
        "total_files": len(files),
        "evaluated_files": len(results),
        "total_generated_comments": total_generated,
        "total_reference_comments": total_reference,
        "total_line_matches": total_line_matches,
        "total_semantic_matches": total_semantic_matches,
        "overall_line_match_rate": round(overall_line_match_rate, 4),
        "overall_semantic_match_rate": round(overall_semantic_match_rate, 4),
        "overall_line_recall": round(overall_line_recall, 4),
        "overall_semantic_recall": round(overall_semantic_recall, 4),
        "details": results,
    }
    
    return summary


async def main():
    """Main entry point."""
    print("=" * 60)
    print("Custom Directory Evaluation")
    print("=" * 60)
    print(f"Input Directory: {INPUT_DIR}")
    print(f"Output File: {OUTPUT_FILE}")
    print(f"File Pattern: {FILE_PATTERN}")
    print()
    
    try:
        result = await evaluate_directory()
        
        # Save results
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        # Print summary
        print("\n" + "=" * 60)
        print("Evaluation Summary")
        print("=" * 60)
        print(f"Files Evaluated: {result['evaluated_files']}/{result['total_files']}")
        print(f"Total Generated Comments: {result['total_generated_comments']}")
        print(f"Total Reference Comments: {result['total_reference_comments']}")
        print(f"Line Match Rate: {result['overall_line_match_rate']:.2%}")
        print(f"Semantic Match Rate: {result['overall_semantic_match_rate']:.2%}")
        print(f"Line Recall: {result['overall_line_recall']:.2%}")
        print(f"Semantic Recall: {result['overall_semantic_recall']:.2%}")
        print(f"\nResults saved to: {OUTPUT_FILE}")
        
    except FileNotFoundError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
