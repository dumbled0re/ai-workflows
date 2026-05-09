"""Pydantic models for point_sites."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

ClickStatus = Literal[
    "success",
    "failed_4xx",
    "failed_5xx",
    "failed_redirect",
    "failed_timeout",
    "failed_connection",
    "pending",
]

ExtractionReason = Literal[
    "whitelist_url_pattern",
    "whitelist_url_pattern_and_anchor",
]


class ClickCandidate(BaseModel):
    url: HttpUrl
    anchor_text: str
    estimated_points: int | None = None
    extraction_reason: ExtractionReason


class ClickResult(BaseModel):
    candidate: ClickCandidate
    final_status: ClickStatus
    http_status: int | None = None
    final_host: str | None = None
    duration_ms: int
    timestamp: datetime


class ParsedMessage(BaseModel):
    message_id: str
    subject: str
    sender: str
    html_body: str | None = None
    plaintext_body: str | None = None
    received_at: datetime | None = None

    @property
    def has_body(self) -> bool:
        return bool(self.plaintext_body or self.html_body)


class MessageRun(BaseModel):
    message_id: str
    subject_redacted: str
    candidates: list[ClickCandidate] = Field(default_factory=list)
    results: list[ClickResult] = Field(default_factory=list)
    attempt_count: int = 0


class RunSummary(BaseModel):
    started_at: datetime
    finished_at: datetime
    messages_processed: int
    candidates_total: int
    success_count: int
    failure_count: int
    parse_failures: list[str] = Field(default_factory=list)
    anomaly_messages: list[str] = Field(default_factory=list)


class UrlState(BaseModel):
    redacted_url: str
    status: ClickStatus
    last_status_at: datetime
    http_status: int | None = None


class MessageState(BaseModel):
    first_seen: datetime
    last_attempt: datetime
    attempt_count: int = 0
    urls: dict[str, UrlState] = Field(default_factory=dict)


class StateFile(BaseModel):
    version: int = 1
    messages: dict[str, MessageState] = Field(default_factory=dict)
