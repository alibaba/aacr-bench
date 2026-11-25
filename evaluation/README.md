# AACR-Bench Evaluation Pipeline

English | [简体中文](README.zh-CN.md)

## Directory Structure

```text
evaluation/
├── pipeline.py                # Command entry; data loading, review, and evaluation
├── config.py                  # Configuration variables, defaults, and paths
├── schema.py                  # JSONL sample format and validation
├── repo_utils.py              # Repository acquisition, caching, and commit checks
├── evaluate.py                # Aggregate review results and save metrics
├── judge.py                   # Judge calls and semantic matching
├── reviewers/                 # Reviewer invocation and result collection
│   ├── ocr.py                 # OpenCodeReview
│   ├── claude.py              # Claude Code
│   └── codex.py               # Codex
├── mcp_finding_server.py       # Receive Claude Code review comments
├── mcp_codex_finding_server.py # Receive Codex review comments
├── docker_runner.py           # Schedule Docker review tasks and save results
├── hooks/
│   ├── on_stop_failure.py     # Record exhausted Claude Code retries
│   └── collect_report_findings.py # Persist native ReportFindings calls
├── converters/
│   └── aacr_bench.py          # Convert raw data to JSONL
├── docker/                    # Docker image build and container runtime
├── benchmark/AACR-Bench/
│   ├── v1/                   # v1 raw-data cache and download metadata
│   └── v2/                   # v2 raw-data cache and download metadata
├── data/                     # Converted JSONL data
├── repo/                     # Default repository cache
├── snapshots/                # Default repository archive cache
├── results/                  # Review results grouped by dataset, reviewer, and run name
├── metrics/                  # Metrics using the same grouping as review results
├── .env.example              # Model configuration template
└── requirements.txt          # Pinned Python dependencies
```

Data, cache, and result directories are created as needed; the tree shows their default locations.

## Pipeline

This directory contains the data converter and code review evaluation program. The converter writes JSONL (one JSON object per line), then `pipeline` performs these stages:

| Stage | Work | Model calls |
| --- | --- | --- |
| Data loading | Read samples, commit ranges, and reference comments; validate the format | None |
| Review | Generate comments with OpenCodeReview (OCR), Claude Code, or Codex | Review model; none during preview |
| Judge evaluation | Match comments against references and compute metrics | Real judge model; none in local simulation mode |

Before review, the program uses a local repository or cached archive when available; otherwise it downloads a Hugging Face repository snapshot, then clones the repository from GitHub if the snapshot is unavailable. The cache directory must not point to a repository used for development or a shared read-only directory.

`--docker` changes only the review stage; evaluation always runs on the host. `--stage review` stops after review, `--stage eval` evaluates existing results on the host and cannot be combined with `--docker`, and the default `--stage all` waits for all selected review tasks to finish before evaluating the selected samples on the host, including samples whose reviews failed. Cancellation or a fatal runner error stops the pipeline.

Default scoring matches generated comments against references in the same file and computes Precision, Recall, and Balanced Score. A generated comment counts toward the precision numerator only if it matches at least one previously unmatched reference. See [metric definitions](../docs/metrics.md) and the [project README](../README.md) for dataset background.

## Standard Data Format

