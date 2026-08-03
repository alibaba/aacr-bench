"""统一评测框架的路径与环境变量配置集中管理。

把所有"约定"集中到一处：默认目录、各阶段所需环境变量名、命名规则等，
避免散落到各模块产生隐式耦合。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 目录布局（相对本文件解析，保证脚本位置稳定）
EVALUATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVALUATION_DIR.parent

# 默认数据 / 产物目录（全部落在 evaluation/ 内，保持目录自洽）
# 原始数据约定：每个 benchmark 的原始文件放在 benchmark/<benchmark名>/ 下，
# 转换器读取后产出标准格式到 data/<benchmark名>.jsonl（按 benchmark 命名，避免覆盖）。
BENCHMARK_DIR = EVALUATION_DIR / "benchmark"
DATA_DIR = EVALUATION_DIR / "data"
DEFAULT_REPO_DIR = EVALUATION_DIR / "repo"
DEFAULT_RESULTS_DIR = EVALUATION_DIR / "results"
DEFAULT_METRICS_DIR = EVALUATION_DIR / "metrics"


def benchmark_raw_dir(benchmark_name: str) -> Path:
    """某 benchmark 的原始数据目录：benchmark/<benchmark名>/。"""
    return BENCHMARK_DIR / benchmark_name


def dataset_path(benchmark_key: str) -> Path:
    """某 benchmark 转换后的标准数据集路径：data/<benchmark_key>.jsonl。

    benchmark_key 用小写下划线形式（如 aacr_bench），与转换器模块名对应。
    """
    return DATA_DIR / f"{benchmark_key}.jsonl"


def benchmark_name_from_dataset(dataset_path_value: Path) -> str:
    """从数据集路径推导 benchmark 名（取文件名去扩展名）。

    例：data/aacr_bench.jsonl -> aacr_bench，用作 results / metrics 的分类子目录。
    """
    return Path(dataset_path_value).stem


def reviewer_results_dir(benchmark_name: str, reviewer: str) -> Path:
    """某 benchmark + reviewer 的结果根目录：results/<benchmark>/<reviewer>/。

    其下每次评审会创建一个独立的 run 子目录（见 make_run_id / run_dir），
    避免多次评测互相覆盖。
    """
    return DEFAULT_RESULTS_DIR / benchmark_name / reviewer


def make_run_id(run_id: Optional[str] = None) -> str:
    """确定本次 run 的目录名。

    传入 run_id 时直接使用（清洗非法字符）；不传则自动生成时间戳 YYYYmmdd_HHMMSS。
    用户可随意命名（如 baseline、v2），后续 eval 用同名即可找到。
    """
    if run_id:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id.strip())
        return cleaned.strip("-") or "run"
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_dir(benchmark_name: str, reviewer: str, run_id: str) -> Path:
    """某次 run 的目录：results/<benchmark>/<reviewer>/<run_id>/。

    评审结果（<safe_id>.json）与评测指标（metrics_<reviewer>_<ts>.json）都落在这里，
    使同一次 run 的产物一一对应。
    """
    return reviewer_results_dir(benchmark_name, reviewer) / run_id


def list_run_dirs(benchmark_name: str, reviewer: str) -> List[Path]:
    """列出某 benchmark + reviewer 下的所有 run 目录，按名称（即时间）升序。"""
    base = reviewer_results_dir(benchmark_name, reviewer)
    if not base.is_dir():
        return []
    return sorted((path for path in base.iterdir() if path.is_dir()), key=lambda p: p.name)


def latest_run_dir(benchmark_name: str, reviewer: str) -> Optional[Path]:
    """返回最新的一个 run 目录（按名称排序取最大）；无则返回 None。"""
    runs = list_run_dirs(benchmark_name, reviewer)
    return runs[-1] if runs else None


def metrics_run_dir(benchmark_name: str, reviewer: str, run_id: str) -> Path:
    """评测指标目录：metrics/<benchmark>/<reviewer>/<run_id>/。

    与 results 目录结构镜像对齐（同 run_id），产物分开但关联清晰。
    """
    return DEFAULT_METRICS_DIR / benchmark_name / reviewer / run_id

# 评审结果文件命名规则：<safe_id>.json，safe_id = instance_id.replace("/", "__")
RESULT_FILE_SUFFIX = ".json"
PARTIAL_FILE_SUFFIX = ".partial.json"

# OCR 评审器所需环境变量
OCR_URL_VAR = "OCR_LLM_URL"
OCR_TOKEN_VAR = "OCR_LLM_TOKEN"
OCR_MODEL_VAR = "OCR_LLM_MODEL"
OCR_USE_ANTHROPIC_VAR = "OCR_USE_ANTHROPIC"
OCR_REQUIRED_ENV_VARS: List[str] = [OCR_URL_VAR, OCR_TOKEN_VAR, OCR_MODEL_VAR]

# Claude 评审器所需环境变量
CLAUDE_URL_VAR = "ANTHROPIC_BASE_URL"
CLAUDE_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
CLAUDE_MODEL_VAR = "ANTHROPIC_MODEL"
CLAUDE_REQUIRED_ENV_VARS: List[str] = [CLAUDE_URL_VAR, CLAUDE_TOKEN_VAR, CLAUDE_MODEL_VAR]

# Claude Code 内置 API retry 配置
CLAUDE_MAX_RETRIES_VAR = "CLAUDE_CODE_MAX_RETRIES"
CLAUDE_MAX_RETRIES_DEFAULT = 10

# StopFailure hook 配置
HOOKS_DIR = EVALUATION_DIR / "hooks"
STOP_FAILURE_HOOK_SCRIPT = HOOKS_DIR / "on_stop_failure.py"
RETRY_EXHAUSTED_LOG = "retry_exhausted.jsonl"

# Claude 评审用的 MCP findings server
MCP_SERVER_SCRIPT = EVALUATION_DIR / "mcp_finding_server.py"
MCP_SERVER_NAME = "findings"
MCP_RESULTS_DIR_ENV_VAR = "REVIEW_RESULTS_DIR"
MCP_INSTANCE_ID_ENV_VAR = "REVIEW_INSTANCE_ID"
MCP_REPORT_TOOL = f"mcp__{MCP_SERVER_NAME}__report"

# Codex 评审器所需环境变量
# codex 非交互模式文档明确："CODEX_API_KEY 仅在 codex exec 中受支持"，
# 本框架全程用 codex exec，因此直接使用 CODEX_API_KEY；不要用 OPENAI_API_KEY。
CODEX_API_KEY_VAR = "CODEX_API_KEY"
CODEX_REQUIRED_ENV_VARS: List[str] = [CODEX_API_KEY_VAR]

# 模型名（可选）：写入临时 config.toml 顶级 model = "..." 行
# 不填则省略该行，用 codex 默认 provider 的默认模型
CODEX_MODEL_VAR = "CODEX_MODEL"

# 自建网关 / 代理 base_url（可选）
# 关键：codex 不存在 CODEX_BASE_URL 鉴权变量（已实测确认），
# 自建网关必须走 model_providers 表 + 顶级 model_provider
CODEX_GATEWAY_URL_VAR = "CODEX_GATEWAY_URL"

# provider id 同时作为顶级 model_provider 取值与 [model_providers."<id>"] 表名；
# 含特殊字符时 TOML 表名须加双引号（已在 write_codex_config 里处理）
CODEX_PROVIDER_ID = "gateway"

# Codex 评审用的 MCP findings server（codex 专用，schema 与 Claude 版不同）
# finding 字段：{file, start_line, end_line, severity, summary, description}
CODEX_MCP_SERVER_SCRIPT = EVALUATION_DIR / "mcp_codex_finding_server.py"
CODEX_MCP_SERVER_NAME = "findings"  # server 命名空间与 Claude 版同名无冲突（不同时跑）

# 评测（LLM as a Judge）专用配置，与 OCR/Claude 解耦；
# 在 import judge 之前会被映射成 judge 模块读取的变量名。
JUDGE_BASE_URL_VAR = "JUDGE_BASE_URL"
JUDGE_API_KEY_VAR = "JUDGE_API_KEY"
JUDGE_MODEL_VAR = "JUDGE_MODEL"
JUDGE_USE_MOCK_VAR = "JUDGE_USE_MOCK"


def safe_id(instance_id: str) -> str:
    """instance_id -> 结果文件名安全 id（把 / 替换为 __）。"""
    return instance_id.replace("/", "__")


def result_path(results_dir: Path, instance_id: str) -> Path:
    return Path(results_dir) / f"{safe_id(instance_id)}{RESULT_FILE_SUFFIX}"


def partial_path(results_dir: Path, instance_id: str) -> Path:
    return Path(results_dir) / f"{safe_id(instance_id)}{PARTIAL_FILE_SUFFIX}"


def retry_exhausted_path(results_dir: Path) -> Path:
    """重试耗尽的失败案例日志路径：<results_dir>/retry_exhausted.jsonl。"""
    return Path(results_dir) / RETRY_EXHAUSTED_LOG


def missing_env_vars(required: List[str]) -> List[str]:
    """返回缺失的环境变量名列表（用于早报错）。"""
    return [name for name in required if not os.environ.get(name)]
