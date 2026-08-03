# evaluation — Unified Code Review Evaluation Framework

Integrates **data loading → review execution → evaluation** into a single configurable pipeline.

Core goal: define a **standard data format**. For any new benchmark, just write a converter
to transform it into the standard format, then run review and evaluation with **one command** —
no framework code changes needed.

---

## Directory Structure

```
evaluation/
├── pipeline.py            # CLI entry point + orchestration: python -m pipeline run ...
├── schema.py              # Standard data format (dataclass) + JSONL loading & validation
├── config.py              # Centralized paths / env vars / naming conventions
├── repo_utils.py          # Shared git clone / checkout / workspace cleanup logic
├── reviewers/
│   ├── ocr.py             # OCR (OpenCodeReview) reviewer
│   ├── claude.py          # Claude Code reviewer (MCP incremental reporting + multi-level fallback)
│   └── codex.py           # Codex reviewer (MCP incremental reporting + JSONL fallback, isomorphic to Claude)
├── mcp_finding_server.py        # Stdio MCP findings server for Claude
├── mcp_codex_finding_server.py  # Codex-specific MCP server (different finding schema from Claude)
├── judge.py               # Evaluation core: 4-stage matching + metrics + semantic judgment (LLM/Mock)
├── evaluate.py            # Evaluation orchestration: align data → call judge → aggregate metrics
├── converters/
│   └── aacr_bench.py      # AACR-Bench → standard format converter
├── benchmark/<name>/      # Raw data for each benchmark (data described by *.meta.json, auto-downloaded on first run)
├── data/<name>.jsonl      # Converted standard format datasets (named by benchmark)
├── repo/                  # Repository clone cache (<owner__name>, auto-generated)
├── results/<benchmark>/<reviewer>/<run_id>/   # Review results (isolated per run, no overwrites)
└── metrics/<benchmark>/<reviewer>/<run_id>/   # Evaluation metrics (mirrors results structure)
```

> **Data directory convention**: Raw data goes under `benchmark/<benchmark name>/` (e.g., `benchmark/AACR-Bench/`).
> Converters read from there and output standard data to `data/<benchmark_key>.jsonl` (e.g., `data/aacr_bench.jsonl`),
> named by benchmark to avoid collisions.

---

## Standard Data Format

The dataset is **JSONL**, one sample per line (`ReviewInstance`):

```json
{
  "instance_id": "psf__requests-5711@9484e13",
  "repo": "psf/requests",
  "base_commit": "5351469472eccee7ed1a6cae53341446c520d807",
  "head_commit": "9484e13c7da927119fe82794bb5571cec144b6d7",
  "clone_url": "https://github.com/psf/requests.git",
  "reference_comments": [
    {
      "path": "setup.py",
      "start_line": 46,
      "end_line": 46,
      "side": "left",
      "text": "Review comment body (human-annotated ground truth)"
    }
  ]
}
```

Field reference:

| Field | Required | Description |
| ---- | -------- | ----------- |
| `instance_id` | Yes | Unique sample identifier; result file is `instance_id.replace("/", "__") + .json` |
| `repo` | Yes | `owner/name` format |
| `base_commit` | Yes | Starting commit of the review diff |
| `head_commit` | Yes | Ending commit of the review diff (the code state being reviewed) |
| `clone_url` | No | Falls back to derived GitHub URL from `repo` if omitted |
| `reference_comments[]` | No | Human-annotated reference comments; empty means no ground truth for this sample |
| `reference_comments[].path` | Yes | File path of the comment |
| `reference_comments[].start_line` / `end_line` | No | Closed line interval `[start, end]`; single-line: set both equal; may be `null` |
| `reference_comments[].side` | No | `"left"` or `"right"` — which side of the diff the comment is on; may be `null` |
| `reference_comments[].text` | Yes | Comment body |

> **Line convention**: Reference comments use closed intervals. During evaluation, Claude's single `line` is
> treated as start = end; OCR and Codex `start_line/end_line` are used directly. Matching checks overlap
> or distance ≤ k against the reference interval.

