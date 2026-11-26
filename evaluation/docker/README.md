# Docker Review

English | [简体中文](README.zh-CN.md)

`python -m pipeline run --docker` prepares and reviews each PR in its own container. The image contains the OCR, Claude Code and Codex CLIs, so they do not need to be installed on the host. Results and logs are saved on the host, where metric evaluation also runs.

## Build

Prepare and activate the host Python environment using the [evaluation guide](../README.md), and make sure the Docker CLI can reach a Docker daemon. Then build the image from the repository root:

```bash
cd evaluation
python docker/build_image.py --image aacr-review:latest
```

Run the remaining commands from `evaluation/`. The image includes Python and Git. Node.js is downloaded and installed during the build, using the latest long-term support (LTS) release by default.

Choose reviewer CLI versions with these optional variables. The build script uses a nonempty shell value first, then `evaluation/.env`, then `latest` if neither provides a value:

| Variable | Default |
| --- | --- |
| `OCR_VERSION` | `latest` |
| `CLAUDE_CODE_VERSION` | `latest` |
| `CODEX_VERSION` | `latest` |

Use a specific version to pin a CLI. Only these three version fields are read from the file for the build; the environment file, credentials and reference comments are not included in the image.

To use another version file and refresh cached installations:

```bash
python docker/build_image.py --image aacr-review:latest \
  --env-file versions.env --no-cache
```

A cached build can retain an earlier `latest` installation; `--no-cache` fetches current versions. Rebuild after changing evaluation code or reviewer versions. If you request a specific version for the active reviewer, it must match the version recorded in the image; otherwise review stops and asks you to rebuild. Pin versions and record the image ID when you need reproducible runs. Reviewer auto-update is disabled during review.

The build script also accepts `--build-arg` for `NODE_VERSION`, `NPM_REGISTRY`, `APT_MIRROR`, `PIP_INDEX_URL` and `INSTALL_OCR` / `INSTALL_CLAUDE` / `INSTALL_CODEX`. Set an `INSTALL_*` option to `0` to omit that CLI. Node.js is omitted when both Claude Code and Codex are disabled. Build for the Docker host's architecture.

## Configure Models

The pipeline loads `evaluation/.env` automatically; variables already set in the shell take precedence. Configure the reviewer you will use, and the host judge when evaluating results:

| Role | Variables |
| --- | --- |
| OCR | `OCR_LLM_URL`, `OCR_LLM_TOKEN`, `OCR_LLM_MODEL`; optional `OCR_USE_ANTHROPIC` |
| Claude Code | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`; optional `CLAUDE_CODE_MAX_RETRIES` |
| Codex | `CODEX_API_KEY`; optional `CODEX_MODEL`, `CODEX_GATEWAY_URL` |
| Host judge | `JUDGE_BASE_URL`, `JUDGE_API_KEY`, `JUDGE_MODEL`; optional `JUDGE_USE_MOCK`, `JUDGE_CONCURRENCY` |

Containers receive the active reviewer's model settings and credentials, plus optional network settings (`HF_ENDPOINT`, `NETWORK_TIMEOUT_SECONDS`). Judge credentials and reference comments stay on the host, and the environment file is not mounted.

Each container prepares its own repository, trying a Hugging Face snapshot first and falling back to GitHub. It needs network access to those sources and the review model endpoint. A host repository cache or archive is not required.

## Run

Preview repository preparation without calling a model:

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 \
  --run-id docker-preview --preview
```

Generate reviews and stop after the review stage:

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

Generate reviews, then evaluate them on the host:

```bash
python -m pipeline run --docker --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-baseline
```

The default `--stage all` waits for every review task to finish and then evaluates the selected samples on the host. A failed review still keeps any findings produced before the failure, and those findings are included in evaluation. Cancellation or a fatal runner error stops the pipeline. Docker `--preview` skips evaluation.

