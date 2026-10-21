from __future__ import annotations

import re


def classify(question: str) -> str:
    value = question.lower()
    if re.search(r"\b(?:mdf|cal)-\d{4}\b", value) or any(term in value for term in ("mdf", "mf4", "dat", "a2l", "hex", "experiment", "commit", "数据库", "数据集", "上传", "文件", "信号", "标定", "版本", "日期", "哈希", "大小")):
        return "data"
    if any(term in value for term in ("wiki", "知识库", "发布", "草稿", "资料")):
        return "wiki"
    if any(term in value for term in ("运行", "agent", "状态", "checkpoint")):
        return "operations"
    return "knowledge"