Loading (`schema.load_instances`) performs **per-line validation**: required fields must be non-empty,
line numbers must be positive integers or null, and `instance_id` must be unique. Any invalid line raises
a `SchemaError` with line location, surfacing bad data as early as possible.

---

## Quick Start

### 0. Prepare Environment

This framework ships with its own [uv](https://docs.astral.sh/uv/) virtual environment (`evaluation/.venv`),
with dependencies locked in `evaluation/requirements.txt` — ready to use, isolated from the rest of the project.

> **Prerequisites** (install once before first use):
>
> - [uv](https://docs.astral.sh/uv/): Python environment and dependency management
> - Benchmark raw data needs no Git LFS: each benchmark ships a committed `*.meta.json`
>   (url + sha256); converters **auto-download** and verify the data on first run, caching
>   under `benchmark/<name>/` for reuse.
> - Node.js / npm: Required to install reviewer CLIs.

```bash
# 0) Enter the framework directory (all subsequent commands run inside evaluation/)
cd evaluation

# 1) Create framework-specific virtual environment and install locked dependencies
uv venv .venv --python 3.11
uv pip install -r requirements.txt

# 2) Activate the virtual environment (then use python directly)
source .venv/bin/activate

# 3) Install reviewer CLIs (as needed, global install)
npm install -g @alibaba-group/open-code-review   # ocr reviewer
npm install -g @anthropic-ai/claude-code         # claude reviewer
npm install -g @openai/codex                    # codex reviewer

# 4) Configure LLM: copy the template and fill in real tokens
#    (.env is .gitignore'd, won't be accidentally committed)
cp .env.example .env
# Edit .env to fill in OCR / Claude / Codex / Judge configs, then load into current shell:
set -a && source .env && set +a
```

> **Runtime convention**: All commands run inside `evaluation/` with venv activated:
> `python -m pipeline run ...` (review/eval), `python -m converters.<benchmark> ...` (conversion).
> Claude / Codex MCP server subprocesses reuse the same interpreter via `sys.executable` — no extra config needed.

The four config groups in `evaluation/.env` each serve a distinct purpose:

| Purpose | Variables |
| ------- | --------- |
| OCR Review | `OCR_LLM_URL` / `OCR_LLM_TOKEN` / `OCR_LLM_MODEL` / `OCR_USE_ANTHROPIC` |
| Claude Review | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` |
| Codex Review | `CODEX_API_KEY` (required) / `CODEX_MODEL` (optional) / `CODEX_GATEWAY_URL` (optional) |
| Judge (Evaluation) | `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL` / `JUDGE_USE_MOCK` |

> **Codex config written to temporary config.toml**: The pipeline writes `CODEX_MODEL` (and
> `CODEX_GATEWAY_URL`, if provided) into `/tmp/codex_home.XXXXXX/config.toml` at the start of the
> review stage, then redirects Codex via `CODEX_HOME=<tmp>` to isolate it from the user's
> `~/.codex/config.toml`, ensuring reproducibility. When `CODEX_GATEWAY_URL` is set, the pipeline
> automatically generates a `model_provider = "gateway"` + `[model_providers."gateway"]` section,
> redirecting Codex to the custom endpoint (e.g. MaaS / cc_switch) instead of `api.openai.com`.
> The MCP server inherits `REVIEW_RESULTS_DIR` / `REVIEW_INSTANCE_ID` from the parent process via `env_vars`.

> **Note**: The model must be compatible with Codex's tool calling — models that cannot call MCP tools will fail to report findings.

> When Judge lacks `JUDGE_API_KEY` or has `JUDGE_USE_MOCK=true`, semantic matching automatically falls
> back to local mock similarity (zero cost, for pipeline validation only; use a real judge model for
> production evaluation).

### 1. Convert a Benchmark to Standard Format

> All commands below run inside `evaluation/` (with venv activated).

Place raw data under the conventional location `benchmark/<benchmark name>/`, and the corresponding
converter will read it by default, producing `data/<benchmark_key>.jsonl`. AACR-Bench converter is
built-in:

```bash
# AACR-Bench (raw data benchmark/AACR-Bench/ -> data/aacr_bench.jsonl)
python -m converters.aacr_bench --validate

# --validate: re-load with schema after conversion to verify output validity
# --limit N: shuffle all records then take the first N (random sampling; omit for full dataset)
# --seed N: random seed for shuffling; makes the sample reproducible (for fixed evaluation subsets)
# You can also explicitly specify input / output:
python -m converters.aacr_bench \
    --input benchmark/AACR-Bench/positive_samples.json \
    --output data/aacr_bench.jsonl \
    --limit 30 --seed 42 --validate
```

> **Sampling randomness**: The converter shuffles raw data before applying `--limit`, so `--limit`
> produces a **random subset** rather than a fixed prefix. Use `--seed` for reproducible subsets.

**Converter parameters** (`python -m converters.<key> [options]`):

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `--input` | Conventional path | Raw data file path (default `benchmark/<benchmark name>/<raw file>`) |
| `--output` | Conventional path | Output standard JSONL path (default `data/<key>.jsonl`) |
| `--limit` | All | Shuffle then take first N (random sampling; omit for full dataset) |
| `--seed` | Random each time | Random seed for shuffling; makes the sample reproducible |
| `--validate` | Off | Re-load with schema after conversion to verify output validity |

### 2. One-Click Run (Review + Evaluation)

```bash
set -a && source .env && set +a

# OCR reviewer, first 5 samples
python -m pipeline run --stage all --reviewer ocr --dataset data/aacr_bench.jsonl --limit 5 --concurrency 1 --run-id baseline

# Claude reviewer, first 2 samples, review then evaluate
python -m pipeline run --stage all --reviewer claude --dataset data/aacr_bench.jsonl --limit 2 --concurrency 1 --run-id baseline

# Codex reviewer, first 2 samples
python -m pipeline run --stage all --reviewer codex --dataset data/aacr_bench.jsonl --limit 2 --concurrency 1 --run-id baseline
```

---

## CLI Reference

> Run inside `evaluation/` (with venv activated).

```
python -m pipeline run --reviewer {ocr|claude|codex} --dataset <standard JSONL> [options]
```

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `--reviewer` | (required) | Reviewer: `ocr` / `claude` / `codex` |
| `--dataset` | (required) | Standard format dataset path, e.g. `data/aacr_bench.jsonl` |
| `--stage` | `all` | `all` (review + eval) / `review` (review only) / `eval` (eval only) |
| `--repo-dir` | `repo/` | Repository clone directory (cached for reuse) |
| `--results-dir` | `results/<benchmark>/<reviewer>/<run_id>` | Directly specify results directory (advanced, bypasses run mechanism); auto-derived from dataset otherwise |
| `--run-id` | Timestamp | Directory name for this run (e.g. `baseline`, `v2`); auto-generated timestamp if omitted for review; **must be explicitly specified for eval** |
| `--limit` | All | Only process the first N samples of the dataset (randomness is determined at conversion stage, see converter `--seed` above) |
| `--line-k` | `1` | Evaluation line matching tolerance (overlap or distance ≤ k counts as a hit) |
| `--timeout-minutes` | `30` | Per-sample review timeout |
| `--eval-rounds` | `1` | Number of evaluation rounds; multi-round runs N independent evaluations then averages the metrics |
| `--ocr-command` | `ocr` | OCR reviewer executable path; can specify a local release binary (e.g. `./ocr-v2.0.0`), for ocr reviewer only |
| `--max-tools` | `30` | OCR max tool call rounds per file (controls per-file review depth), for ocr reviewer only |
| `--concurrency` | `1` | Number of concurrent repo groups during review (`1` = fully serial); see "Concurrent Review" below |
| `--preview` | Off | Clone/checkout only, do not actually invoke the review LLM |

### Concurrent Review (`--concurrency`)

Each sample's review involves `checkout` / `clean` on its repository. Samples sharing the same repo
use the same local workspace (`repo/<owner__name>`), so concurrency would cause conflicts. The
concurrency model groups by `repo`:

- **Inter-group concurrency**: Samples from different repos can be reviewed in parallel, up to `--concurrency` repo groups
- **Intra-group serial**: Samples from the same repo are strictly sequential, no interference
- `--concurrency 1` (default) = fully serial, same as pre-concurrency behavior

This only applies to the review stage; evaluation is purely local computation + LLM judgment and is unaffected.

```bash
# 4 repo groups reviewed in parallel (samples within the same repo remain serial)
python -m pipeline run --stage review --reviewer ocr \
    --dataset data/aacr_bench.jsonl --concurrency 4
```

> Actual concurrency is clamped to `min(--concurrency, number of repo groups)`.

### Staged Runs + Multi-Eval Comparison (Run Mechanism)

Each review automatically creates an isolated run directory `results/<benchmark>/<reviewer>/<run_id>/`.
Both review results and evaluation metrics live under this directory, so **multiple runs never overwrite
each other**, making cross-run comparison easy.

```bash
# Review only (auto-creates a timestamped run directory)
python -m pipeline run --stage review --reviewer claude --dataset data/aacr_bench.jsonl --limit 5

# Give the run a meaningful name (the run directory will be called "baseline")
python -m pipeline run --stage review --reviewer claude --dataset data/aacr_bench.jsonl --run-id baseline

# Run again with different parameters, no overwrites
python -m pipeline run --stage review --reviewer claude --dataset data/aacr_bench.jsonl --run-id run2

# Evaluate only (--run-id is required)
python -m pipeline run --stage eval --reviewer claude --dataset data/aacr_bench.jsonl --run-id baseline

# Multi-round evaluation averaging (reduces single-run variance)
python -m pipeline run --stage eval --reviewer claude --dataset data/aacr_bench.jsonl --run-id baseline --eval-rounds 3
```

---

## How the Three Stages Work

### Data Loading (schema.py)

Read standard JSONL → per-line validation → produce `ReviewInstance` list. `--limit N` takes the
first N samples in **original data order**.

### Review Execution (reviewers/)

For each sample: `clone/fetch` → `checkout head_commit` → invoke reviewer → write `<safe_id>.json`.
Review progress includes a **progress bar** (completed / total + real-time rate + ETA), useful for
estimating total runtime. Multi-repo concurrent progress is thread-safe (see "Concurrent Review" above).

**Workspace cleanup (unified across all three reviewers)**: `git reset --hard && git clean -fdx` runs
**before and after** each review (pre-review cleanup is built into `checkout_commit`; post-review cleanup
is done by each reviewer's `clean_worktree` after saving results). This prevents temporary files or
changes left by reviewers from polluting the next checkout.

- **OCR**: `ocr review --from base --to head --format json`, results in `review.comments[]`.
- **Claude**: Official `/code-review <base>...<head>`, structured output via two channels:
  1. **MCP incremental reporting**: Claude calls the `report` tool once per confirmed finding, writing
     to `<safe_id>.partial.json`, decoupled from the final text format;
  2. **stdout parsing fallback**: Handles both single JSON envelopes and stream-json output (models may
     return stream-json even when asked for JSON), extracts final text and parses findings; unparseable
     output is treated as empty review or preserved as raw text.
     The channel with **more findings** wins (MCP preferred on ties). `review_output_source` is marked
     as `mcp_stream` or `stdout_fallback`.
- **Codex**: `codex exec "/review <base>...<head>"`, fully isomorphic dual-channel strategy with Claude:
  1. **MCP incremental reporting** (primary): Codex-specific MCP server (`mcp_codex_finding_server.py`)
     exposes `report(file, summary, description, start_line, end_line, severity)`, with finding fields
     `{file, start_line, end_line, severity, summary, description}` (different schema from Claude).
  2. **JSONL fallback** (secondary): `--json` output produces a stream of JSONL events; parses
     `item.completed`'s `agent_message` text as fallback, and extracts token usage from
     `turn.completed.usage`.
     The channel with **more findings** wins (MCP preferred on ties). `review_output_source` is marked
     as `mcp_stream` or `jsonl_fallback`. Model config and MCP settings are written into a temporary
     `$CODEX_HOME/config.toml` (Plan D), loaded via `CODEX_HOME=<tmp>` redirection.

### Evaluation (evaluate.py + judge.py)

Locates result files by `instance_id` (`<safe_id>.json`), then runs local `judge.py` for
four-stage matching (path → side → line(k) → semantic):

- Samples with missing result files are counted in `missing_instances` and **excluded from metric
  denominators** (avoiding treating un-run samples as 0 recall).
- Outputs both **semantic** and **line-based** Precision / Recall / F1.

`note` (semantic comparison text) mapping: reference uses `text`, OCR uses `content`, Claude uses
`summary + failure_scenario`, Codex uses `summary + description` (semantically expanded to failure
scenario + suggested fix). Codex's `severity` field is archived only, not used in evaluation.

---

## Adding a New Benchmark (4 Steps)

**Just write a converter — zero changes to the rest of the framework.** The `benchmark_key` convention
uses lowercase underscores (e.g. `aacr_bench`), which determines the output filename `data/<key>.jsonl`,
results directory `results/<key>/`, and metrics directory `metrics/<key>/`.

Use the built-in `converters/aacr_bench.py` as a reference example:

1. **Provide raw data**: Commit a same-named `*.meta.json` (fields: `url`, `sha256`, optional
   `filename`, `description`) under `benchmark/<benchmark name>/` (e.g. `benchmark/AACR-Bench/`).
   The converter reads it to **auto-download** and verify the data on first run (see
   `aacr_bench.py`'s `ensure_raw_file`); the data file itself is gitignored, never committed.
   `filename` (defaults to the meta's stem) lets the data file be named differently from the meta.
2. **Write a converter**: Copy `converters/aacr_bench.py` and adapt it. The core is `convert_record()`
   which transforms each raw record into a `ReviewInstance`. Reuse the path conventions:
   - Default input: `config.benchmark_raw_dir("<benchmark name>") / <raw filename>`
   - Default output: `config.dataset_path("<key>")` (i.e. `data/<key>.jsonl`)

   Run conversion: `python -m converters.<key> --validate`

3. **Run the pipeline**:
   `python -m pipeline run --stage all --reviewer <ocr|claude|codex> --dataset data/<key>.jsonl`
4. **Check metrics**: Results are in `metrics/<key>/<reviewer>/<run_id>/metrics_<reviewer>_<timestamp>.json`;
   the console also prints a summary.

> **Built-in converter reference**: AACR-Bench reads a **whole JSON array**, parses `owner/name` from
> `githubPrUrl` via regex, and generates `instance_id` as `owner__name@<head first 7 chars>`.
> New benchmarks should follow their own format in the same pattern.

---

## Output Files

> `<benchmark>` in paths is derived from the `--dataset` filename (e.g. `data/aacr_bench.jsonl` → `aacr_bench`);
> `<run_id>` is the directory name for this run, defaulting to a timestamp, or the name given via `--run-id`.
> Review results go under `results/`, evaluation metrics under `metrics/` — mirrored structure (same run_id),
> stored separately but clearly correlated.

| File | Content |
| ---- | ------- |
| `results/<benchmark>/<reviewer>/<run_id>/<safe_id>.json` | Review result for a single sample |
| `results/<benchmark>/<reviewer>/<run_id>/summary_<reviewer>.json` | Status summary for each sample in the review stage |
| `metrics/<benchmark>/<reviewer>/<run_id>/metrics_<reviewer>_<timestamp>.json` | Evaluation metrics (including summary, per-sample match flags, and `missing_instance_ids`) |

---

## FAQ

**Q: Many samples are skipped entirely (large `missing_instances`)?**
This means those samples don't have a corresponding `<safe_id>.json` in the results directory — the review
stage hasn't run them yet. This is different from "model produced no findings" — in that case, a result file
would exist but with an empty `review_output`, and would still enter evaluation and pull down recall.

**Q: Semantic metrics are all zero during evaluation?**
Judge is likely running in Mock mode (local similarity struggles with significant wording differences).
Configure `JUDGE_API_KEY` in `evaluation/.env` and set `JUDGE_USE_MOCK=false`, then re-run to use a real
judge model (the evaluation stage reads this file automatically).

**Q: Where are repos cloned? Will they be cloned repeatedly?**
Repos are cloned to `evaluation/repo/<owner__name>`. The same repo is cached and reused — only `fetch` is
run, never a repeated clone.