To retry only the failed samples in an existing run, add `--retry` and keep the same `--run-id`:

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review --retry
```

Use the same dataset and `--limit` as the original run. `--retry` requires an existing run directory, `--run-id`, and a readable `summary_<reviewer>.json`. It archives the failed samples' result JSON (and any MCP `.partial.json`) plus `docker-logs/instances/<attempt>/` directories under `retry-archive/<timestamp>/`, then reruns only the failed samples. Successful results remain unchanged. If archiving fails, the command stops before rerunning. With `--stage all` or `--stage eval`, evaluation continues after the retry only when every selected sample has succeeded; if failures remain, the command exits non-zero and does not evaluate. Repeat `--retry` after fixing the remaining failures. `--docker --stage eval --retry` is allowed for this workflow; plain `--docker --stage eval` is still invalid.

During review, each completed sample prints a line with the local time, sample ID, status, collected finding count, duration, and overall progress:

```text
[14:32:10] [docker] [37/151] owner__repo@abc123 ok findings=6 duration=412.8s
```

To evaluate saved results without retrying, leave out `--docker`:

```bash
python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

`--docker` cannot be combined with `--stage eval` unless `--retry` is also set. Review and evaluation should use the same dataset, reviewer, run ID, and sample limit. `--limit` takes the first N JSONL rows; `--eval-rounds`, `--line-k` and `--use-deprecated-score` keep their evaluation meanings.

Reusing a run ID without `--retry` prints a warning, then reviews every selected PR again, including those that succeeded earlier, and overwrites the selected results. Unselected files remain in place. Check the dataset and run ID before reusing a directory, and do not send concurrent jobs to the same result directory.

## Options and Outputs

| Option | Default and behavior |
| --- | --- |
| `--image` | `aacr-review:latest`, or `AACR_DOCKER_IMAGE`. The resolved image is recorded in the run logs. |
| `--retry` | Off. Archive and rerun only failed samples from an existing `--run-id`; keep successful results. |
| `--concurrency` | 1 PR container at a time. |
| `--cpus` / `--memory` | 1 CPU / 2g per container. The sum of concurrent containers' memory limits must not exceed 50% of the Docker daemon's memory. |
| `--timeout-minutes` | Reviewer timeout defaults to 30 minutes. The container's overall timeout allows extra time for repository preparation, review completion and cleanup, and is at least two hours. |
| `--ocr-command` / `--max-tools` | OCR executable inside the image and the per-file tool limit. |

Containers run as a non-root user with a read-only root filesystem and limits on CPU, memory, process count and writable temporary storage. Codex runs without its own sandbox inside the container; Docker's restrictions still apply. Custom images must set a non-root `USER` and must not declare `VOLUME`.

On Linux, containers use a non-root caller's UID/GID. A root caller uses the image user (UID 1000); the output directory and its parents must allow that user to write, or the pipeline should be run as a non-root user. Other platforms use the image user. Exported files are readable from the host, and per-PR result files copied to the host use mode 0600.

Default output directories:

```text
results/<dataset>/<reviewer>/<run-id>/
metrics/<dataset>/<reviewer>/<run-id>/
```

The results directory contains per-PR JSON files, `summary_<reviewer>.json` and `docker_run.json`. Logs, container IDs and run metadata are in its `docker-logs/` subdirectory. When using `--results-dir`, logs are saved to `<results-dir>/docker-logs/`.

A failed review can still produce partial findings. The runner copies the container's `failed-results/<sample>.json` to the canonical result file, marks it with `review_status: failed` (and `review_error` when available), and keeps those findings for evaluation. If only the MCP `.partial.json` exists for Claude Code or Codex, the runner builds the result from that list. Malformed finding entries are dropped so they cannot break evaluation. Failed attempts that produced no result are written with empty findings. `--retry` archives failed result files, partial files and attempt directories under `retry-archive/<timestamp>/` before rerunning them.

Claude stream and stderr logs, along with available `.collector` hook files, are retained on success and failure under `docker-logs/instances/<attempt>/out/diagnostics/`. Paths in the result JSON refer to these retained files. Missing or rejected diagnostics are reported separately and do not replace the original review error.

Check the review summary before interpreting metrics. Empty comment lists are valid results; previews do not measure model performance. Without `JUDGE_API_KEY`, or with `JUDGE_USE_MOCK` enabled, evaluation uses local similarity instead of a real judge. Configure a real judge for model comparisons.
