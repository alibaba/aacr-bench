# Evaluation Metrics Migration: File-Level Semantic Coverage

## TL;DR

AACR-Bench switched its default evaluation metrics from **per-comment one-to-one
matching** (semantic / line Precision / Recall / F1) to **file-level semantic
coverage** (`file_semantic_match_rate` / `file_semantic_recall_rate` /
`balanced_score`).

The motivation is empirical: we repeatedly observed that models — especially
strong LLM reviewers — often point out **multiple distinct problems in a single
review comment**. The legacy one-to-one matching could only credit such a
comment once, so both match counts and recall were systematically undercounted
and the metrics read artificially low.

The legacy metric system is still available via `--use-deprecated-score` and
will be removed in a future release.

## Background: the legacy matching pipeline

The legacy evaluator (`evaluate_comments` in `evaluation/judge.py`) iterated
over each reference (ground-truth) comment and scanned the generated comments
through four filtering stages:

```
path → side → line(k) → semantic
```

- **path / side**: hard equality on file path and diff side.
- **line(k)**: hard gate — the generated comment's line range had to overlap or
  be within distance `k` of the reference range (whenever both sides carried
  line information).
- **semantic**: a pairwise judge-LLM call deciding whether the two comments
  express the same concern.

Crucially, matching was **one-to-one with global consumption**: each generated
comment could be consumed by at most one reference comment for semantic
matching (`matched_gen_ids_by_semantic`).

## Why the legacy metrics read artificially low

### 1. One comment, multiple issues — but only one credit

Real reviewer output does not follow the one-issue-per-comment convention of
the annotated dataset. A single generated comment like:

> "This method swallows the exception and returns null; callers will NPE, and
> the failed transaction is never rolled back."

may genuinely cover two or three separately annotated reference comments. Under
one-to-one matching, the first reference comment consumed the generated
comment, and every additional reference comment it covered was scored as a
**miss** — directly deflating recall.

### 2. Pairwise judging penalizes multi-issue comments

The legacy semantic stage compared one generated comment against one reference
comment. When the generated comment bundles several issues, the judge sees a
long text of which only a fraction corresponds to the reference, producing
borderline "not similar" verdicts even for genuinely covered issues.

### 3. The line(k) gate discarded correct comments before judging

When both comments carried line information, a generated comment pointing at
the same problem on slightly shifted lines never reached the semantic stage at
all. Line numbers turned out to be a noisy hard filter for LLM reviewers, which
routinely anchor the right issue to a neighboring line.

### Worked example

Suppose a file has two reference comments (GT-1, GT-2), and the model emits a
single comment covering both:

| | Legacy (one-to-one) | New (file-level) |
|---|---|---|
| GT-1 | matched (consumes the comment) | covered |
| GT-2 | **missed** (comment already consumed) | covered |
| Recall | 1/2 = 0.5 | 2/2 = 1.0 |
| Precision | 1/1 = 1.0 | 1/1 = 1.0 |

The model did everything right; the legacy recall still reported 0.5.

## The new system: file-level semantic coverage

The default evaluator (`evaluate_file_level_semantics` in
`evaluation/judge.py`) works as follows:

1. **Group by file.** Reference and generated comments are grouped by
   normalized file path. Side and line number are no longer hard filters; the
   line range is passed to the judge as context only.
2. **One judge call per generated comment.** Each generated comment is compared
   against a numbered list of *all* reference comments in the same file in a
   single judge-LLM call. The judge is instructed to ignore wording, tone,
   formatting and exact line numbers, and to return the numbers of all
   reference comments expressing the same concern — i.e. **one-to-many
   coverage** is a first-class outcome.
3. **Greedy dedup for precision.** Processing generated comments in generation
   order, a comment counts toward precision only if it covers at least one
   previously uncovered reference comment (`hit_new_gt`). Comments that merely
   re-cover already-covered references are marked as duplicate hits and
   excluded from the precision numerator, so repeated hits cannot inflate
   precision.

### Metric definitions

| Metric | Definition |
|---|---|
| `file_semantic_match_rate` (precision) | generated comments that covered ≥1 previously uncovered reference / total generated comments |
| `file_semantic_recall_rate` (recall) | reference comments covered by ≥1 generated comment (deduplicated) / total reference comments |
| `balanced_score` | harmonic mean of precision and recall |

### Audit trail

Every judged comment is recorded in the metrics JSON under
`eval_res[].generated_comments[]` with:

- `matched_gt_indices`: indices (into the sibling `comments` array) of the
  reference comments this generated comment covers;
- `judge_reason`: the judge's per-reference rationale;
- `hit_new_gt`: whether the comment covered at least one previously uncovered
  reference (false = duplicate hit, excluded from precision).

Generated comments in files with no reference comments are recorded with an
empty `matched_gt_indices` and do not invoke the judge.

## Migration notes

- **Mode marker**: result JSON carries `ex_info.score_mode`
  (`"file_semantic"` | `"deprecated"`) so downstream tooling can tell which
  system produced the numbers.
- **`--line-k` only affects the deprecated mode**; the file-level system passes
  line ranges to the judge as context instead of filtering on them.
- **Numbers are not comparable across modes.** Do not mix legacy and
  file-level scores in the same table or chart; re-run evaluation under the new
  system for historical baselines.
- The complete field reference for the current metric system lives in
  [metrics.md](metrics.md).
- Usage details: see the *Evaluation* section of
  [../evaluation/README.md](../evaluation/README.md).
