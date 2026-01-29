# Evaluator Runner

A code review comment evaluation framework for assessing the match between AI-generated review comments and human-annotated reference comments.

## Directory Structure

```
evaluator_runner/
├── __init__.py              # Module exports
│── example_test.py          # Example usage
│── README.md                # README
│── README.zh-CN.md          # README in Chinese
├── core/
│   ├── evaluator.py         # Core evaluation logic
│   ├── match_location.py    # Location matching logic
│   ├── match_base.py        # Semantic matching base class
│   ├── match_llm.py         # LLM semantic matching
│   ├── match_embedding.py   # Embedding semantic matching
│   └── matcher_factory.py   # Matcher factory
└── utils/
    ├── config.py            # Configuration classes and enums
    └── .env                 # Environment variables
```

## Quick Start

### Installation

```bash
pip install openai python-dotenv
```

### Configure Environment Variables

Create a `.env` file in the `utils/` directory, or copy from `.env_sample`:

```env
LLM_MODEL_URL="your_llm_model_url"
LLM_MODEL="your_llm_model"
LLM_API_KEY="your_llm_api_key"

EMBEDDING_MODEL_URL="your_embedding_model_url"
EMBEDDING_MODEL="your_embedding_model"
EMBEDDING_API_KEY="your_embedding_api_key"
```

### Basic Usage

```python
import asyncio
from evaluator_runner import (
    get_evaluator_ans_from_json,
    load_generated_comments_from_file,
    EvaluatorConfig
)

async def main():
    # Load generated comments to evaluate
    generated_comments = load_generated_comments_from_file("path/to/comments.txt")
    
    # Reference comments (load from positive_samples.json)
    reference_comments = [...]
    
    # Run evaluation with default config
    result = await get_evaluator_ans_from_json(
        github_pr_url="https://github.com/owner/repo/pull/123",
        generated_comments=generated_comments,
        good_comments=reference_comments
    )
    
    print(f"Location Match Rate: {result['positive_line_match_rate']}")
    print(f"Semantic Match Rate: {result['positive_match_rate']}")

asyncio.run(main())
```

## Configuration

### EvaluatorConfig

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `line_distance_threshold` | `int` | `1` | Line matching distance threshold, 0 means must overlap |
| `semantic_matcher_type` | `SemanticMatcherType` | `LLM` | Semantic matcher type: `LLM` or `EMBEDDING` |
| `enable_semantic_match` | `bool` | `True` | Whether to enable semantic matching |
| `filter_config` | `FilterConfig` | `None` | Data filtering configuration |

### Configuration Shortcuts

```python
from evaluator_runner import EvaluatorConfig

# Use Embedding matcher
config = EvaluatorConfig.with_embedding(line_distance_threshold=2)

# Location-only matching (disable semantic matching)
config = EvaluatorConfig.location_only(line_distance_threshold=1)

# Config with filter conditions
config = EvaluatorConfig.with_filter(
    pr_categories=["Bug Fix"],
    project_languages=["Python"],
    comment_categories=["Code Defect"]
)
```

### FilterConfig

| Parameter | Type | Description |
|-----------|------|-------------|
| `pr_categories` | `List[str]` | PR category filter |
| `project_languages` | `List[str]` | Project language filter |
| `comment_categories` | `List[str]` | Comment category filter |
| `comment_contexts` | `List[str]` | Comment context level filter |

## Input Format

### Generated Comments File (.txt)

```
<path>src/main.py</path>
<side>right</side>
<from>10</from>
<to>15</to>
<note>Potential null pointer issue here</note>
<notesplit />
<path>src/utils.py</path>
<side>right</side>
<from>20</from>
<to>25</to>
<note>Consider adding exception handling</note>
<notesplit />
```

### Reference Comments Format (positive_samples.json)

```json
{
    "category": "Bug Fix",
    "project_main_language": "Python",
    "githubPrUrl": "https://github.com/owner/repo/pull/123",
    "comments": [
        {
            "id": "comment_1",
            "note": "Comment content",
            "path": "src/main.py",
            "side": "right",
            "from_line": 10,
            "to_line": 15,
            "category": "Code Defect",
            "context": "Diff Level"
        }
    ]
}
```

## Output Format

```json
{
    "github_pr_url": "https://github.com/owner/repo/pull/123",
    "evaluation_id": "repo_123",
    "positive_expected_nums": 10,
    "total_generated_nums": 8,
    "positive_line_match_nums": 6,
    "positive_match_nums": 5,
    "positive_line_match_rate": 0.75,
    "positive_line_recall_rate": 0.6,
    "positive_match_rate": 0.625,
    "positive_recall_rate": 0.5,
    "match_details": [...],
    "matched_reference_comments": [...],
    "llm_comparisons": [...]
}
```

## Matching Process

```
Generated Comment ──┬── Location Match ──┬── Semantic Match ── ✓ Full Match
                    │                    │
                    ✗ No Match           ✗ Location Only
```

**Location Matching Rules**:
1. File paths must be identical
2. Side fields must be identical
3. Line ranges must overlap or be within threshold distance

**Semantic Matching**: Uses LLM or Embedding to determine if two comments express the same meaning

## Enum Types

### PRCategory
- `Bug Fix`
- `Code Refactoring / Architectural Improvement`
- `New Feature Additions`
- `Performance Optimizations`
- `Security Patches / Vulnerability Fixes`
- `Documentation Update`
- `Code Style, Linting, Formatting Fixes`
- `Test Suite / CI Enhancements`
- `Dependency Updates & Environment Compatibility`

### ProjectLanguage
- `Python`, `Java`, `JavaScript`, `TypeScript`, `Go`, `Rust`, `C`, `C++`, `C#`, `PHP`

### CommentCategory
- `Code Defect`
- `Maintainability and Readability`
- `Performance`
- `Security Vulnerability`

### CommentContext
- `Diff Level`
- `File Level`
- `Repo Level`

## Batch Evaluation Example

Use `example_test.py` for batch evaluation of a custom directory:

### Configuration

Edit the configuration section at the top of `example_test.py`:

```python
# Input/Output Settings
INPUT_DIR = "./test_comments"           # Directory containing comment files
OUTPUT_FILE = "./evaluation_results.json"  # Output file path
FILE_PATTERN = "*.txt"                   # File matching pattern
REFERENCE_DATA_FILE = "./positive_samples.json"  # Reference data file

# Evaluation Settings
LINE_DISTANCE_THRESHOLD = 1              # Line matching threshold
ENABLE_SEMANTIC_MATCH = True             # Enable semantic matching
SEMANTIC_MATCHER_TYPE = "llm"            # "llm" or "embedding"

# Filter Settings (Optional, set to None to disable)
PR_CATEGORIES = None                     # e.g., ["Bug Fix"]
PROJECT_LANGUAGES = None                 # e.g., ["Python", "Java"]
COMMENT_CATEGORIES = None                # e.g., ["Code Defect"]
COMMENT_CONTEXTS = None                  # e.g., ["Diff Level"]
```

### Run Evaluation

```bash
python evaluator_runner/example_test.py
```

### Expected File Naming

Comment files should follow this naming pattern:
```
comments_{repo_name}_{pr_number}.txt
```

Example: `comments_cherry-studio_5540.txt`