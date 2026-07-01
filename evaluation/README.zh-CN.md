# eval_all — 统一代码审查评测框架

把 **数据加载 → 执行评审 → 执行评测** 三个阶段整合成一条可配置流水线。

核心目标：定义一套**标准数据格式**。任何新 benchmark 出来后，只需写一个转换器
把它转成标准格式，就能**一键运行**评审与评测，无需改动框架代码。

---

## 目录结构

```
eval_all/
├── pipeline.py            # CLI 入口 + 编排：python -m pipeline run ...
├── schema.py              # 标准数据格式（dataclass）+ JSONL 加载校验
├── config.py              # 路径 / 环境变量 / 命名规则集中配置
├── repo_utils.py          # git clone / checkout / 清理工作区 公共逻辑
├── reviewers/
│   ├── ocr.py             # OCR（OpenCodeReview）评审器
│   ├── claude.py          # Claude Code 评审器（含 MCP 增量上报 + 多级兜底）
│   └── codex.py           # Codex 评审器（MCP 增量上报 + JSONL 兜底，与 Claude 同构）
├── mcp_finding_server.py        # Claude 评审用的 stdio MCP findings server
├── mcp_codex_finding_server.py  # Codex 评审专用（finding schema 与 Claude 版不同）
├── judge.py               # 评测核心：四阶段匹配 + 指标统计 + 语义判定（LLM/Mock）
├── evaluate.py            # 评测编排：对齐数据 -> 调 judge -> 汇总指标
├── converters/
│   └── aacr_bench.py      # AACR-Bench -> 标准格式 转换器
├── benchmark/<名字>/      # 各 benchmark 的原始数据（约定输入位置，大文件走 Git LFS）
├── data/<名字>.jsonl      # 转换后的标准格式数据集（按 benchmark 命名）
├── repo/                  # 仓库 clone 缓存（<owner__name>，自动生成）
├── results/<benchmark>/<reviewer>/<run_id>/   # 评审结果（每次 run 独立目录，防覆盖）
└── metrics/<benchmark>/<reviewer>/<run_id>/   # 评测指标（与 results 镜像对齐）
```

> **数据目录约定**：原始数据放 `benchmark/<benchmark名>/`（如 `benchmark/AACR-Bench/`），
> 转换器读取后产出标准数据到 `data/<benchmark_key>.jsonl`（如 `data/aacr_bench.jsonl`），
> 按 benchmark 命名避免多个 benchmark 互相覆盖。

---

## 标准数据格式

数据集为 **JSONL**，每行一个样本（`ReviewInstance`）：

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
      "text": "评论正文（人工标注的参考答案）"
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| ---- | ---- | ---- |
| `instance_id` | 是 | 样本唯一标识；结果文件名为 `instance_id.replace("/", "__") + .json` |
| `repo` | 是 | `owner/name` 形式 |
| `base_commit` | 是 | 评审 diff 的起点 commit |
| `head_commit` | 是 | 评审 diff 的终点 commit（被评审的代码状态） |
| `clone_url` | 否 | 缺省按 `repo` 推导 GitHub 地址 |
| `reference_comments[]` | 否 | 人工标注的参考评论；为空表示该样本无参考答案 |
| `reference_comments[].path` | 是 | 评论所在文件路径 |
| `reference_comments[].start_line` / `end_line` | 否 | 行号闭区间 `[start, end]`；单点令两者相等；可为 `null` |
| `reference_comments[].side` | 否 | `"left"` 或 `"right"`，评论附着在 diff 的哪一侧；可为 `null` |
| `reference_comments[].text` | 是 | 评论正文 |

> **行号约定**：参考评论统一用闭区间。评测时，Claude 的单个 `line` 视为起始=终止，
> OCR 与 Codex 的 `start_line/end_line` 直接使用，与参考的区间做重叠 / 距离 ≤ k 的匹配。

加载时（`schema.load_instances`）会**逐行校验**：必填字段非空、行号为正整数或 null、
`instance_id` 不重复。任何非法行都会带行号定位抛 `SchemaError`，让问题数据尽早暴露。

---

## 快速开始

### 0. 准备环境

