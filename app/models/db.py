"""Pydantic v2 models representing database rows, inputs, and domain structures."""

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class OAuthStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


class EmailCategory(StrEnum):
    WORK = "work"
    PERSONAL = "personal"
    FINANCE = "finance"
    PROMO = "promo"
    NEWSLETTER = "newsletter"
    SECURITY = "security"
    TRAVEL = "travel"
    OTHER = "other"


class EmailPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DraftStatus(StrEnum):
    DRAFT_GENERATED = "draft_generated"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    REJECTED = "rejected"
    REVISING = "revising"
    EXPIRED = "expired"


class DraftVersionSource(StrEnum):
    LLM = "llm"
    USER_EDIT = "user_edit"
    REVISION = "revision"


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"
    WEB = "web"
    EMAIL = "email"
    ALL = "all"


class TrustLevel(StrEnum):
    SUGGEST = "suggest"
    ACT_NOTIFY = "act_notify"
    AUTO = "auto"


# -----------------------------------------------------------------------------
# User Models
# -----------------------------------------------------------------------------
class UserPrefs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tone: str = "friendly"
    signature: str = ""
    language: str = "en"


class UserRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    google_sub: str
    email: str
    name: str | None = None
    timezone: str = "UTC"
    digest_time: time = time(9, 0)
    locale: str = "en"
    prefs: dict[str, Any] = Field(default_factory=dict)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    next_digest_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    google_sub: str
    email: str
    name: str | None = None
    timezone: str = "UTC"
    digest_time: str = "09:00:00"
    locale: str = "en"
    prefs: dict[str, Any] = Field(
        default_factory=lambda: {"tone": "friendly", "signature": "", "language": "en"}
    )
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    next_digest_at: datetime | None = None


class UserUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    digest_time: str | None = None
    locale: str | None = None
    prefs: dict[str, Any] | None = None
    feature_flags: dict[str, bool] | None = None
    next_digest_at: datetime | None = None


# -----------------------------------------------------------------------------
# OAuth & Session Models
# -----------------------------------------------------------------------------
class OAuthTokenRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    user_id: UUID
    refresh_token_enc: str
    key_version: int = 1
    scopes: list[str] = Field(default_factory=list)
    status: OAuthStatus = OAuthStatus.ACTIVE
    last_refreshed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OAuthTokenUpsert(BaseModel):
    user_id: str
    refresh_token_enc: str
    key_version: int = 1
    scopes: list[str]
    status: str = "active"
    last_refreshed_at: str | None = None


class SessionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    token_hash: str
    expires_at: datetime
    user_agent: str | None = None
    ip: str | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class SessionCreate(BaseModel):
    user_id: str
    token_hash: str
    expires_at: str
    user_agent: str | None = None
    ip: str | None = None


# -----------------------------------------------------------------------------
# Sync State Models
# -----------------------------------------------------------------------------
class SyncStateRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    user_id: UUID
    last_history_id: str | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None
    lock_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


# -----------------------------------------------------------------------------
# Email Models
# -----------------------------------------------------------------------------
class EmailRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    gmail_msg_id: str
    thread_id: str
    rfc_message_id: str | None = None
    sender: str
    sender_email: str
    reply_to: str | None = None
    subject: str
    received_at: datetime
    snippet: str | None = None
    body_clean: str
    has_attachments: bool = False
    list_unsubscribe: str | None = None
    labels: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None
    created_at: datetime
    updated_at: datetime


