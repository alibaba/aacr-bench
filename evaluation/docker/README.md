# Docker Review

English | [简体中文](README.zh-CN.md)

Use `python -m pipeline run --docker` to prepare repositories and generate reviews in one container per PR. Metrics are computed on the host.

## Build

Install the host Python dependencies and activate the environment as described in the [evaluation guide](../README.md). Docker review requires the Docker CLI and a reachable daemon.

From the repository root:

```bash
cd evaluation
python docker/build_image.py --image aacr-review:latest
```

Run subsequent commands from `evaluation/`. The image includes Python, Git and the OCR, Claude Code and Codex CLIs. Node.js is downloaded and installed during the build, using the latest long-term support (LTS) release by default.

The build helper reads the following optional version fields from `evaluation/.env`. Nonempty shell values take priority; unset or empty values fall back to the configuration file, then `latest`.

| Variable | Default |
| --- | --- |
| `OCR_VERSION` | `latest` |
| `CLAUDE_CODE_VERSION` | `latest` |
| `CODEX_VERSION` | `latest` |

Set `OCR_VERSION`, `CLAUDE_CODE_VERSION`, or `CODEX_VERSION` to pin a reviewer CLI version. The Docker image installs all selected versions and runs each reviewer with its configured model; Codex runs without its own sandbox inside the container, while the container's read-only root filesystem, tmpfs mounts, and resource limits still apply.

Select another version file or refresh cached installation layers:

```bash
python docker/build_image.py --image aacr-review:latest \
  --env-file versions.env --no-cache
```

Only these version fields are passed from the file to Docker; credentials and the file itself are excluded from the image. A cached build may reuse an earlier `latest` installation; use `--no-cache` to fetch current versions.

The helper also accepts `--build-arg` for installation settings such as `NODE_VERSION`, `NPM_REGISTRY`, `APT_MIRROR`, `PIP_INDEX_URL` and `INSTALL_OCR` / `INSTALL_CLAUDE` / `INSTALL_CODEX`. Set `INSTALL_OCR`, `INSTALL_CLAUDE`, or `INSTALL_CODEX` to `0` to omit the corresponding CLI. Node is omitted when both Claude Code and Codex are disabled. Build for the Docker host's architecture.

Rebuild after changing a reviewer version. An explicit non-`latest` version for the active reviewer must match the version recorded in the image; otherwise review stops with a rebuild error. For reproducible runs, select specific versions and record the image ID.

Reviewer auto-update is disabled during review.

## Configure Models

The pipeline loads `evaluation/.env` automatically; existing shell values take priority. Configure the selected reviewer and host judge:

| Role | Variables |
| --- | --- |
| OCR | `OCR_LLM_URL`, `OCR_LLM_TOKEN`, `OCR_LLM_MODEL`; optional `OCR_USE_ANTHROPIC` |
| Claude Code | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL` |
| Codex | `CODEX_API_KEY`; optional `CODEX_MODEL`, `CODEX_GATEWAY_URL` |
| Host judge | `JUDGE_BASE_URL`, `JUDGE_API_KEY`, `JUDGE_MODEL`; optional `JUDGE_USE_MOCK`, `JUDGE_CONCURRENCY` |

Only the active reviewer's model configuration, credentials and optional network settings (`HF_ENDPOINT`, `NETWORK_TIMEOUT_SECONDS`) are taken from the host. Judge credentials and reference comments stay on the host. The environment file is not mounted.

Each container downloads and prepares its repository using Hugging Face snapshots with GitHub fallback. It needs network access to repository sources and the review model endpoint; no host repository cache or archive is required.

On Linux, non-root callers are automatically mapped to their UID/GID so containers can write to the output mount. Root callers use the image user (UID 1000); ensure the results directory and its parents are writable by that user, or run the pipeline as a non-root user. Other platforms use the image user. Exported files are readable by the host; per-PR result files copied to the host use mode 0600.

## Run

Preview repository preparation without model calls:

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 \
  --run-id docker-preview --preview
```

Generate reviews only:

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

Generate reviews and evaluate:

```bash
python -m pipeline run --docker --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-baseline
```

The default `--stage all` waits for all review tasks to finish, then evaluates on the host. Review failure or cancellation stops automatic evaluation. Docker `--preview` skips evaluation.

Evaluate saved results without `--docker`:

```bash
python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

`--docker` and `--stage eval` cannot be combined. Review and evaluation must use the same dataset, reviewer, run ID and sample limit. `--limit` selects the first N JSONL rows; `--eval-rounds`, `--line-k` and `--use-deprecated-score` retain their evaluation meanings.

Reusing a Docker run ID prints a warning and re-reviews all selected PRs, overwriting their results. Successful old reviews are not skipped, and unselected files remain. Check the dataset/run ID and do not run concurrent jobs into the same result directory.

## Options and Outputs

| Option | Default / meaning |
| --- | --- |
| `--image` | `aacr-review:latest`, or `AACR_DOCKER_IMAGE`; the resolved image is recorded in run logs. |
| `--concurrency` | 1 concurrent PR container. |
| `--cpus` / `--memory` | 1 CPU / 2g per container; combined memory limits must not exceed 50% of daemon memory. |
| `--timeout-minutes` | 30-minute reviewer timeout. |
| `--ocr-command` / `--max-tools` | OCR executable inside the image and per-file tool limit. |

Containers run as a non-root user with a read-only root filesystem and limits on CPU, memory, process count, and writable temporary storage.

Default output directories:

```text
results/<dataset>/<reviewer>/<run-id>/
metrics/<dataset>/<reviewer>/<run-id>/
```

Results include per-PR JSON, `summary_<reviewer>.json` and `docker_run.json`. Attempt logs, container IDs and metadata are under `docker-logs/` inside the run directory. With the advanced `--results-dir` override, logs go to `<results-dir>/docker-logs/`.

Check summary statuses before evaluating saved results. Empty comment lists are valid; preview is not a model-performance result. Missing `JUDGE_API_KEY` or enabled `JUDGE_USE_MOCK` selects local similarity. Use a real judge for model comparisons.
