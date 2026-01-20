[//]: # (# AACR-Bench: 多语言、仓库级上下文感知的自动化代码评审评测数据集)
![AACR-Bench Overview](imgs/bg.png)

<div align="center">

![](https://img.shields.io/badge/License-Apache_2.0-blue.svg)
![](https://img.shields.io/badge/Dataset-v1.0-green.svg)
![](https://img.shields.io/badge/Languages-10-orange.svg)
![](https://img.shields.io/badge/PRs-200-red.svg)

[English](README.md) | 简体中文

</div>

## 📋 简介
AACR-Bench 是一个**多语言、仓库级上下文感知的代码评审评测数据集**，可用于评估大语言模型在自动代码评审任务中的表现。数据集包含来自50个活跃开源项目的200个真实Pull Request，覆盖10种主流编程语言，每个实例不仅包含代码变更，还保留了完整的仓库上下文，真实还原了代码评审的全过程。通过人类与LLM协同评审 + 人类专家多轮标注，确保了数据的高质量和全面性。

### 主要用途
+ 评测代码审查模型的问题检测能力
+ 评估评审建议的质量和可行性
+ 跨语言、跨项目的模型泛化能力测试
+ 上下文感知能力的细粒度分析

## ✨ 核心特性
### 🌐 **多语言覆盖**
+ 涵盖10种主流编程语言：Python, Java, JavaScript, TypeScript, Go, Rust, C++, C#, Ruby, PHP

### 📁 **仓库级上下文**
+ 保留完整项目结构和依赖信息
+ 支持跨文件引用分析

### 🤖 人类专家 + **LLM增强标注**
+ 人类专家初始评审 + LLM系统性补充
+ 识别细微问题和潜在改进点
+ 人类专家多轮标注审核确保评论质量

### 📊 **全面评测指标**
+ 准确率评估（Precision）：评论质量与行级定位精度
+ 召回率分析（Recall）：问题发现的完整性
+ 噪声率控制（Noise Rate）：无效评论识别
+ 多维度细粒度分析：支持按语言、问题类型统计

## 🚀 快速开始（TBD）
### 安装依赖
```bash

```

### 下载数据集
```bash

```

### 运行评测
```python

```



## <font style="color:rgba(0, 0, 0, 0.88);">📈</font><font style="color:rgba(0, 0, 0, 0.88);"> </font>数据概览
### 数据统计
| 指标 | 数量 |
| --- | --- |
| Pull Requests | 200 |
| 编程语言 | 10 |
| 源项目 | 50 |
| 总评审意见 | 2145 |


### 数据格式
```json
{
  "type": "array",
  "item": {
    "change_line_count": {"type": "integer", "description": "修改的行数量"},
    "project_main_language":  {"type": "string", "description": "项目主语言"},
    "source_commit":  {"type": "string", "description": "源commit"},
    "target_commit":  {"type": "string", "description": "目标commit"},
    "githubPrUrl":  {"type": "string", "description": "github 的网址"},
    "comments": {
      "type": "array",
      "description": "标注的评论信息",
      "items": {
        "type": "object",
        "properties": {
          "is_ai_comment": {"type": "boolean", "description": "是否是AI评论"},
          "note": {"type": "string", "description": "纯英文评论，提取原有的 note_pure_en 字段"},
          "path": {"type": "string", "description": "文件路径"},
          "side": {"type": "string", "description": "评论挂载位置"},
          "source_model": {"type": "string", "description": "来源模型"},
          "from_line": {"type": "integer", "description": "开始行号，提取原有的 final_annotations 字段下的 from_line 字段"},
          "to_line": {"type": "integer", "description": "结束行号，提取原有的 final_annotations 字段下的 to_line 字段"},
          "category": {"type": "string", "description": "分类，提取原有的 final_annotations 字段下的 category 字段"},
          "context": {"type": "string", "description": "评论的作用域，提取原有的 final_annotations 字段下的 context 字段"}
        }
      }
    }
  }
}
```

## 📏 评测指标
我们采用多维度指标体系全面评估代码审查模型的性能，完整的指标定义、计算方法和分语言统计请查看 [docs/METRICS.md](docs/METRICS.md)。

### 核心指标
| 指标 | 说明 | 计算公式 |
| --- | --- | --- |
| **准确率** (Precision) | 模型生成的有效评论占比 | `有效匹配数 / 生成总数` |
| **召回率** (Recall) | 发现标注集中问题的能力 | `有效匹配数 / 数据集中有效数` |
| **行级准确率** (Line Precision) | 精确定位到代码行的能力 | `行号匹配数 / 生成总数` |
| **噪声率** (Noise Rate) | 无效或错误评论的比例 | `未匹配数 / 生成总数` |


### 基准测试结果（TBD）
| 模型 | 准确率 | 召回率 | 行级准确率 | 噪声率 |
| --- | --- | --- | --- | --- |
| Qwen |  |  |  |  |
| Claude-4 |  |  |  |  |
| Deepseek |  |  |  |  |


## 🤝 贡献指南
我们欢迎社区贡献！如果您想为 AACR-Bench 做出贡献，请按照以下步骤操作：

1. **Fork** 本仓库
2. **创建** 特性分支 (`git checkout -b feat/<font style="color:rgb(20, 20, 20);background-color:rgb(243, 244, 246);">add-new-prs</font>`)
3. **提交** 更改 (`git commit -m '<font style="color:rgb(20, 20, 20);background-color:rgb(243, 244, 246);">feat: add new PRs for rust</font>'`)
4. **推送** 到分支 (`git push origin feat/<font style="color:rgb(20, 20, 20);background-color:rgb(243, 244, 246);">add-new-prs</font>`)
5. **创建** Pull Request

详细贡献指南请参考 [CONTRIBUTING.md](CONTRIBUTING.md)

## 👥 作者与维护者
| 姓名 | GitHub                                             | 负责领域  | 主要职责                 |
| :--- |:---------------------------------------------------|:------|:---------------------|
| **李峥峰** | [@lizhengfeng](https://github.com/lizhengfeng101)  | 项目负责人 | 总体架构设计、技术方向把控        |
| **王博格** | [@wangboge](https://github.com/wbgbg)          | 技术顾问  | 技术方案评审、技术指导          |
| **张耒** | [@zhanglei](https://github.com/TongWu-ZL)          | 数据架构  | 评测框架架构、指标体系设计、性能优化   |
| **于永达** | [@yuyongda](https://github.com/inkeast)            | 评测系统  | 数据模式设计、评测协议制定、质量控制标准 |
| **郭欣欣** | [@guoxinxin](https://github.com/guoxinxin125)      | 标注平台  | 标注系统开发、工作流设计、质量保证机制  |
| **余明晖** | [@yuminghui](https://github.com/yuminghui)         | AI增强  | LLM标注流程、模型选择、提示工程优化  |
| **庄正奇** | [@zhuangzhengqi](https://github.com/ZhengqiZhuang) | 工程化   | CI/CD流程、自动化测试、部署脚本   |


## 📄 许可
本项目采用 Apache License 2.0 许可证，<font style="color:rgb(31, 35, 40);">有关详细信息，请参阅</font> [LICENSE](LICENSE) <font style="color:rgb(31, 35, 40);">文件。</font>

## 📚 引用（TBD）
如果您在研究中使用了AACR-Bench，请引用我们的论文：

```plain
@article{liu2026aacrbench,
  title={AACR-Bench: A Multi-lingual Repository-level Context-aware Automated Code Review Benchmark},
  author={Li, Zhengfeng and Zhang, Lei and Yu, Yongda and Guo, Xinxin and Yu, Minghui and Zhuang, Zhengqi},
  journal={arXiv preprint arXiv:2026.xxxxx},
  year={2026},
  url={https://arxiv.org/abs/2026.xxxxx}
}
```



## 🗺️ 路线图
- [x] v1.0 (2026.01): 初始发布 - 200个PR，10种语言

## <font style="color:rgb(31, 35, 40);">🌟</font><font style="color:rgb(31, 35, 40);"> </font>致谢
+ 感谢所有参与数据标注的贡献者，特别是完成15条以上有效标注的核心贡献者，完整名单见 [docs/ANNOTATORS.md](docs/ANNOTATORS.md)。
+ 感谢提供原始PR数据的开源项目维护者。