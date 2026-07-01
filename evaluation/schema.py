"""统一评测框架的标准数据格式（schema）与加载校验。

新 benchmark 接入流程：只要把原始数据转换成本文件定义的标准格式（JSONL，
每行一个 ReviewInstance），即可直接被 review / evaluate 阶段消费，无需改动框架。

为什么用 dataclass + 加载时校验：
- 字段与类型集中声明，避免各阶段对原始字段名硬编码、产生隐式耦合；
- 加载时即校验必填字段与行号合法性，问题数据能在最早阶段报错，而不是
  跑到评审/评测中途才暴露。

标准格式（每行一个 JSON 对象）：
    {
      "instance_id": "psf__requests-5711@9484e13",
      "repo": "psf/requests",
      "base_commit": "5351469...",
      "head_commit": "9484e13...",
      "clone_url": "https://github.com/psf/requests.git",   // 可选，缺省按 repo 推导
      "reference_comments": [
        {"path": "setup.py", "start_line": 46, "end_line": 46, "text": "...", "side": "left"}
      ]
    }

行号约定：reference_comments 的行号统一用闭区间 [start_line, end_line]。
单点评论令 start_line == end_line。允许为 null（表示无明确行号）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class SchemaError(ValueError):
    """标准数据格式校验失败时抛出，附带行号/instance 定位信息。"""


@dataclass
class ReferenceComment:
    """一条人工标注的参考评审评论（标准格式）。"""

    path: str
    text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    side: Optional[str] = None  # "left" 或 "right"，表示评论附着在 diff 的哪一侧

    @staticmethod
    def from_dict(data: Dict[str, Any], where: str) -> "ReferenceComment":
        if not isinstance(data, dict):
            raise SchemaError(f"{where}: reference_comment 必须是对象，实际为 {type(data).__name__}")

        path = data.get("path")
        text = data.get("text")
        if not isinstance(path, str) or not path.strip():
            raise SchemaError(f"{where}: reference_comment.path 必须是非空字符串")
        if not isinstance(text, str) or not text.strip():
            raise SchemaError(f"{where}: reference_comment.text 必须是非空字符串")

        start_line = _validate_optional_line(data.get("start_line"), f"{where}.start_line")
        end_line = _validate_optional_line(data.get("end_line"), f"{where}.end_line")
        # 行号统一为有序闭区间；任一缺失用另一个补齐
        start_line, end_line = _normalize_line_range(start_line, end_line)

        side = data.get("side")
        if side is not None:
            if not isinstance(side, str) or side.strip() not in ("left", "right"):
                raise SchemaError(f"{where}.side: 若提供则必须是 'left' 或 'right'，实际为 {side!r}")
            side = side.strip()

        return ReferenceComment(
            path=path.strip(),
            text=text.strip(),
            start_line=start_line,
            end_line=end_line,
            side=side,
        )

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
        }
        if self.side is not None:
            result["side"] = self.side
        return result


@dataclass
class ReviewInstance:
    """一个待评审 / 评测的样本（标准格式）。"""

    instance_id: str
    repo: str
    base_commit: str
    head_commit: str
    clone_url: Optional[str] = None
    reference_comments: List[ReferenceComment] = field(default_factory=list)

    @property
    def safe_id(self) -> str:
        """结果文件名所用的安全 id（与 eval/ 旧脚本规则一致）。"""
        return self.instance_id.replace("/", "__")

    @property
    def resolved_clone_url(self) -> str:
        """clone 用的 URL：显式提供则用之，否则按 repo 推导 GitHub 地址。"""
        if self.clone_url:
            return self.clone_url
        return f"https://github.com/{self.repo}.git"

    @staticmethod
    def from_dict(data: Dict[str, Any], where: str) -> "ReviewInstance":
        if not isinstance(data, dict):
            raise SchemaError(f"{where}: 每行必须是 JSON 对象，实际为 {type(data).__name__}")

        instance_id = data.get("instance_id")
        repo = data.get("repo")
        base_commit = data.get("base_commit")
        head_commit = data.get("head_commit")

        _require_non_empty_str(instance_id, f"{where}.instance_id")
        _require_non_empty_str(repo, f"{where}.repo")
        _require_non_empty_str(base_commit, f"{where}.base_commit")
        _require_non_empty_str(head_commit, f"{where}.head_commit")

        clone_url = data.get("clone_url")
        if clone_url is not None and (not isinstance(clone_url, str) or not clone_url.strip()):
            raise SchemaError(f"{where}.clone_url: 若提供则必须是非空字符串")

        raw_comments = data.get("reference_comments", []) or []
        if not isinstance(raw_comments, list):
            raise SchemaError(f"{where}.reference_comments: 必须是数组")

        reference_comments = [
            ReferenceComment.from_dict(item, f"{where}.reference_comments[{idx}]")
            for idx, item in enumerate(raw_comments)
        ]

        return ReviewInstance(
            instance_id=instance_id.strip(),
            repo=repo.strip(),
            base_commit=base_commit.strip(),
            head_commit=head_commit.strip(),
            clone_url=clone_url.strip() if isinstance(clone_url, str) else None,
            reference_comments=reference_comments,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "reference_comments": [comment.to_dict() for comment in self.reference_comments],
        }
        if self.clone_url:
            payload["clone_url"] = self.clone_url
        return payload


def _require_non_empty_str(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{where}: 必须是非空字符串")


def _validate_optional_line(value: Any, where: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{where}: 行号必须是整数或 null，实际为 {value!r}")
    if value <= 0:
        raise SchemaError(f"{where}: 行号必须为正整数，实际为 {value}")
    return value


def _normalize_line_range(
    start_line: Optional[int], end_line: Optional[int]
) -> tuple[Optional[int], Optional[int]]:
    if start_line is None and end_line is None:
        return None, None
    if start_line is None:
        start_line = end_line
    if end_line is None:
        end_line = start_line
    if start_line > end_line:
        start_line, end_line = end_line, start_line
    return start_line, end_line


def load_instances(dataset_path: Path, limit: Optional[int] = None) -> List[ReviewInstance]:
    """加载标准格式 JSONL 数据集，逐行校验并返回 ReviewInstance 列表。

    任何一行不合法都会抛出带行号定位的 SchemaError，确保问题数据尽早暴露。
    重复的 instance_id 也会被拒绝，避免结果文件相互覆盖。
    """
    dataset_path = Path(dataset_path)
    if not dataset_path.exists():
        raise SchemaError(f"数据集不存在: {dataset_path}")

    instances: List[ReviewInstance] = []
    seen_ids: set[str] = set()

    with dataset_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            where = f"{dataset_path.name}:L{line_number}"
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise SchemaError(f"{where}: JSON 解析失败: {error}") from error

            instance = ReviewInstance.from_dict(data, where)
            if instance.instance_id in seen_ids:
                raise SchemaError(f"{where}: 重复的 instance_id: {instance.instance_id}")
            seen_ids.add(instance.instance_id)
            instances.append(instance)

            if limit is not None and len(instances) >= limit:
                break

    return instances