本框架自带独立的 [uv](https://docs.astral.sh/uv/) 虚拟环境（`eval_all/.venv`），
依赖锁定在 `eval_all/requirements.txt`，开箱即用、与项目其它部分隔离。

> **前置工具**（首次使用需先装好）：
>
> - [uv](https://docs.astral.sh/uv/)：Python 环境与依赖管理
> - [Git LFS](https://git-lfs.com/)：`benchmark/` 下的大文件（数据集）通过 LFS 存储。
>   **若不装 LFS，clone 后 `benchmark/` 里只是文本指针而非真实数据，转换器会失败。**
>   安装后执行一次 `git lfs install`；已 clone 过的仓库补拉数据：`git lfs pull`。
> - Node.js / npm：安装评审器 CLI 所需。

```bash
# 0) 首次：安装并初始化 Git LFS（macOS 示例）
brew install git-lfs && git lfs install

# 1) 进入框架目录（后续所有命令都在 eval_all/ 内执行）
cd eval_all

# 2) 若是刚 clone 的仓库，拉取 benchmark 大文件（LFS 实体）
git lfs pull

# 3) 创建框架专属虚拟环境并安装锁定依赖
uv venv .venv --python 3.11
uv pip install -r requirements.txt

# 4) 激活虚拟环境（之后直接用 python 即可）
source .venv/bin/activate

# 5) 安装评审器 CLI（按需，全局安装）
npm install -g @alibaba-group/open-code-review   # ocr 评审器
npm install -g @anthropic-ai/claude-code         # claude 评审器
npm install -g @openai/codex                    # codex 评审器

# 6) 配置 LLM：从模板复制出 .env 并填入真实 token
#    （.env 已被 .gitignore 忽略，不会误提交泄露）
cp .env.example .env
# 编辑 .env 填好 OCR / Claude / Codex / Judge 四套配置后加载到当前 shell：
set -a && source .env && set +a
```

> **运行约定**：所有命令都在 `eval_all/` 目录内、激活 venv 后执行：
> `python -m pipeline run ...`（评审/评测）、`python -m converters.<benchmark> ...`（转换）。
> Claude / Codex 评审用的 MCP server 子进程通过 `sys.executable` 复用同一解释器，无需额外配置。

`eval_all/.env` 中四套配置各司其职：

| 用途 | 变量 |
| ---- | ---- |
| OCR 评审 | `OCR_LLM_URL` / `OCR_LLM_TOKEN` / `OCR_LLM_MODEL` / `OCR_USE_ANTHROPIC` |
| Claude 评审 | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` |
| Codex 评审 | `CODEX_API_KEY`（必填）/ `CODEX_MODEL`（可选）/ `CODEX_GATEWAY_URL`（可选） |
| 评测裁判（Judge） | `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL` / `JUDGE_USE_MOCK` |

> **Codex 配置写入临时 config.toml**：pipeline 会在 review 阶段开始时把
> `CODEX_MODEL`（以及 `CODEX_GATEWAY_URL`，如提供）写入 `/tmp/codex_home.XXXXXX/config.toml`，
> 通过 `CODEX_HOME=<tmp>` 重定向让 codex 读取，隔离用户 `~/.codex/config.toml`，保证可复现。
> 设置 `CODEX_GATEWAY_URL` 后，框架自动生成 `model_provider = "gateway"` + `[model_providers."gateway"]` 段，
> 将 Codex 重定向到自定义端点（如 MaaS / cc_switch 转发）而非直连 `api.openai.com`。
> MCP server 通过 `env_vars` 字段从父进程继承 `REVIEW_RESULTS_DIR` / `REVIEW_INSTANCE_ID`。

> **注意**：模型需兼容 Codex 的工具调用——不支持 MCP 工具调用的模型将无法上报 finding。

> Judge 未配 `JUDGE_API_KEY` 或 `JUDGE_USE_MOCK=true` 时，语义匹配自动退化为本地 Mock
> 相似度（零成本，仅用于验证流程；真实评测请配置真实裁判模型）。

### 1. 把 benchmark 转换为标准格式

> 以下命令均在 `eval_all/` 目录内执行（已激活 venv）。

把原始数据放到约定位置 `benchmark/<benchmark名>/` 后，对应转换器默认即可读取，
产出 `data/<benchmark_key>.jsonl`。当前已内置 AACR-Bench 的转换器：

```bash
# AACR-Bench（原始数据 benchmark/AACR-Bench/ -> data/aacr_bench.jsonl）
python -m converters.aacr_bench --validate

# --validate：转换后用 schema 重新加载校验，确保产物合法
# --limit N：先把全部样本乱序，再取前 N 条（随机抽样，不传则全量）
# --seed N：乱序随机种子，指定后随机可复现（用于固定评测子集；默认每次随机）
# 也可显式指定输入 / 输出：
python -m converters.aacr_bench \
    --input benchmark/AACR-Bench/positive_samples.json \
    --output data/aacr_bench.jsonl \
    --limit 30 --seed 42 --validate
```

> **抽样随机性**：转换器会先打乱原始数据顺序再应用 `--limit`，因此 `--limit` 得到的是
> **随机子集**而非固定的前 N 条。需要可复现的固定子集时传入 `--seed`。

**转换器参数**（`python -m converters.<key> [options]`）：

| 参数 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `--input` | 约定路径 | 原始数据文件路径（默认 `benchmark/<benchmark名>/<原始文件>`） |
| `--output` | 约定路径 | 输出标准格式 JSONL 路径（默认 `data/<key>.jsonl`） |
| `--limit` | 全部 | 先整体乱序再取前 N 条（随机抽样，不传则全量） |
| `--seed` | 每次随机 | 乱序随机种子；指定后随机可复现（用于固定评测子集） |
| `--validate` | 关 | 转换后用 schema 重新加载校验，确保产物合法 |

### 2. 一键运行（评审 + 评测）

```bash
set -a && source .env && set +a

# OCR 评审器，跑前 5 条
python -m pipeline run --stage all --reviewer ocr --dataset data/aacr_bench.jsonl --limit 5 --concurrency 1 --run-id baseline

# Claude 评审器，跑前 2 条，评审完直接评测
python -m pipeline run --stage all --reviewer claude --dataset data/aacr_bench.jsonl --limit 2 --concurrency 1 --run-id baseline

# Codex 评审器，跑前 2 条
python -m pipeline run --stage all --reviewer codex --dataset data/aacr_bench.jsonl --limit 2 --concurrency 1 --run-id baseline
```

---

## CLI 参考

> 在 `eval_all/` 目录内执行（已激活 venv）。

```
python -m pipeline run --reviewer {ocr|claude|codex} --dataset <标准JSONL> [options]
```

| 参数 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `--reviewer` | （必填） | 评审器：`ocr` / `claude` / `codex` |
| `--dataset` | （必填） | 标准格式数据集路径，如 `data/aacr_bench.jsonl` |
| `--stage` | `all` | `all`（评审+评测）/ `review`（仅评审）/ `eval`（仅评测） |
| `--repo-dir` | `repo/` | 仓库 clone 目录（缓存复用） |
| `--results-dir` | `results/<benchmark>/<reviewer>/<run_id>` | 直接指定结果目录（高级用法，绕过 run 机制）；缺省按 dataset 自动推导 |
| `--run-id` | 时间戳 | 本次 run 的目录名（如 `baseline`、`v2`）；review 不传则自动用时间戳命名；**eval 阶段必须显式指定** |
| `--limit` | 全部 | 只处理标准数据集的前 N 条（数据集的随机性已在转换阶段决定，见上方转换器 `--seed`） |
| `--line-k` | `1` | 评测行号匹配容差（重叠或距离 ≤ k 即命中） |
| `--timeout-minutes` | `30` | 单条样本评审超时 |
| `--eval-rounds` | `1` | 评测阶段执行轮数；多轮时各轮独立评测后取数值平均作为最终指标 |
| `--ocr-command` | `ocr` | OCR 评审器可执行文件路径；可指定本地 release 包路径（如 `./ocr-v2.0.0`），仅 ocr 评审器有效 |
| `--max-tools` | `30` | OCR 每个文件最大工具调用轮数（控制单文件评审深度），仅 ocr 评审器有效 |
| `--concurrency` | `1` | 评审阶段并发的 repo 组数（`1`=完全串行）；详见下方「并发评审」 |
| `--preview` | 关 | 只 clone/checkout，不真正调用评审 LLM |

### 并发评审（`--concurrency`）

每条样本评审时会对其所属仓库做 `checkout` / `clean`，**同一仓库的样本共享同一个本地
工作区**（`repo/<owner__name>`），并发会互相踩踏。因此并发模型按 `repo` 分组：

- **组间并发**：不同 repo 的样本可同时评审，最多 `--concurrency` 个 repo 组并行
- **组内串行**：同一 repo 的样本严格顺序执行，互不干扰
- `--concurrency 1`（默认）= 完全串行，与改造前行为一致

只作用于评审阶段（`review`）；评测阶段为纯本地计算 + LLM 判定，不受影响。

```bash
# 4 个 repo 组并行评审（同一 repo 的样本仍串行）
python -m pipeline run --stage review --reviewer ocr \
    --dataset data/aacr_bench.jsonl --concurrency 4
```

> 实际并发数会被自动夹到 `min(--concurrency, repo 组数)`。

### 分阶段运行 + 多次评测对比（run 机制）

每次 review 都会自动创建一个独立的 run 目录 `results/<benchmark>/<reviewer>/<run_id>/`，
评审结果与评测指标都落在该目录内，**多次评测互不覆盖**，便于横向对比。

```bash
# 只评审（自动创建时间戳 run 目录）
python -m pipeline run --stage review --reviewer claude --dataset data/aacr_bench.jsonl --limit 5

# 给本次 run 起个有意义的名字（run 目录就叫 baseline）
python -m pipeline run --stage review --reviewer claude --dataset data/aacr_bench.jsonl --run-id baseline

# 换不同参数再跑一次，互不覆盖
python -m pipeline run --stage review --reviewer claude --dataset data/aacr_bench.jsonl --run-id run2

# 只评测（必须指定 --run-id）
python -m pipeline run --stage eval --reviewer claude --dataset data/aacr_bench.jsonl --run-id baseline

# 多轮评测取平均（减少单次随机波动）
python -m pipeline run --stage eval --reviewer claude --dataset data/aacr_bench.jsonl --run-id baseline --eval-rounds 3
```

---

## 三个阶段的工作机制

### 数据加载（schema.py）

读取标准 JSONL → 逐行校验 → 产出 `ReviewInstance` 列表。`--limit N` 按**原始数据顺序**取前 N 条。

### 执行评审（reviewers/）

对每条样本：`clone/fetch` → `checkout head_commit` → 调用评审器 → 写 `<safe_id>.json`。
评审过程带**进度条**（已完成 / 总数 + 实时速率 + ETA），可用于预估总耗时；
多 repo 并发时进度条线程安全地汇总推进（并发机制见上方「并发评审」）。

**工作区清理（三个评审器统一）**：评审**前后**都执行 `git reset --hard && git clean -fdx`
清理工作区（评审前清理由 `checkout_commit` 内置保证，评审后由各评审器在落盘结果后调用
`clean_worktree`）。避免评审器在仓库里留下的临时文件 / 改动污染下一次 checkout。

- **OCR**：`ocr review --from base --to head --format json`，结果存 `review.comments[]`。
- **Claude**：官方 `/code-review <base>...<head>`，结构化输出靠两条腿：
  1. **MCP 增量上报**：Claude 每确认一条 finding 就调一次 `report` 工具，写入
     `<safe_id>.partial.json`，与最终文本格式解耦；
  2. **stdout 解析兜底**：兼容单 JSON envelope 与逐行 stream-json（模型即使被要求输出 json 仍可能返回 stream-json），从中取最终文本再解析 finding；无法解析时按空评审 / 原始文本处理。
     最终取两者中 finding **数量较多**的一方（平局优先 MCP），`review_output_source`
     标记为 `mcp_stream` 或 `stdout_fallback`。
- **Codex**：`codex exec "/review <base>...<head>"`，与 Claude 完全同构的双通道策略：
  1. **MCP 增量上报**（主通道）：codex 专用 MCP server（`mcp_codex_finding_server.py`）
     暴露 `report(file, summary, description, start_line, end_line, severity)` 工具，
     finding 字段 `{file, start_line, end_line, severity, summary, description}`
     （与 Claude 版 schema 不同）。
  2. **JSONL 兜底**（辅通道）：`--json` 输出逐行 JSONL 事件流，解析
     `item.completed` 的 `agent_message` 文本兜底，并从 `turn.completed.usage` 取 token 统计。
     最终取两者中 finding **数量较多**的一方（平局优先 MCP），`review_output_source`
     标记为 `mcp_stream` 或 `jsonl_fallback`。模型三件套与 MCP 配置全部写进临时
     `$CODEX_HOME/config.toml`（方案 D），通过 `CODEX_HOME=<tmp>` 重定向加载。

### 执行评测（evaluate.py + judge.py）

按 `instance_id` **精确定位**结果文件（`<safe_id>.json`），由本地 `judge.py` 做
四阶段匹配（path → side → line(k) → semantic）：

- 找不到结果文件的样本计入 `missing_instances`，**不计入指标分母**（避免把没跑的样本算成 0 召回）。
- 同时输出**语义**与**行号**两套 Precision / Recall / F1。

`note`（语义比对文本）映射：参考用 `text`，OCR 用 `content`，Claude 用
`summary + failure_scenario`，Codex 用 `summary + description`（语义扩展为
failure scenario + 建议修复）。Codex 的 `severity` 字段仅存档，不参与评测。

---

## 接入新 benchmark（四步）

**只需写一个转换器，框架其余部分零改动。** 约定 `benchmark_key` 用小写下划线（如 `aacr_bench`），
它同时决定输出文件名 `data/<key>.jsonl`、结果目录 `results/<key>/`、指标目录 `metrics/<key>/`。

下面以已内置的 `converters/aacr_bench.py` 为实际范例：

1. **放原始数据**：放到约定目录 `benchmark/<benchmark名>/`（如 `benchmark/AACR-Bench/`）。
   大文件建议用 Git LFS 跟踪（见「准备环境」）。
2. **写转换器**：复制 `converters/aacr_bench.py` 改写，
   核心是 `convert_record()` 把原始记录逐条转成 `ReviewInstance`。建议复用路径约定：
   - 默认输入 `config.benchmark_raw_dir("<benchmark名>") / <原始文件名>`
   - 默认输出 `config.dataset_path("<key>")`（即 `data/<key>.jsonl`）

   运行转换：`python -m converters.<key> --validate`

3. **跑流水线**：
   `python -m pipeline run --stage all --reviewer <ocr|claude|codex> --dataset data/<key>.jsonl`
4. **看指标**：结果在 `metrics/<key>/<reviewer>/<run_id>/metrics_<reviewer>_<时间戳>.json`，控制台也会打印汇总。

> **内置转换器参考**：AACR-Bench 读**整个 JSON 数组**、需从 `githubPrUrl` 正则解析出 `owner/name`，
> `instance_id` 按 `owner__name@<head前7位>` 生成。新 benchmark 按自身格式照搬即可。

---

## 输出文件说明

> 路径中的 `<benchmark>` 取自 `--dataset` 文件名（如 `data/aacr_bench.jsonl` → `aacr_bench`）；
> `<run_id>` 为本次 run 的目录名，缺省为时间戳，`--run-id` 指定时直接使用该名字。
> 评审结果在 `results/` 下，评测指标在 `metrics/` 下，两者结构镜像对齐（同 run_id），
> 产物分开存放但关联清晰。

| 文件 | 内容 |
| ---- | ---- |
| `results/<benchmark>/<reviewer>/<run_id>/<safe_id>.json` | 单条样本的评审结果 |
| `results/<benchmark>/<reviewer>/<run_id>/summary_<reviewer>.json` | 评审阶段每条样本的状态摘要 |
| `metrics/<benchmark>/<reviewer>/<run_id>/metrics_<reviewer>_<时间戳>.json` | 评测指标（含 summary 与逐条匹配标记、`missing_instance_ids`） |

---

## FAQ

**Q：很多样本被整条跳过（missing_instances 很大）？**
说明这些样本在结果目录里没有对应的 `<safe_id>.json`，即评审还没跑到它们。这与「模型没生成意见」
不同——后者会有结果文件但 `review_output` 为空，仍会进入评测并拉低召回。

**Q：评测时语义指标全是 0？**
多半是 Judge 走了 Mock 模式（措辞差异大时本地相似度难命中）。配置 `eval_all/.env` 的
`JUDGE_API_KEY` 并 `JUDGE_USE_MOCK=false` 后重跑即可走真实裁判模型（评测阶段会自动读取该文件）。

**Q：仓库 clone 在哪？会重复 clone 吗？**
clone 到 `eval_all/repo/<owner__name>`，同一仓库缓存复用、只 fetch 不重复 clone。