class EmailListItem(BaseModel):
    """List projection: NEVER includes body_clean for high performance."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    gmail_msg_id: str
    thread_id: str
    sender: str
    sender_email: str
    subject: str
    received_at: datetime
    snippet: str | None = None
    has_attachments: bool = False
    labels: list[str] = Field(default_factory=list)


class EmailCreate(BaseModel):
    user_id: str
    gmail_msg_id: str
    thread_id: str
    rfc_message_id: str | None = None
    sender: str
    sender_email: str
    reply_to: str | None = None
    subject: str
    received_at: str
    snippet: str | None = None
    body_clean: str
    has_attachments: bool = False
    list_unsubscribe: str | None = None
    labels: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None


# -----------------------------------------------------------------------------
# Email Analysis Models
# -----------------------------------------------------------------------------
class ActionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task: str
    assignee: str | None = None
    due_date: str | None = None


class EmailAnalysisRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    email_id: UUID
    category: EmailCategory
    needs_reply: bool
    priority: EmailPriority
    summary: str
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    deadline: date | None = None
    confidence: float
    risk_flags: list[str] = Field(default_factory=list)
    model: str
    prompt_version: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    created_at: datetime


class EmailAnalysisCreate(BaseModel):
    email_id: str
    category: str
    needs_reply: bool
    priority: str
    summary: str
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    deadline: str | None = None
    confidence: float
    risk_flags: list[str] = Field(default_factory=list)
    model: str
    prompt_version: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


# -----------------------------------------------------------------------------
# Draft & Version Models
# -----------------------------------------------------------------------------
class DraftRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email_id: UUID
    status: DraftStatus
    current_version: int = 1
    idempotency_key: str
    sent_gmail_msg_id: str | None = None
    sent_at: datetime | None = None
    failure_reason: str | None = None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class DraftCreate(BaseModel):
    user_id: str
    email_id: str
    status: str = "draft_generated"
    current_version: int = 1
    idempotency_key: str
    expires_at: str


class DraftVersionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    draft_id: UUID
    version: int
    body: str
    user_instruction: str | None = None
    source: DraftVersionSource
    model: str
    created_at: datetime


class DraftVersionCreate(BaseModel):
    draft_id: str
    version: int
    body: str
    user_instruction: str | None = None
    source: str = "llm"
    model: str


# -----------------------------------------------------------------------------
# Telegram Models
# -----------------------------------------------------------------------------
class TelegramLinkRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    user_id: UUID
    chat_id: int
    is_active: bool = True
    linked_at: datetime
    updated_at: datetime


class TelegramLinkTokenRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    token_hash: str
    user_id: UUID
    expires_at: datetime
    used_at: datetime | None = None
    created_at: datetime


class CallbackTokenRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    token: str
    user_id: UUID
    chat_id: int
    draft_id: UUID
    action: str
    expires_at: datetime
    used_at: datetime | None = None
    created_at: datetime


class PendingInteractionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    chat_id: int
    user_id: UUID
    draft_id: UUID
    type: str
    expires_at: datetime
    created_at: datetime


# -----------------------------------------------------------------------------
# Notification & Rule Models
# -----------------------------------------------------------------------------
class NotificationPrefsRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    user_id: UUID
    channel: NotificationChannel = NotificationChannel.TELEGRAM
    quiet_start: time | None = None
    quiet_end: time | None = None
    minimal_mode: bool = False
    snoozed_until: datetime | None = None
    digest_enabled: bool = True
    created_at: datetime
    updated_at: datetime


class RuleRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    condition: dict[str, Any]
    action: dict[str, Any]
    trust_level: TrustLevel = TrustLevel.SUGGEST
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


class RuleCreate(BaseModel):
    user_id: str
    condition: dict[str, Any]
    action: dict[str, Any]
    trust_level: str = "suggest"
    enabled: bool = True


class RuleUpdate(BaseModel):
    condition: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    trust_level: str | None = None
    enabled: bool | None = None


class FeedbackRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    entity_type: str
    entity_id: str
    rating: int
    note: str | None = None
    created_at: datetime


class FeedbackCreate(BaseModel):
    user_id: str
    entity_type: str
    entity_id: str
    rating: int
    note: str | None = None


class AuditLogRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID | None = None
    action: str
    entity: str
    entity_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: datetime


# -----------------------------------------------------------------------------
# Later-phase Feature Models (Phase 8)
# -----------------------------------------------------------------------------
class TaskRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email_id: UUID | None = None
    title: str
    due_date: date | None = None
    status: str = "pending"
    created_at: datetime
    updated_at: datetime


class ReminderRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email_id: UUID | None = None
    remind_at: datetime
    note: str | None = None
    triggered: bool = False
    created_at: datetime


class FollowupRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email_id: UUID | None = None
    recipient: str
    subject: str
    sent_at: datetime
    due_at: datetime
    resolved: bool = False
    created_at: datetime


class CommitmentRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email_id: UUID | None = None
    promise: str
    promised_to: str
    due_date: date | None = None
    status: str = "open"
    created_at: datetime


class ContactRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email: str
    name: str | None = None
    vip: bool = False
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class BillRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    id: UUID
    user_id: UUID
    email_id: UUID | None = None
    biller: str
    amount: float
    currency: str = "USD"
    due_date: date | None = None
    paid: bool = False
    created_at: datetime
