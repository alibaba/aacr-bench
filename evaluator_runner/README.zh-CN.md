# Evaluator Runner

代码评审评论评测框架，用于评估 AI 生成的代码评审意见与人工标注参考评论的匹配程度。

## 目录结构

```
evaluator_runner/
├── __init__.py              # 模块导出
│── example_test.py          # 示例用法
│── README.zh-CN.md          # README 中文版
├── README.md                # README
├── core/
│   ├── evaluator.py         # 核心评估逻辑
│   ├── match_location.py    # 位置匹配逻辑
│   ├── match_base.py        # 语义匹配基类
│   ├── match_llm.py         # LLM 语义匹配实现
│   ├── match_embedding.py   # Embedding 语义匹配实现
│   └── matcher_factory.py   # 匹配器工厂
└── utils/
    ├── config.py            # 配置类和枚举定义
    └── .env                 # 环境变量配置
```

## 快速开始

### 安装依赖

```bash
pip install openai python-dotenv
```

### 配置环境变量

在 `utils/` 目录下创建 `.env` 文件，或从 `.env_sample` 复制：

```env
LLM_MODEL_URL="your_llm_model_url"
LLM_MODEL="your_llm_model"
LLM_API_KEY="your_llm_api_key"

EMBEDDING_MODEL_URL="your_embedding_model_url"
EMBEDDING_MODEL="your_embedding_model"
EMBEDDING_API_KEY="your_embedding_api_key"
```

### 基础用法

```python
import asyncio
from evaluator_runner import (
    get_evaluator_ans_from_json,
    load_generated_comments_from_file,
    EvaluatorConfig
)

async def main():
    # 加载待评测评论
    generated_comments = load_generated_comments_from_file("path/to/comments.txt")
    
    # 参考评论（从 positive_samples.json 加载）
    reference_comments = [...]
    
    # 使用默认配置运行评测
    result = await get_evaluator_ans_from_json(
        github_pr_url="https://github.com/owner/repo/pull/123",
        generated_comments=generated_comments,
        good_comments=reference_comments
    )
    
    print(f"位置匹配率: {result['positive_line_match_rate']}")
    print(f"语义匹配率: {result['positive_match_rate']}")

asyncio.run(main())
```

## 配置说明

### EvaluatorConfig

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `line_distance_threshold` | `int` | `1` | 行号匹配距离阈值，0 表示必须完全重叠 |
| `semantic_matcher_type` | `SemanticMatcherType` | `LLM` | 语义匹配器类型：`LLM` 或 `EMBEDDING` |
| `enable_semantic_match` | `bool` | `True` | 是否启用语义匹配 |
| `filter_config` | `FilterConfig` | `None` | 数据筛选配置 |

### 配置快捷方法

```python
from evaluator_runner import EvaluatorConfig

# 使用 Embedding 匹配器
config = EvaluatorConfig.with_embedding(line_distance_threshold=2)

# 仅位置匹配（禁用语义匹配）
config = EvaluatorConfig.location_only(line_distance_threshold=1)

# 带筛选条件的配置
config = EvaluatorConfig.with_filter(
    pr_categories=["Bug Fix"],
    project_languages=["Python"],
    comment_categories=["Code Defect"]
)
```

### FilterConfig 筛选配置

| 参数 | 类型 | 说明 |
|------|------|------|
| `pr_categories` | `List[str]` | PR 类别筛选 |
| `project_languages` | `List[str]` | 项目语言筛选 |
| `comment_categories` | `List[str]` | 评论类别筛选 |
| `comment_contexts` | `List[str]` | 评论上下文级别筛选 |

## 输入数据格式

### 待评测评论文件 (.txt)

```
<path>src/main.py</path>
<side>right</side>
<from>10</from>
<to>15</to>
<note>这里存在潜在的空指针问题</note>
<notesplit />
<path>src/utils.py</path>
<side>right</side>
<from>20</from>
<to>25</to>
<note>建议添加异常处理</note>
<notesplit />
```

### 参考评论格式 (positive_samples.json)

```json
{
    "category": "Bug Fix",
    "project_main_language": "Python",
    "githubPrUrl": "https://github.com/owner/repo/pull/123",
    "comments": [
        {
            "id": "comment_1",
            "note": "评论内容",
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

## 输出结果格式

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

## 匹配流程

```
生成评论 ──┬── 位置匹配 ──┬── 语义匹配 ── ✓ 完全匹配
           │              │
           ✗ 不匹配       ✗ 仅位置匹配
```

**位置匹配规则**：
1. 文件路径必须相同
2. side 字段必须相同
3. 行号范围重叠或距离在阈值内

**语义匹配**：使用 LLM 或 Embedding 判断两条评论是否表达相同含义

## 枚举类型

### PRCategory（PR 类别）
- `Bug Fix`
- `Code Refactoring / Architectural Improvement`
- `New Feature Additions`
- `Performance Optimizations`
- `Security Patches / Vulnerability Fixes`
- `Documentation Update`
- `Code Style, Linting, Formatting Fixes`
- `Test Suite / CI Enhancements`
- `Dependency Updates & Environment Compatibility`

### ProjectLanguage（项目语言）
- `Python`, `Java`, `JavaScript`, `TypeScript`, `Go`, `Rust`, `C`, `C++`, `C#`, `PHP`

### CommentCategory（评论类别）
- `Code Defect`
- `Maintainability and Readability`
- `Performance`
- `Security Vulnerability`

### CommentContext（评论上下文）
- `Diff Level`
- `File Level`
- `Repo Level`

## 批量评测示例

使用 `example_test.py` 进行自定义目录的批量评测：

### 配置说明

编辑 `example_test.py` 顶部的配置区：

```python
# 输入/输出设置
INPUT_DIR = "./test_comments"           # 待评测评论文件目录
OUTPUT_FILE = "./evaluation_results.json"  # 输出文件路径
FILE_PATTERN = "*.txt"                   # 文件匹配模式
REFERENCE_DATA_FILE = "./positive_samples.json"  # 参考数据文件

# 评测设置
LINE_DISTANCE_THRESHOLD = 1              # 行号匹配阈值
ENABLE_SEMANTIC_MATCH = True             # 是否启用语义匹配
SEMANTIC_MATCHER_TYPE = "llm"            # "llm" 或 "embedding"

# 筛选设置（可选，设为 None 禁用筛选）
PR_CATEGORIES = None                     # 如：["Bug Fix"]
PROJECT_LANGUAGES = None                 # 如：["Python", "Java"]
COMMENT_CATEGORIES = None                # 如：["Code Defect"]
COMMENT_CONTEXTS = None                  # 如：["Diff Level"]
```

### 运行评测

```bash
python evaluator_runner/example_test.py
```

### 文件命名规范

评论文件应遵循以下命名格式：
```
comments_{仓库名}_{PR编号}.txt
```

示例：`comments_cherry-studio_5540.txt`

