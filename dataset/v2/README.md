# AACR-Bench v2

[Back to the dataset card](../../README.md)

This directory contains the v2 release of AACR-Bench: 944 review comments from 151 pull requests across 42 repositories. v2 is a positive-only subset of v1, retaining a selection of v1's high-value review comments.

The nested canonical JSON in this directory is the source consumed by the GitHub evaluation pipeline. The flattened `data/v2/dataset.json` on the Hugging Face dataset repository is generated from the same records and uses the 16-field dataset-card schema.

## At a glance

| Item | Value |
|---|---:|
| Review comments | 944 |
| Unique pull requests | 151 |
| Unique repositories | 42 |
| Correct comments (HF flat `label=1`) | 944 |
| Incorrect comments (HF flat `label=0`) | 0 |
| AI-generated comments (`is_ai_comment=true`) | 725 |
| PR review comments (`is_ai_comment=false`) | 219 |

## Label distribution

| Label | Meaning | Comments | Share |
|---|---|---:|---:|
| `1` | Expert-verified correct | 944 | 100% |

Shares are rounded to one decimal place. The canonical nested JSON in this directory has no `label` field; the 16-field Hugging Face flat export sets `label=1` for every v2 comment.

## Comment categories

| Category | Comments | Share |
|---|---:|---:|
| Code Defect | 606 | 64.2% |
| Maintainability and Readability | 196 | 20.8% |
| Performance | 103 | 10.9% |
| Security Vulnerability | 39 | 4.1% |

## Context levels

| Context | Comments | Share |
|---|---:|---:|
| Diff Level | 419 | 44.4% |
| File Level | 352 | 37.3% |
| Repo Level | 173 | 18.3% |

## Languages

| Language | Comments |
|---|---:|
| C++ | 207 |
| TypeScript | 144 |
| Java | 129 |
| Go | 113 |
| C | 102 |
| Python | 93 |
| JavaScript | 69 |
| Rust | 34 |
| PHP | 27 |
| C# | 26 |

## Source models

`source_model` is empty for PR review comments (`is_ai_comment=false`); these come from the original PR review threads and may include bot-authored comments.

| Source model | Comments |
|---|---:|
| GPT-5.2 | 270 |
| PR review comment | 219 |
| Claude-Code/Claude-4.5-Sonnet | 122 |
| GLM-4.7 | 102 |
| Qwen-Coder-480B | 101 |
| Deepseek-V3.2 | 67 |
| Gemini-3-Pro | 63 |

## Pull request statistics

Statistics are computed over unique pull requests.

| Metric | Value |
|---|---:|
| Changed lines per PR: min / median / mean / max | 5 / 159 / 238.9 / 971 |
| Comments per PR: min / median / mean / max | 1 / 5 / 6.3 / 30 |

## File

`positive_samples.json` — nested canonical JSON, one object per pull request. Each object keeps the GitHub PR URL and the `source_commit` / `target_commit` pair; `comments[]` contains the 9 reference-comment fields, and the converter maps `path`, `note`, `from_line`, `to_line` and `side` into the standard evaluation schema. The v2 canonical does not include the v1 `created_at` / `merged_at` fields; evaluation does not use them. All v2 comments are stored on the right side: two v1 left-side comments were re-anchored to right so the evaluator's right-side generated comments can match them; every other audited field is identical to v1.

The v1 dataset remains available as `dataset/positive_samples.json` (correct comments) and `dataset/negative_samples.json` (annotated negative/candidate pool; `label=0` in the HF flat export).

## Repository snapshots

The evaluation pipeline can download shallow repository snapshots from the Hugging Face snapshot repository. Each archive retains the original license of its upstream repository; no aggregate license is granted. The Apache-2.0 license of this GitHub repository covers its own code, not the third-party code inside the snapshots. Use of a snapshot must comply with that repository's license and attribution requirements.