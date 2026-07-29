"""Pydantic models and in-memory job structures."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Format = Literal["epub", "pdf", "mobi", "html", "azw3"]
SearchType = Literal["author", "tag"]
# Doubles as a whitelist: only these values ever reach AO3 query params.
SortColumn = Literal["revised_at", "kudos_count", "hits", "bookmarks_count", "word_count"]


class Work(BaseModel):
    work_id: str
    title: str
    authors: list[str] = Field(default_factory=lambda: ["Anonymous"])
    word_count: int | None = None
    tags: list[str] = Field(default_factory=list)
    fandoms: list[str] = Field(default_factory=list)
    summary: str = ""
    series: str | None = None
    kudos: int | None = None
    hits: int | None = None
    bookmarks: int | None = None
    chapters: str | None = None  # e.g. "3/?" or "12/12"
    rating: str | None = None  # AO3 label, e.g. "Teen And Up Audiences"
    complete: bool | None = None  # derived from chapters; None = unknown


class SearchRequest(BaseModel):
    query: str
    search_type: SearchType
    max_results: int = 100
    sort_by: SortColumn = "revised_at"
    complete_only: bool = False
    words_from: int | None = Field(default=None, ge=0)
    words_to: int | None = Field(default=None, ge=0)
    exclude_tags: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    works: list[Work]
    message: str | None = None
    truncated: bool = False
    # work_id -> formats already in the library, for the results table.
    downloaded: dict[str, list[str]] = Field(default_factory=dict)


class EnqueueRequest(BaseModel):
    works: list[Work]
    format: Format = "epub"
    category: str


class ItemStatus(str, Enum):
    queued = "queued"
    downloading = "downloading"
    done = "done"
    skipped = "skipped"
    error = "error"


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str | None
    role: str  # 'user' | 'admin'
    provider: str  # 'local' | 'oidc'
    subject: str | None
    created_at: str
    last_login_at: str | None
    cover_style: str = "hybrid"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class Session:
    token_hash: str
    user_id: int
    csrf_token: str
    created_at: str
    expires_at: str
    last_used_at: str
    user_agent: str | None


@dataclass(frozen=True)
class OidcState:
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    next_path: str
    created_at: str


@dataclass(frozen=True)
class OidcConfig:
    enabled: bool
    client_id: str
    client_secret: str
    issuer: str
    scopes: str

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.client_id) and bool(self.issuer)


@dataclass(frozen=True)
class GrimmoryConfig:
    """How much Grimmory-specific metadata to embed in downloaded EPUBs."""

    enabled: bool
    genres: str  # 'tag_only' | 'fandoms' | 'extended'
    trim_subjects: bool
    map_rating: bool


@dataclass
class JobItem:
    work: Work
    status: ItemStatus = ItemStatus.queued
    message: str = ""
    filename: str = ""


@dataclass
class Job:
    job_id: str
    category: str
    format: str
    items: list[JobItem]
    cover_style: str = "hybrid"
    state: str = "queued"  # queued | running | finished
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in ItemStatus}
        for item in self.items:
            counts[item.status.value] += 1
        return counts

    def summary(self) -> dict:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "category": self.category,
            "format": self.format,
            "cover_style": self.cover_style,
            "total": len(self.items),
            "counts": self.counts(),
            "created_at": self.created_at.isoformat(),
        }

    def detail(self) -> dict:
        return {
            **self.summary(),
            "items": [
                {
                    "work_id": item.work.work_id,
                    "title": item.work.title,
                    "status": item.status.value,
                    "message": item.message,
                    "filename": item.filename,
                }
                for item in self.items
            ],
        }