The dataset uses **JSONL**, with one sample (`ReviewInstance`) per line. The example below is from [browser-use/browser-use PR #1482](https://github.com/browser-use/browser-use/pull/1482) in v2 and is expanded for readability:

```json
{
  "instance_id": "browser-use__browser-use@0b21e50",
  "repo": "browser-use/browser-use",
  "base_commit": "fa461585f1fc2d4a430e68e6c115a12f4936fe73",
  "head_commit": "0b21e50ca640581ecd46990475c8b223eaa05045",
  "clone_url": "https://github.com/browser-use/browser-use.git",
  "reference_comments": [
    {
      "path": "browser_use/browser/browser.py",
      "start_line": 290,
      "end_line": 292,
      "side": "right",
      "text": "`self.config.chrome_remote_debugging_port` is typed as `int | None`. If it is `None`, `socket.connect_ex` will raise a `TypeError` because it expects an integer port. Suggest adding a check for `None` or ensuring it is a valid integer before using it here."
    }
  ]
}
```

Field reference:

| Field | Required | Description |
| --- | --- | --- |
| `instance_id` | Yes | Unique, non-empty sample identifier; result filename is `instance_id.replace("/", "__") + ".json"` |
| `repo` | Yes | Repository name in `owner/name` form |
| `base_commit` | Yes | Base commit used to determine the review range |
| `head_commit` | Yes | Target commit containing the code being reviewed |
| `clone_url` | No | Clone URL; derived from `repo` as a GitHub URL if omitted |
| `reference_comments[]` | No | Annotated reference comments; defaults to an empty list |
| `reference_comments[].path` | Yes | Non-empty path of the referenced file |
| `reference_comments[].start_line` / `end_line` | No | Inclusive line range; positive integers or `null`; equal for a single line |
| `reference_comments[].side` | No | `left` or `right` for the old or new diff side; may be `null` |
| `reference_comments[].text` | Yes | Non-empty reference comment text |

One supplied line endpoint fills the other; reversed endpoints are reordered. Default file-level scoring does not filter by line or `side`; only line information is passed to the judge as context. Legacy per-comment scoring filters by `side`, line range, and distance.

Loading checks required fields, line values, and duplicate `instance_id` values. Errors identify the JSONL line. With `--limit`, only loaded samples are checked. See [schema.py](schema.py) for the definitions.

## Configuration

### Models and Network

`pipeline` reads this directory's `.env` at startup; matching variables passed from the terminal take precedence. Configure the selected reviewer and judge:

| Purpose | Variables |
| --- | --- |
| OCR review | `OCR_LLM_URL`, `OCR_LLM_TOKEN`, `OCR_LLM_MODEL` |
| Claude Code review | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL` |
| Codex review | `CODEX_API_KEY`; select a model with `CODEX_MODEL` |
| Judge evaluation | `JUDGE_BASE_URL`, `JUDGE_API_KEY`, `JUDGE_MODEL` |

| Optional variable | Meaning |
| --- | --- |
| `OCR_VERSION`, `CLAUDE_CODE_VERSION`, `CODEX_VERSION` | Reviewer versions in the Docker image; omitted or blank values use `latest`. Rebuild the image after changing a reviewer version |
| `OCR_USE_ANTHROPIC` | `true` uses Anthropic protocol; `false` uses OpenAI protocol |
| `CLAUDE_CODE_MAX_RETRIES` | Claude Code retries; default 10 |
| `CODEX_GATEWAY_URL` | Codex-compatible model gateway |
| `CODEX_HOME_BASE`, `CODEX_HOME_BACKUP` | Parent directory for temporary Codex configuration, and where to save it after review |
| `JUDGE_USE_MOCK` | Use local similarity when `true` |
| `JUDGE_CONCURRENCY` | Concurrent file-level judge requests; default 3 |
| `HF_ENDPOINT` | Hugging Face service or mirror |
| `NETWORK_TIMEOUT_SECONDS` | Timeout for connection checks and supported download requests; default 60 seconds, excluding judge requests and total run time |

If `CODEX_MODEL` is omitted, Codex uses its default model. Without `CODEX_HOME_BACKUP`, the generated directory is cleaned up after review. Missing `JUDGE_API_KEY` or enabled `JUDGE_USE_MOCK` selects local simulation, intended only for workflow checks. Evaluation logs identify the active judge mode.

### Run Parameters

| Option | Default | Meaning |
| --- | --- | --- |
| `--reviewer` | Required | `ocr`, `claude`, or `codex` |
| `--dataset` | Required | Converted JSONL file |
| `--stage` | `all` | `all`: review and evaluation; `review`: review only; `eval`: evaluation only |
| `--docker` | Off | Run review generation in Docker containers and save results on the host; metric evaluation still runs on the host; invalid with `--stage eval` |
| `--run-id` | Generated for review | Run name; evaluation-only commands using default output directories need an existing run name |
| `--limit` | All | First N JSONL samples; use a positive integer |
| `--concurrency` | `1` | Concurrent repository groups on the host (serial within each repository), or PR containers with `--docker` |
| `--timeout-minutes` | `30` | Review timeout per sample, in minutes |
| `--eval-rounds` | `1` | Judge-evaluation rounds; use a positive integer |
| `--repo-dir` | `repo/` | Program-managed repository cache for host review; unused in Docker mode |
| `--results-dir` | Automatic | Write review results and metrics directly to the given directory instead of a named run directory |
| `--ocr-command` | `ocr` | OCR command on PATH or absolute binary path; paths used in Docker mode must exist inside the image |
| `--max-tools` | `30` | Per-file tool limit passed to OCR; accepted range is determined by OCR |
| `--preview` | Off | Check repositories and diffs without review-model calls; use with `--stage review`. Docker preview skips evaluation automatically |
| `--use-deprecated-score` | Off | Legacy per-comment semantic and line scoring |
| `--line-k` | `1` | Line tolerance for legacy scoring only |

The review-process timeout is the value of `--timeout-minutes` plus five minutes for OpenCodeReview or one minute for Claude Code/Codex. Network and review timeouts are separate; `--timeout-minutes` does not control judge requests.

### Conversion Parameters

| Option | Default | Meaning |
| --- | --- | --- |
| `--version` | `v1` | Select `v1` or `v2` positive data |
| `--input` | Version-specific path | Raw JSON file |
| `--output` | Version-specific path | JSONL destination |
| `--limit` | All | Shuffle the raw data, then select the first N records |
| `--seed` | Random each time | Fix sample order |
| `--validate` | Off | Check the converted data format, not annotation correctness |

v1 outputs `data/aacr_bench.jsonl`; v2 outputs `data/aacr_bench_v2.jsonl`. Raw data can be downloaded and verified from its metadata file on first use. Custom AACR-Bench output names should retain `v1` or `v2` so connection checks can identify the version.

## Quick Start

This example uses the **v2 dataset and OpenCodeReview**. Obtain this repository's source, then start in the `aacr-bench` directory.

Python 3.11 or newer is supported; the example uses Python 3.12. Install [uv](https://docs.astral.sh/uv/) and Git. OpenCodeReview requires Git 2.41 or newer. Installing reviewer packages through npm also requires Node.js and npm.

### 1. Install

```bash
cd evaluation
uv venv .venv --python 3.12
uv pip install -r requirements.txt
source .venv/bin/activate
npm install -g @alibaba-group/open-code-review
```

The example installs OpenCodeReview. Other reviewers can be installed with:

| Reviewer | Install command |
| --- | --- |
| Claude Code | `npm install -g @anthropic-ai/claude-code` |
| Codex | `npm install -g @openai/codex` |

Run all subsequent commands inside `evaluation/` with the virtual environment active.

### 2. Configure Models

```bash
if [ ! -e .env ] && [ ! -L .env ]; then
  cp -n .env.example .env
fi
chmod 600 .env
```

The copy preserves an existing `.env`. Edit that file using [.env.example](.env.example) to configure the review model and judge:

| Purpose | Variables |
| --- | --- |
| OpenCodeReview review | `OCR_LLM_URL`, `OCR_LLM_TOKEN`, `OCR_LLM_MODEL`; optional `OCR_USE_ANTHROPIC` |
| Judge evaluation | `JUDGE_BASE_URL`, `JUDGE_API_KEY`, `JUDGE_MODEL` |

The program loads `.env` automatically; matching variables passed from the terminal take precedence. Missing `JUDGE_API_KEY` or `JUDGE_USE_MOCK=true` selects local similarity for workflow checks. Configure a real judge to compare model performance.

### 3. Convert and Preview

```bash
python -m converters.aacr_bench --version v2 --seed 42 --validate

python -m pipeline run --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl \
  --limit 2 --preview --run-id v2-preview
```

Conversion randomly orders the samples; using the same random seed, such as `--seed 42`, with the same input produces the same sample order. `--limit 2` processes only the first two samples in the converted JSONL file (one JSON object per line). Conversion overwrites its output; you should use a new `--output` path when changing the input or sampling options, and preserve the original data file and order for saved runs. Preview checks the required commits and diff without calling the review model; `--stage review` prevents judge evaluation at this step.

### 4. Review and Evaluate

```bash
python -m pipeline run --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl \
  --limit 2 --run-id v2-baseline
```

The review stage calls the review model to generate comments. Judge evaluation calls the judge model for semantic matching in real-judge mode, or calculates local similarity in simulation mode. The program then computes metrics. Both stages process the same first two samples.

Review results are saved to `results/aacr_bench_v2/ocr/v2-baseline/`, and metrics to `metrics/aacr_bench_v2/ocr/v2-baseline/`. Use a different run name for a new review to preserve earlier results.

For v1, select `--version v1` and use `data/aacr_bench.jsonl`. After configuring the corresponding agent and model, select `--reviewer claude` or `--reviewer codex` to use another reviewer.


## Common Operations

### Staged Runs

Review first, then evaluate saved results:

```bash
python -m pipeline run --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id v2-review

python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id v2-review
```

Use the same data file and order, reviewer, sample limit, and run name in both commands. Use a new name for a new review experiment; using the same name again can overwrite existing results. Use a name that begins and ends with a letter or digit, with periods, underscores, or hyphens inside, such as `v2-review`. For evaluation, use the directory name shown in the review log.

### Concurrency and Repeated Evaluation

Add `--concurrency 4` to a review command; actual parallelism is limited by selected repository count. Judge concurrency is controlled separately by `JUDGE_CONCURRENCY`.

To evaluate saved results again:

```bash
python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl \
  --limit 2 --run-id v2-review --eval-rounds 1
```

Each command uses the saved review results. Preserve the previous metrics file before comparing evaluations.

### Inspect Results

`<dataset>` in default output paths is the JSONL filename without its extension:

| Location | Contents |
| --- | --- |
| `results/<dataset>/<reviewer>/<run-name>/` | Review results for each sample and `summary_<reviewer>.json` |
| `metrics/<dataset>/<reviewer>/<run-name>/` | Metric summary and matches for each sample |

Check the sample count and statuses in the review summary before interpreting metrics; exit code 0 does not establish that all samples succeeded. Missing or unreadable files, malformed JSON, and non-object results count toward `missing_instances` and are excluded from denominators; empty comments in a parsed object are still evaluated. Saved failures or unstructured responses may also be treated as zero comments; inspect their status and result fields. Judge errors can produce empty matches, so check the logged mode and errors. Model usage and duration summaries cover the review stage only.

Use `python -m pipeline run --help` and `python -m converters.aacr_bench --help` for command-line usage.

### Docker Review

Use `--docker` to generate review comments in containers while evaluation stays on the host. See the [Docker guide](docker/README.md) for image builds, versions, resources, and logs.

## FAQ

### Why is `missing_instances` large?

It counts samples whose result could not be loaded: a missing or unreadable file, malformed JSON, or a non-object result. Check the run name, data file, `--limit`, and review summary. Saved results without parsable comments still enter evaluation, so the missing count alone does not establish review success.

### Why are semantic scores all zero?

Check the logged judge mode and the `JUDGE_API_KEY`, `JUDGE_MODEL`, and service endpoint settings. For a real judge, also check model-call errors and whether structured review comments were saved. A written metrics file does not establish that every judge call succeeded; local simulation scores are not for comparing model quality.

### Where are repositories cloned? Will they be cloned again?

The default cache is `repo/<owner__repo>/`, configurable with `--repo-dir`. Usable caches are reused. An unusable cache or missing required commit can cause another snapshot download or clone. Docker review prepares the repository in each container and does not require a host cache.
