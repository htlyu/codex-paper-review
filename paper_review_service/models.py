from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Issue(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: Literal["claim", "method", "experiment", "statistics", "literature", "presentation"]
    severity: Literal["critical", "major", "minor", "suggestion"]
    status: Literal["confirmed", "unresolved"]
    confidence: Literal["high", "medium", "low"]
    page: int = Field(ge=1)
    evidence_kind: Literal["text", "visual"]
    evidence: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    countercheck: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    source_urls: list[str]


class Coverage(StrictModel):
    area: str = Field(min_length=1)
    status: Literal["checked", "partial", "not_checked", "not_applicable"]
    details: str = Field(min_length=1)


class Source(StrictModel):
    title: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://")
    publication_date: str
    supports: str = Field(min_length=1)


class Report(StrictModel):
    summary: str = Field(min_length=1)
    strengths: list[str]
    issues: list[Issue]
    coverage: list[Coverage]
    reviewed_pages: list[int]
    visual_pages: list[int]
    unreviewed_pages: list[int]
    sources: list[Source]
    limitations: list[str]


def validate_report(report: Report, pages: list[dict]) -> list[str]:
    """Validate declared coverage and text quotations, never semantic correctness."""
    valid = set(range(1, len(pages) + 1))
    reviewed, skipped, visual = map(set, (report.reviewed_pages, report.unreviewed_pages, report.visual_pages))
    errors = []
    if reviewed & skipped or reviewed | skipped != valid:
        errors.append("Reviewed and unreviewed page lists must partition all PDF pages")
    if len(reviewed) != len(report.reviewed_pages) or len(skipped) != len(report.unreviewed_pages):
        errors.append("Page lists contain duplicates")
    if not visual <= reviewed or len(visual) != len(report.visual_pages):
        errors.append("Visual pages must be unique reviewed pages")
    ids = [issue.id for issue in report.issues]
    if len(set(ids)) != len(ids):
        errors.append("Duplicate issue IDs")
    source_urls = {source.url for source in report.sources}
    for issue in report.issues:
        if issue.page not in reviewed:
            errors.append(f"{issue.id}: evidence page is not marked reviewed")
        if not set(issue.source_urls) <= source_urls:
            errors.append(f"{issue.id}: external evidence missing from sources")
        if issue.page not in valid:
            errors.append(f"{issue.id}: page outside PDF")
        elif issue.evidence_kind == "text":
            needle = " ".join(issue.evidence.split())
            haystack = " ".join(pages[issue.page - 1]["text"].split())
            if not needle or needle not in haystack:
                errors.append(f"{issue.id}: quotation not present in extracted page text")
        elif issue.page not in visual:
            errors.append(f"{issue.id}: visual evidence page was not declared visually inspected")
    if not report.coverage:
        errors.append("Coverage cannot be empty")
    return errors


def render_markdown(report: Report, run: dict) -> str:
    lines = ["# 论文检查报告", "", report.summary, "",
             f"PDF SHA-256：`{run['input_sha256']}`", "",
             f"运行配置：`{run['model']}` / `{run['reasoning']}`；PDF 共 {run['page_count']} 页。",
             "页码均为 PDF 物理页码，从 1 开始。", "", "## 有证据支持的优点", ""]
    lines += [f"- {text}" for text in report.strengths] or ["未单独记录。"]
    lines += ["", "## 问题与建议", ""]
    if not report.issues:
        lines.append("本轮未检出可报告的问题；这不证明论文不存在错误。")
    for issue in report.issues:
        lines += [f"### {issue.id}：{issue.title}", "",
                  f"{issue.severity} · {issue.status} · {issue.confidence} · 第 {issue.page} 页", "",
                  f"**证据（{issue.evidence_kind}）**：{issue.evidence}", "",
                  f"**问题**：{issue.problem}", "", f"**影响**：{issue.impact}", "",
                  f"**反证复查**：{issue.countercheck}", "", f"**建议行动**：{issue.next_action}", ""]
        lines += [f"- [外部依据]({url})" for url in issue.source_urls]
    lines += ["", "## 检查覆盖", ""]
    lines += [f"- {item.area}（{item.status}）：{item.details}" for item in report.coverage]
    lines += [f"- 已检查页：{report.reviewed_pages}", f"- 已查看图像页：{report.visual_pages}",
              f"- 未检查页：{report.unreviewed_pages}", "", "## 外部资料", ""]
    lines += [f"- [{source.title}]({source.url})（{source.publication_date}）：{source.supports}"
              for source in report.sources]
    lines += ["", "## 限制", ""]
    lines += [f"- {item}" for item in report.limitations]
    lines += ["- 页码、文本引文和声明的覆盖范围已做机械校验；视觉判断、来源支持关系及技术结论仍属于模型审查结果。",
              "- 本服务按公开方法设计，未经与 Google PAT 的效果等价验证。", ""]
    return "\n".join(lines)
