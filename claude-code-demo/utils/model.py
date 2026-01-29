from pydantic import BaseModel
from typing import Optional

class Comment(BaseModel):
    is_ai_comment: bool
    note: str
    path: str
    side: str
    source_model: str
    from_line: int
    to_line: int
    category: str
    context: str

class PRDataItem(BaseModel):
    change_line_count: int
    category: str
    project_main_language: str
    source_commit: str
    target_commit: str
    githubPrUrl: str
    comments: list[Comment]
    finish: Optional[bool] = False
