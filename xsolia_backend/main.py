from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import time
import warnings
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import sqrt
from threading import Lock
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from pydantic import field_validator, model_validator
from sqlalchemy import UniqueConstraint, case, func, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel, Session, create_engine, select

# ========= Database =========
DATABASE_URL = (
    os.getenv("XSOLIA_DATABASE_URL")
    or os.getenv("KROTKA_DATABASE_URL")
    or "sqlite:///./database.db"
)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
)

# ========= Security constants =========
ROLES = {"creator", "tester"}
SUBSCRIPTIONS = {"free", "creator_basic", "creator_plus"}
INNOVATION_INTENTS = {"open", "looking_for_team", "just_idea"}
PROJECT_STATUS_UPDATES = {"active", "closed", "archived"}
INNOVATION_STATUS_UPDATES = {"active", "archived"}
PROJECT_VISIBILITIES = {"public", "tester_only", "invite_only", "private_link"}
PROJECT_DETAIL_LEVELS = {"problem_only", "concept_summary", "full_description"}

PBKDF2_ITERATIONS = int(
    os.getenv("XSOLIA_PBKDF2_ITERATIONS")
    or os.getenv("KROTKA_PBKDF2_ITERATIONS")
    or "180000"
)
TOKEN_EXPIRE_SECONDS = int(
    os.getenv("XSOLIA_TOKEN_EXPIRE_SECONDS")
    or os.getenv("KROTKA_TOKEN_EXPIRE_SECONDS")
    or "604800"
)
SECRET_KEY = (
    os.getenv("XSOLIA_SECRET_KEY")
    or os.getenv("KROTKA_SECRET_KEY")
    or "dev-secret-change-me"
)
APP_ENV = (
    os.getenv("XSOLIA_ENV")
    or os.getenv("KROTKA_ENV")
    or os.getenv("ENV")
    or "development"
).lower()

FREE_CREATOR_PROJECT_QUOTA = int(
    os.getenv("XSOLIA_FREE_CREATOR_PROJECT_QUOTA")
    or os.getenv("KROTKA_FREE_CREATOR_PROJECT_QUOTA")
    or "1"
)

AUTH_RATE_LIMIT_WINDOW_SECONDS = int(
    os.getenv("XSOLIA_AUTH_RATE_LIMIT_WINDOW_SECONDS")
    or os.getenv("KROTKA_AUTH_RATE_LIMIT_WINDOW_SECONDS")
    or "300"
)
AUTH_RATE_LIMIT_MAX_ATTEMPTS = int(
    os.getenv("XSOLIA_AUTH_RATE_LIMIT_MAX_ATTEMPTS")
    or os.getenv("KROTKA_AUTH_RATE_LIMIT_MAX_ATTEMPTS")
    or "20"
)
AUTH_RATE_LIMIT_SWEEP_INTERVAL_SECONDS = int(
    os.getenv("XSOLIA_AUTH_RATE_LIMIT_SWEEP_INTERVAL_SECONDS")
    or os.getenv("KROTKA_AUTH_RATE_LIMIT_SWEEP_INTERVAL_SECONDS")
    or "120"
)
AI_PROVIDER = (
    os.getenv("XSOLIA_AI_PROVIDER")
    or os.getenv("KROTKA_AI_PROVIDER")
    or "disabled"
).strip().lower()
DEFAULT_AI_MODEL = "gemini-2.0-flash" if AI_PROVIDER == "gemini" else "gpt-4o"
AI_MODEL = (
    os.getenv("XSOLIA_AI_MODEL")
    or os.getenv("KROTKA_AI_MODEL")
    or DEFAULT_AI_MODEL
)
AI_API_KEY = (
    os.getenv("XSOLIA_AI_API_KEY")
    or os.getenv("KROTKA_AI_API_KEY")
    or os.getenv("OPENAI_API_KEY")
)
GEMINI_API_KEY = (
    os.getenv("XSOLIA_GEMINI_API_KEY")
    or os.getenv("KROTKA_GEMINI_API_KEY")
    or os.getenv("GEMINI_API_KEY")
    or AI_API_KEY
)
AI_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("XSOLIA_AI_REQUEST_TIMEOUT_SECONDS")
    or os.getenv("KROTKA_AI_REQUEST_TIMEOUT_SECONDS")
    or "45"
)
ALLOWED_ORIGINS_RAW = (
    os.getenv("XSOLIA_ALLOWED_ORIGINS")
    or os.getenv("KROTKA_ALLOWED_ORIGINS")
    or "*"
)
if ALLOWED_ORIGINS_RAW.strip() == "*":
    ALLOWED_ORIGINS = ["*"]
else:
    ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS_RAW.split(",") if origin.strip()]
    if not ALLOWED_ORIGINS:
        ALLOWED_ORIGINS = ["*"]

_AUTH_ATTEMPTS: dict[str, list[float]] = {}
_AUTH_ATTEMPTS_LOCK = Lock()
_AUTH_LAST_SWEEP_AT = 0.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ====================== Models ======================


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    password_hash: str
    role: str = "tester"
    subscription: str = "free"
    points: int = 0
    username: Optional[str] = Field(default=None, unique=True, index=True)
    avatar_url: Optional[str] = None
    streak_current: int = Field(default=0)
    streak_best: int = Field(default=0)
    last_response_date: Optional[str] = None


class UserCreate(SQLModel):
    email: str
    name: str
    password: str
    role: str = "tester"
    subscription: str = "free"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized:
            raise ValueError("Invalid email")
        return normalized

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("Name is too short")
        if len(cleaned) > 64:
            raise ValueError("Name is too long")
        return cleaned

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(value) > 128:
            raise ValueError("Password is too long")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError("Invalid role")
        return value

    @field_validator("subscription")
    @classmethod
    def validate_subscription(cls, value: str) -> str:
        if value not in SUBSCRIPTIONS:
            raise ValueError("Invalid subscription plan")
        return value

    @model_validator(mode="after")
    def validate_role_subscription(self):
        if self.role == "tester" and self.subscription != "free":
            raise ValueError("Testers can only use free plan")
        return self


class UserLogin(SQLModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class TesterReputationOut(SQLModel):
    responses_count: int = 0
    accepted_responses_count: int = 0
    acceptance_rate: float = 0.0
    avg_interest_given: float = 0.0
    best_categories: list[str] = Field(default_factory=list)
    reliability_score: int = 0


class UserOut(SQLModel):
    id: int
    email: str
    name: str
    role: str
    subscription: str
    points: int
    username: Optional[str] = None
    avatar_url: Optional[str] = None
    streak_current: int = 0
    streak_best: int = 0
    responses_count: int = 0
    accepted_responses_count: int = 0
    acceptance_rate: float = 0.0
    avg_interest_given: float = 0.0
    best_categories: list[str] = Field(default_factory=list)
    reliability_score: int = 0


class LoginOut(SQLModel):
    user_id: int
    role: str
    name: str
    subscription: str
    username: Optional[str] = None
    avatar_url: Optional[str] = None
    access_token: str
    token_type: str
    expires_in: int


class UsernameUpdate(SQLModel):
    username: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,30}", cleaned):
            raise ValueError("Username must be 3-30 characters using letters, numbers, or underscores")
        return cleaned


class AvatarUpdate(SQLModel):
    avatar_url: Optional[str] = None

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > 2_500_000:
            raise ValueError("Avatar image is too large")
        if cleaned.startswith("data:image/") and ";base64," in cleaned:
            return cleaned
        if cleaned.startswith("https://") or cleaned.startswith("http://"):
            return cleaned
        raise ValueError("Avatar must be a valid image URL or data URL")


class PublicTopProject(SQLModel):
    title: str
    responses_count: int
    avg_interest: Optional[float]


class UserPublicOut(SQLModel):
    username: str
    name: str
    role: str
    projects_count: int
    total_responses: int
    avg_interest: Optional[float]
    top_project: Optional[PublicTopProject]
    top_category: Optional[str] = None
    points: int = 0
    streak_current: int = 0
    streak_best: int = 0
    responses_count: int = 0
    accepted_responses_count: int = 0
    acceptance_rate: float = 0.0
    avg_interest_given: float = 0.0
    best_categories: list[str] = Field(default_factory=list)
    reliability_score: int = 0


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    creator_id: int = Field(index=True)
    title: str
    description: str
    target_audience: str
    questions: str
    image_urls: str = "[]"
    reward_note: Optional[str] = None
    budget: int
    main_category: str = "testing"
    subcategory: Optional[str] = None
    status: str = "active"
    visibility: str = "public"
    detail_level: str = "concept_summary"
    allow_indexing: bool = False
    source_innovation_id: Optional[int] = Field(default=None, index=True)


class ProjectQuestion(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("project_id", "position", name="uq_project_question_position"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    position: int = Field(ge=0)
    text: str


class ProjectAISummary(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    model: str
    input_hash: str = Field(index=True)
    summary_json: str
    created_at: datetime = Field(default_factory=utc_now)


class ProjectCreate(SQLModel):
    title: str
    description: str
    target_audience: str
    questions: list[str] = Field(min_length=1, max_length=8)
    image_urls: list[str] = Field(default_factory=list, max_length=3)
    reward_note: Optional[str] = None
    budget: int = Field(ge=0, le=1_000_000_000)
    main_category: str = "testing"
    subcategory: Optional[str] = None
    visibility: str = "public"
    detail_level: str = "concept_summary"
    allow_indexing: bool = False
    source_innovation_id: Optional[int] = Field(default=None, ge=1)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("Title is too short")
        if len(cleaned) > 160:
            raise ValueError("Title is too long")
        return cleaned

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 10:
            raise ValueError("Description is too short")
        if len(cleaned) > 4000:
            raise ValueError("Description is too long")
        return cleaned

    @field_validator("target_audience")
    @classmethod
    def validate_target_audience(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("Target audience is too short")
        if len(cleaned) > 500:
            raise ValueError("Target audience is too long")
        return cleaned

    @field_validator("questions")
    @classmethod
    def validate_questions(cls, value: list[str]) -> list[str]:
        cleaned = [q.strip() for q in value if q and q.strip()]
        if not cleaned:
            raise ValueError("At least one question is required")
        for question in cleaned:
            if len(question) > 500:
                raise ValueError("Question is too long")
        return cleaned

    @field_validator("image_urls")
    @classmethod
    def validate_image_urls(cls, value: list[str]) -> list[str]:
        cleaned = [image.strip() for image in value if image and image.strip()]
        if len(cleaned) > 3:
            raise ValueError("At most 3 images are allowed")
        for image in cleaned:
            if len(image) > 2_048:
                raise ValueError("Image URL is too long")
            if image.startswith("https://") or image.startswith("http://"):
                continue
            raise ValueError("Each image must be a valid image URL")
        return cleaned

    @field_validator("reward_note")
    @classmethod
    def validate_reward_note(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) > 500:
            raise ValueError("Reward note is too long")
        return cleaned or None

    @field_validator("main_category")
    @classmethod
    def validate_main_category(cls, value: str) -> str:
        cleaned = value.strip().lower() or "testing"
        if len(cleaned) > 64:
            raise ValueError("Main category is too long")
        return cleaned

    @field_validator("subcategory")
    @classmethod
    def validate_subcategory(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if len(cleaned) > 64:
            raise ValueError("Subcategory is too long")
        return cleaned or None

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in PROJECT_VISIBILITIES:
            raise ValueError("Invalid visibility")
        return cleaned

    @field_validator("detail_level")
    @classmethod
    def validate_detail_level(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in PROJECT_DETAIL_LEVELS:
            raise ValueError("Invalid detail level")
        return cleaned


class ProjectStatusUpdate(SQLModel):
    status: str
    launched: bool = False

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in PROJECT_STATUS_UPDATES:
            raise ValueError("Invalid project status")
        return cleaned


class ProjectOut(SQLModel):
    id: int
    creator_id: int
    title: str
    description: str
    target_audience: str
    questions: list[str]
    image_urls: list[str]
    reward_note: Optional[str]
    budget: int
    main_category: str
    subcategory: Optional[str]
    status: str
    visibility: str = "public"
    detail_level: str = "concept_summary"
    allow_indexing: bool = False
    source_innovation_id: Optional[int] = None


class ProjectAISummaryOut(SQLModel):
    project_id: int
    responses_count: int
    model: str
    input_hash: str
    cached: bool
    generated_at: datetime
    summary: dict[str, Any]


class Response(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_response_user"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    user_id: int = Field(index=True)
    interest_level: int
    answers: str
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    accepted_by_creator: bool = False
    accepted_at: Optional[datetime] = None
    likes_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class ResponseAnswer(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("response_id", "position", name="uq_response_answer_position"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: int = Field(foreign_key="response.id", index=True)
    question_id: Optional[int] = Field(default=None, foreign_key="projectquestion.id")
    position: int = Field(ge=0)
    text: str


class ResponseLike(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("response_id", "user_id", name="uq_response_like_user"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: int = Field(foreign_key="response.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)


class ResponseComment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: int = Field(foreign_key="response.id", index=True)
    author_id: int = Field(foreign_key="user.id", index=True)
    text: str
    created_at: datetime = Field(default_factory=utc_now)


class ResponseCreate(SQLModel):
    interest_level: int = Field(ge=1, le=5)
    answers: list[str] = Field(min_length=1, max_length=12)
    price_min: Optional[int] = Field(default=None, ge=0)
    price_max: Optional[int] = Field(default=None, ge=0)

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value: list[str]) -> list[str]:
        cleaned = [answer.strip() for answer in value if answer and answer.strip()]
        if not cleaned:
            raise ValueError("At least one answer is required")
        for answer in cleaned:
            if len(answer) > 3000:
                raise ValueError("Answer is too long")
        return cleaned

    @model_validator(mode="after")
    def validate_price_range(self):
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("price_min cannot be greater than price_max")
        return self


class ResponseCommentCreate(SQLModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 1:
            raise ValueError("Comment text is required")
        if len(cleaned) > 2000:
            raise ValueError("Comment text is too long")
        return cleaned


class ResponseOut(SQLModel):
    id: int
    project_id: int
    project_title: Optional[str] = None
    user_id: int
    interest_level: int
    answers: list[str]
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    accepted_by_creator: bool
    likes_count: int
    created_at: datetime
    responder_name: Optional[str] = None
    responder_reputation: Optional[TesterReputationOut] = None


class ResponseCommentOut(SQLModel):
    id: int
    response_id: int
    author_id: int
    author_name: Optional[str] = None
    text: str
    created_at: datetime


class DailyPicksOut(SQLModel):
    completed_today: bool
    picks: list[ProjectOut]


class TesterLeaderboardEntryOut(SQLModel):
    rank: int
    user_id: int
    username: str
    name: str
    points: int
    streak_best: int
    responses_count: int
    accepted_responses_count: int
    acceptance_rate: float
    reliability_score: int
    best_categories: list[str] = Field(default_factory=list)


class CreatorDashboardSummaryOut(SQLModel):
    total_projects: int = 0
    active_projects: int = 0
    total_responses: int = 0
    avg_interest_overall: float = 0.0
    projects_needing_decision: int = 0


class CreatorDashboardProjectOut(SQLModel):
    id: int
    title: str
    status: str
    visibility: str
    responses_count: int
    avg_interest: Optional[float]
    acceptance_rate: float
    avg_price_min: Optional[float]
    avg_price_max: Optional[float]
    decision_stage: str
    top_signal: str
    next_step: str
    source_innovation_id: Optional[int] = None


class CreatorDashboardOut(SQLModel):
    summary: CreatorDashboardSummaryOut
    projects: list[CreatorDashboardProjectOut]


class Notification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    type: str
    payload_json: str
    read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class NotificationOut(SQLModel):
    id: int
    type: str
    payload: dict[str, Any]
    read: bool
    created_at: datetime


class ProjectStats(SQLModel):
    project_id: int
    responses_count: int
    interest_distribution: dict[int, int]
    avg_interest: Optional[float]
    interest_stddev: Optional[float]
    avg_price_min: Optional[float]
    avg_price_max: Optional[float]
    price_percentiles: Optional[dict[str, float]]
    acceptance_rate: float


class Innovation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    author_id: int = Field(index=True)
    title: str
    description: str
    tags: Optional[str] = None
    intent: str = "open"
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    upvotes: int = 0


class InnovationVote(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("innovation_id", "user_id", name="uq_innovation_vote_user"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    innovation_id: int = Field(foreign_key="innovation.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)


class InnovationSave(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("innovation_id", "user_id", name="uq_innovation_save_user"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    innovation_id: int = Field(foreign_key="innovation.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)


class InnovationComment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    innovation_id: int = Field(foreign_key="innovation.id", index=True)
    author_id: int = Field(foreign_key="user.id", index=True)
    text: str
    created_at: datetime = Field(default_factory=utc_now)


class InnovationCreate(SQLModel):
    title: str
    description: str
    tags: Optional[list[str]] = None
    intent: str = "open"

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("Title is too short")
        if len(cleaned) > 160:
            raise ValueError("Title is too long")
        return cleaned

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 10:
            raise ValueError("Description is too short")
        if len(cleaned) > 4000:
            raise ValueError("Description is too long")
        return cleaned

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = []
        for raw_tag in value:
            tag = raw_tag.strip().lower()
            if not tag:
                continue
            if len(tag) > 32:
                raise ValueError("Tag is too long")
            cleaned.append(tag)
        if len(cleaned) > 10:
            raise ValueError("Too many tags")
        return cleaned or None

    @field_validator("intent")
    @classmethod
    def validate_intent(cls, value: str) -> str:
        if value not in INNOVATION_INTENTS:
            raise ValueError("Invalid intent")
        return value


class InnovationOut(SQLModel):
    id: int
    author_id: int
    title: str
    description: str
    tags: list[str]
    intent: str
    status: str
    created_at: datetime
    upvotes: int


class InnovationStatusUpdate(SQLModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in INNOVATION_STATUS_UPDATES:
            raise ValueError("Invalid innovation status")
        return cleaned


class InnovationCommentCreate(SQLModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 1:
            raise ValueError("Comment text is required")
        if len(cleaned) > 2000:
            raise ValueError("Comment text is too long")
        return cleaned


class InnovationCommentOut(SQLModel):
    id: int
    innovation_id: int
    author_id: int
    author_name: Optional[str] = None
    text: str
    created_at: datetime


class InnovationValidationDraftOut(SQLModel):
    title: str
    description: str
    main_category: Optional[str] = None
    subcategory: Optional[str] = None
    target_audience: str = ""
    questions: list[str]
    source_innovation_id: int


# ====================== Utilities ======================


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    ensure_user_profile_columns()
    ensure_project_columns()


def ensure_user_profile_columns() -> None:
    dialect = engine.dialect.name
    with engine.begin() as connection:
        if dialect == "sqlite":
            existing_columns = {
                row[1]
                for row in connection.execute(text('PRAGMA table_info("user")')).fetchall()
            }
            column_sql = {
                "username": 'ALTER TABLE "user" ADD COLUMN username VARCHAR',
                "avatar_url": 'ALTER TABLE "user" ADD COLUMN avatar_url VARCHAR',
                "streak_current": 'ALTER TABLE "user" ADD COLUMN streak_current INTEGER NOT NULL DEFAULT 0',
                "streak_best": 'ALTER TABLE "user" ADD COLUMN streak_best INTEGER NOT NULL DEFAULT 0',
                "last_response_date": 'ALTER TABLE "user" ADD COLUMN last_response_date VARCHAR',
            }
            for column_name, statement in column_sql.items():
                if column_name not in existing_columns:
                    connection.execute(text(statement))
            connection.execute(
                text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_username ON "user" (username) WHERE username IS NOT NULL')
            )
            return

        if dialect == "postgresql":
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS username VARCHAR'))
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS avatar_url VARCHAR'))
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS streak_current INTEGER NOT NULL DEFAULT 0'))
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS streak_best INTEGER NOT NULL DEFAULT 0'))
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS last_response_date VARCHAR'))
            connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_username ON "user" (username) WHERE username IS NOT NULL'))


def ensure_project_columns() -> None:
    dialect = engine.dialect.name
    with engine.begin() as connection:
        if dialect == "sqlite":
            existing_columns = {
                row[1]
                for row in connection.execute(text('PRAGMA table_info("project")')).fetchall()
            }
            if "image_urls" not in existing_columns:
                connection.execute(
                    text('ALTER TABLE "project" ADD COLUMN image_urls VARCHAR NOT NULL DEFAULT \'[]\'')
                )
            if "visibility" not in existing_columns:
                connection.execute(
                    text('ALTER TABLE "project" ADD COLUMN visibility VARCHAR NOT NULL DEFAULT \'public\'')
                )
            if "detail_level" not in existing_columns:
                connection.execute(
                    text('ALTER TABLE "project" ADD COLUMN detail_level VARCHAR NOT NULL DEFAULT \'concept_summary\'')
                )
            if "allow_indexing" not in existing_columns:
                connection.execute(
                    text('ALTER TABLE "project" ADD COLUMN allow_indexing BOOLEAN NOT NULL DEFAULT 0')
                )
            if "source_innovation_id" not in existing_columns:
                connection.execute(
                    text('ALTER TABLE "project" ADD COLUMN source_innovation_id INTEGER')
                )
            connection.execute(text('UPDATE "project" SET image_urls = \'[]\' WHERE image_urls IS NULL OR trim(image_urls) = \'\''))
            connection.execute(text('UPDATE "project" SET visibility = \'public\' WHERE visibility IS NULL OR trim(visibility) = \'\''))
            connection.execute(text('UPDATE "project" SET detail_level = \'concept_summary\' WHERE detail_level IS NULL OR trim(detail_level) = \'\''))
            connection.execute(text("UPDATE \"project\" SET allow_indexing = 0 WHERE allow_indexing IS NULL"))
            connection.execute(text('CREATE INDEX IF NOT EXISTS ix_project_source_innovation_id ON "project" (source_innovation_id)'))
            return

        if dialect == "postgresql":
            connection.execute(text('ALTER TABLE "project" ADD COLUMN IF NOT EXISTS image_urls VARCHAR'))
            connection.execute(text('ALTER TABLE "project" ADD COLUMN IF NOT EXISTS visibility VARCHAR'))
            connection.execute(text('ALTER TABLE "project" ADD COLUMN IF NOT EXISTS detail_level VARCHAR'))
            connection.execute(text('ALTER TABLE "project" ADD COLUMN IF NOT EXISTS allow_indexing BOOLEAN NOT NULL DEFAULT FALSE'))
            connection.execute(text('ALTER TABLE "project" ADD COLUMN IF NOT EXISTS source_innovation_id INTEGER'))
            connection.execute(text("UPDATE \"project\" SET image_urls = '[]' WHERE image_urls IS NULL OR btrim(image_urls) = ''"))
            connection.execute(text("UPDATE \"project\" SET visibility = 'public' WHERE visibility IS NULL OR btrim(visibility) = ''"))
            connection.execute(text("UPDATE \"project\" SET detail_level = 'concept_summary' WHERE detail_level IS NULL OR btrim(detail_level) = ''"))
            connection.execute(text("UPDATE \"project\" SET allow_indexing = FALSE WHERE allow_indexing IS NULL"))
            connection.execute(text('CREATE INDEX IF NOT EXISTS ix_project_source_innovation_id ON "project" (source_innovation_id)'))


def get_session():
    with Session(engine) as session:
        yield session


def encode_legacy_list(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def decode_legacy_list(raw: str) -> list[str]:
    if not raw:
        return []

    raw_text = raw.strip()
    if raw_text.startswith("["):
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass

    return [segment.strip() for segment in raw.split("\n") if segment and segment.strip()]


def encode_innovation_tags(tags: Optional[list[str]]) -> Optional[str]:
    if not tags:
        return None
    return json.dumps(tags, ensure_ascii=False)


def decode_innovation_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []

    raw_text = raw.strip()
    if raw_text.startswith("["):
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass

    # Backward compatibility for legacy comma-separated storage.
    return [segment.strip() for segment in raw.split(",") if segment and segment.strip()]


def _description_by_detail_level(project: Project, viewer: Optional[User]) -> str:
    if viewer and viewer.id == project.creator_id:
        return project.description

    detail_level = (project.detail_level or "concept_summary").strip().lower()
    source = project.description or ""
    if detail_level == "full_description":
        return source
    if detail_level == "problem_only":
        sentence = re.split(r"(?<=[.!?])\s+", source.strip(), maxsplit=1)[0]
        cleaned = sentence.strip()
        if cleaned:
            return cleaned
        return "Problem context shared. Additional details are intentionally hidden."
    if len(source) <= 280:
        return source
    return f"{source[:277].rstrip()}..."


def can_view_project(project: Project, current_user: Optional[User]) -> bool:
    if current_user and current_user.id == project.creator_id:
        return True

    visibility = (project.visibility or "public").strip().lower()
    if visibility == "public":
        return True
    if visibility == "tester_only":
        return current_user is not None and current_user.role in {"tester", "creator"}
    if visibility == "invite_only":
        return False
    if visibility == "private_link":
        return True
    return True


def can_answer_project(project: Project, current_user: User) -> bool:
    if current_user.role != "tester":
        return False
    if current_user.id == project.creator_id:
        return False
    return can_view_project(project, current_user)


def _compute_reliability_score(
    responses_count: int,
    accepted_responses_count: int,
    streak_best: int,
) -> int:
    score = 0
    score += min(responses_count * 2, 40)
    score += min(accepted_responses_count * 5, 40)
    score += min(streak_best * 2, 20)
    return max(0, min(100, int(score)))


def build_tester_reputation_map(
    session: Session,
    user_ids: list[int],
) -> dict[int, TesterReputationOut]:
    cleaned_ids = sorted({int(user_id) for user_id in user_ids if user_id is not None})
    if not cleaned_ids:
        return {}

    user_rows = session.exec(
        select(User.id, User.streak_best).where(User.id.in_(cleaned_ids))
    ).all()
    streak_map = {int(row[0]): int(row[1] or 0) for row in user_rows}

    stat_rows = session.exec(
        select(
            Response.user_id,
            func.count(Response.id),
            func.sum(case((Response.accepted_by_creator.is_(True), 1), else_=0)),
            func.avg(Response.interest_level),
        )
        .where(Response.user_id.in_(cleaned_ids))
        .group_by(Response.user_id)
    ).all()
    stats_map: dict[int, tuple[int, int, float]] = {}
    for row in stat_rows:
        stats_map[int(row[0])] = (
            int(row[1] or 0),
            int(row[2] or 0),
            float(row[3] or 0.0),
        )

    category_rows = session.exec(
        select(Project.main_category, Response.user_id, func.count(Response.id))
        .select_from(Response)
        .join(Project, Project.id == Response.project_id)
        .where(Response.user_id.in_(cleaned_ids))
        .group_by(Response.user_id, Project.main_category)
        .order_by(Response.user_id.asc(), func.count(Response.id).desc(), Project.main_category.asc())
    ).all()
    best_categories_map: dict[int, list[str]] = {}
    for category, user_id, _count in category_rows:
        uid = int(user_id)
        categories = best_categories_map.setdefault(uid, [])
        if category and category not in categories and len(categories) < 3:
            categories.append(category)

    reputation_map: dict[int, TesterReputationOut] = {}
    for user_id in cleaned_ids:
        responses_count, accepted_count, avg_interest = stats_map.get(user_id, (0, 0, 0.0))
        acceptance_rate = (accepted_count / responses_count) if responses_count else 0.0
        streak_best = streak_map.get(user_id, 0)
        reputation_map[user_id] = TesterReputationOut(
            responses_count=responses_count,
            accepted_responses_count=accepted_count,
            acceptance_rate=acceptance_rate,
            avg_interest_given=avg_interest if responses_count else 0.0,
            best_categories=best_categories_map.get(user_id, []),
            reliability_score=_compute_reliability_score(
                responses_count=responses_count,
                accepted_responses_count=accepted_count,
                streak_best=streak_best,
            ),
        )

    return reputation_map


def serialize_user_out(user: User, session: Session) -> UserOut:
    tester_summary = TesterReputationOut()
    if user.role == "tester":
        tester_summary = build_tester_reputation_map(session, [user.id]).get(user.id, TesterReputationOut())

    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        subscription=user.subscription,
        points=user.points,
        username=user.username,
        avatar_url=user.avatar_url,
        streak_current=user.streak_current,
        streak_best=user.streak_best,
        responses_count=tester_summary.responses_count,
        accepted_responses_count=tester_summary.accepted_responses_count,
        acceptance_rate=tester_summary.acceptance_rate,
        avg_interest_given=tester_summary.avg_interest_given,
        best_categories=tester_summary.best_categories,
        reliability_score=tester_summary.reliability_score,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('utf-8')}$"
        f"{base64.b64encode(digest).decode('utf-8')}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    # Backward compatibility for legacy sha256 hashes.
    if "$" not in password_hash:
        return hashlib.sha256(password.encode("utf-8")).hexdigest() == password_hash

    try:
        scheme, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
    except ValueError:
        return False

    if scheme != "pbkdf2_sha256":
        return False

    try:
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text.encode("utf-8"))
        expected_digest = base64.b64decode(digest_text.encode("utf-8"))
    except Exception:
        return False

    actual_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_digest, expected_digest)


def create_access_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=TOKEN_EXPIRE_SECONDS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    raw_sub = payload.get("sub")
    try:
        user_id = int(raw_sub)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")

    payload["sub"] = user_id
    return payload


def _enforce_auth_rate_limit(request: Request, email: str) -> None:
    global _AUTH_LAST_SWEEP_AT

    client_host = request.client.host if request.client and request.client.host else "unknown"
    key = f"{client_host}:{email.strip().lower()}"
    now = time.time()

    with _AUTH_ATTEMPTS_LOCK:
        if now - _AUTH_LAST_SWEEP_AT >= AUTH_RATE_LIMIT_SWEEP_INTERVAL_SECONDS:
            expired_keys = []
            for existing_key, existing_attempts in _AUTH_ATTEMPTS.items():
                filtered_attempts = [ts for ts in existing_attempts if now - ts <= AUTH_RATE_LIMIT_WINDOW_SECONDS]
                if filtered_attempts:
                    _AUTH_ATTEMPTS[existing_key] = filtered_attempts
                else:
                    expired_keys.append(existing_key)
            for expired_key in expired_keys:
                _AUTH_ATTEMPTS.pop(expired_key, None)
            _AUTH_LAST_SWEEP_AT = now

        attempts = _AUTH_ATTEMPTS.get(key, [])
        attempts = [ts for ts in attempts if now - ts <= AUTH_RATE_LIMIT_WINDOW_SECONDS]

        if len(attempts) >= AUTH_RATE_LIMIT_MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many auth attempts, please try again later")

        attempts.append(now)
        if attempts:
            _AUTH_ATTEMPTS[key] = attempts
        else:
            _AUTH_ATTEMPTS.pop(key, None)


def _build_project_questions_map(session: Session, project_ids: list[int]) -> dict[int, list[ProjectQuestion]]:
    if not project_ids:
        return {}

    rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id.in_(project_ids))
        .order_by(ProjectQuestion.project_id, ProjectQuestion.position)
    ).all()

    mapping: dict[int, list[ProjectQuestion]] = {}
    for row in rows:
        mapping.setdefault(row.project_id, []).append(row)
    return mapping


def _build_response_answers_map(session: Session, response_ids: list[int]) -> dict[int, list[ResponseAnswer]]:
    if not response_ids:
        return {}

    rows = session.exec(
        select(ResponseAnswer)
        .where(ResponseAnswer.response_id.in_(response_ids))
        .order_by(ResponseAnswer.response_id, ResponseAnswer.position)
    ).all()

    mapping: dict[int, list[ResponseAnswer]] = {}
    for row in rows:
        mapping.setdefault(row.response_id, []).append(row)
    return mapping


def serialize_project(
    project: Project,
    question_rows: Optional[list[ProjectQuestion]] = None,
    viewer: Optional[User] = None,
) -> ProjectOut:
    if question_rows is None:
        questions = decode_legacy_list(project.questions)
    else:
        questions = [row.text for row in question_rows]
        if not questions:
            questions = decode_legacy_list(project.questions)

    image_urls = decode_legacy_list(project.image_urls or "[]")

    return ProjectOut(
        id=project.id,
        creator_id=project.creator_id,
        title=project.title,
        description=_description_by_detail_level(project, viewer),
        target_audience=project.target_audience,
        questions=questions,
        image_urls=image_urls,
        reward_note=project.reward_note,
        budget=project.budget,
        main_category=project.main_category,
        subcategory=project.subcategory,
        status=project.status,
        visibility=project.visibility or "public",
        detail_level=project.detail_level or "concept_summary",
        allow_indexing=bool(project.allow_indexing),
        source_innovation_id=project.source_innovation_id,
    )


def serialize_project_ai_summary(
    summary: ProjectAISummary,
    responses_count: int,
    cached: bool,
) -> ProjectAISummaryOut:
    try:
        summary_payload = json.loads(summary.summary_json)
    except json.JSONDecodeError:
        summary_payload = {"summary": summary.summary_json}

    return ProjectAISummaryOut(
        project_id=summary.project_id,
        responses_count=responses_count,
        model=summary.model,
        input_hash=summary.input_hash,
        cached=cached,
        generated_at=summary.created_at,
        summary=summary_payload,
    )


def serialize_response(
    response: Response,
    answer_rows: Optional[list[ResponseAnswer]] = None,
    project_title: Optional[str] = None,
    responder_name: Optional[str] = None,
    responder_reputation: Optional[TesterReputationOut] = None,
) -> ResponseOut:
    if answer_rows is None:
        answers = decode_legacy_list(response.answers)
    else:
        answers = [row.text for row in answer_rows]
        if not answers:
            answers = decode_legacy_list(response.answers)

    return ResponseOut(
        id=response.id,
        project_id=response.project_id,
        project_title=project_title,
        user_id=response.user_id,
        interest_level=response.interest_level,
        answers=answers,
        price_min=response.price_min,
        price_max=response.price_max,
        accepted_by_creator=response.accepted_by_creator,
        likes_count=response.likes_count,
        created_at=response.created_at,
        responder_name=responder_name,
        responder_reputation=responder_reputation,
    )


def serialize_response_comment(
    comment: ResponseComment,
    author_name: Optional[str] = None,
) -> ResponseCommentOut:
    return ResponseCommentOut(
        id=comment.id,
        response_id=comment.response_id,
        author_id=comment.author_id,
        author_name=author_name,
        text=comment.text,
        created_at=comment.created_at,
    )


def serialize_notification(notification: Notification) -> NotificationOut:
    try:
        payload = json.loads(notification.payload_json)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return NotificationOut(
        id=notification.id,
        type=notification.type,
        payload=payload,
        read=notification.read,
        created_at=notification.created_at,
    )


def serialize_innovation(innovation: Innovation) -> InnovationOut:
    tags = decode_innovation_tags(innovation.tags)
    return InnovationOut(
        id=innovation.id,
        author_id=innovation.author_id,
        title=innovation.title,
        description=innovation.description,
        tags=tags,
        intent=innovation.intent,
        status=innovation.status,
        created_at=innovation.created_at,
        upvotes=innovation.upvotes,
    )


def serialize_innovation_comment(
    comment: InnovationComment,
    author_name: Optional[str] = None,
) -> InnovationCommentOut:
    return InnovationCommentOut(
        id=comment.id,
        innovation_id=comment.innovation_id,
        author_id=comment.author_id,
        author_name=author_name,
        text=comment.text,
        created_at=comment.created_at,
    )


INNOVATION_VALIDATION_DRAFT_QUESTIONS = [
    "What problem does this idea solve for you?",
    "How painful is this problem in your current workflow?",
    "What would make you try this solution?",
    "What concerns would stop you from using it?",
    "How much would you be willing to pay for this?",
]

CREATOR_DECISION_NEXT_STEP = {
    "draft_or_low_signal": "Get at least 5 responses before making decisions.",
    "testing": "Keep collecting responses.",
    "enough_signal": "Review top objections and consider building a landing page or MVP.",
    "weak_signal": "Reconsider positioning, target audience, or problem statement.",
    "decision_needed": "Close the test or make a build / pivot / drop decision.",
}


def infer_validation_category_from_innovation(innovation: Innovation) -> Optional[str]:
    tags = decode_innovation_tags(innovation.tags)
    normalized = " ".join(tags).lower()
    if any(keyword in normalized for keyword in {"saas", "app", "software", "ai", "dev", "api"}):
        return "digital"
    if any(keyword in normalized for keyword in {"hardware", "device", "wearable", "product"}):
        return "physical"
    if any(keyword in normalized for keyword in {"service", "consulting", "agency", "marketplace"}):
        return "service"
    if any(keyword in normalized for keyword in {"course", "creator", "learning", "education"}):
        return "education"
    if any(keyword in normalized for keyword in {"health", "fitness", "wellness"}):
        return "health"
    return None


def compute_creator_decision_stage(responses_count: int, avg_interest: Optional[float]) -> str:
    if responses_count >= 20:
        return "decision_needed"
    if responses_count < 5:
        return "draft_or_low_signal"
    if responses_count < 15:
        return "testing"
    if avg_interest is not None and avg_interest >= 3.5:
        return "enough_signal"
    return "weak_signal"


def update_tester_streak(tester: User) -> None:
    now = utc_now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    if tester.last_response_date == yesterday:
        tester.streak_current += 1
    elif tester.last_response_date != today:
        tester.streak_current = 1

    tester.streak_best = max(tester.streak_best, tester.streak_current)
    tester.last_response_date = today


def create_launch_notifications(session: Session, project: Project) -> None:
    response_rows = session.exec(
        select(Response.user_id, Response.interest_level)
        .where(Response.project_id == project.id)
        .order_by(Response.created_at.asc())
    ).all()

    seen_user_ids: set[int] = set()
    for user_id, interest_level in response_rows:
        if user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)
        payload = {
            "project_id": project.id,
            "project_title": project.title,
            "your_interest": interest_level,
        }
        session.add(
            Notification(
                user_id=user_id,
                type="prediction_confirmed",
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
        )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot compute percentile on empty list")

    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (len(sorted_values) - 1) * percentile
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = rank - lower_index
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return lower + (upper - lower) * weight


def _compute_price_percentiles(price_rows: list[tuple[Optional[int], Optional[int]]]) -> Optional[dict[str, float]]:
    samples: list[float] = []
    for price_min, price_max in price_rows:
        if price_min is not None and price_max is not None:
            samples.append((price_min + price_max) / 2)
        elif price_min is not None:
            samples.append(float(price_min))
        elif price_max is not None:
            samples.append(float(price_max))

    if not samples:
        return None

    return {
        "p25": _percentile(samples, 0.25),
        "p50": _percentile(samples, 0.50),
        "p75": _percentile(samples, 0.75),
    }


AI_SUMMARY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "key_signals": {"type": "array", "items": {"type": "string"}},
        "pricing_insight": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "willingness": {"type": "string"},
                "observed_range": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["willingness", "observed_range", "notes"],
        },
        "interest_insight": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "distribution_note": {"type": "string"},
                "best_segment": {"type": "string"},
            },
            "required": ["distribution_note", "best_segment"],
        },
        "top_objections": {"type": "array", "items": {"type": "string"}},
        "suggested_next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "key_signals",
        "pricing_insight",
        "interest_insight",
        "top_objections",
        "suggested_next_steps",
    ],
}


def _format_price_range(price_min: Optional[int], price_max: Optional[int]) -> str:
    if price_min is not None and price_max is not None:
        return f"{price_min}-{price_max}"
    if price_min is not None:
        return f"{price_min}+"
    if price_max is not None:
        return f"up to {price_max}"
    return "not specified"


def build_ai_summary_input(
    project: Project,
    question_rows: list[ProjectQuestion],
    responses: list[Response],
    answer_map: dict[int, list[ResponseAnswer]],
) -> str:
    questions = [row.text for row in question_rows] or decode_legacy_list(project.questions)
    lines = [
        "Project:",
        f"Title: {project.title}",
        f"Description: {project.description}",
        f"Target audience: {project.target_audience}",
        f"Reward note: {project.reward_note or 'not specified'}",
        f"Budget: {project.budget}",
        f"Category: {project.main_category} / {project.subcategory or 'none'}",
        "",
        "Questions:",
    ]

    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question}")

    lines.extend(["", f"Responses count: {len(responses)}", "Responses:"])
    if not responses:
        lines.append("- No responses yet.")
        return "\n".join(lines)

    for response in responses:
        lines.append(
            "- "
            f"Response #{response.id}; "
            f"Interest: {response.interest_level}/5; "
            f"Price: {_format_price_range(response.price_min, response.price_max)}; "
            f"Accepted: {str(response.accepted_by_creator).lower()}"
        )

        answer_rows = answer_map.get(response.id, [])
        answers = [row.text for row in answer_rows] or decode_legacy_list(response.answers)
        for index, answer in enumerate(answers, start=1):
            question_text = questions[index - 1] if index - 1 < len(questions) else f"Question {index}"
            lines.append(f"  Q{index}: {question_text}")
            lines.append(f"  A{index}: {answer}")

    return "\n".join(lines)


def _extract_response_text(response_payload: dict[str, Any]) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = response_payload.get("output")
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        if chunks:
            return "\n".join(chunks)

    raise HTTPException(status_code=502, detail="AI provider returned an empty response")


def _schema_with_property_ordering(schema: dict[str, Any]) -> dict[str, Any]:
    copied_schema = json.loads(json.dumps(schema))

    def add_ordering(node: dict[str, Any]) -> None:
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["propertyOrdering"] = list(properties.keys())
            for child in properties.values():
                if isinstance(child, dict):
                    add_ordering(child)

        items = node.get("items")
        if isinstance(items, dict):
            add_ordering(items)

    add_ordering(copied_schema)
    return copied_schema


async def _call_openai_summary(summary_input: str) -> dict[str, Any]:
    if not AI_API_KEY:
        raise HTTPException(status_code=501, detail="AI API key is not configured")

    request_payload = {
        "model": AI_MODEL,
        "input": [
            {
                "role": "system",
                "content": (
                    "You summarize structured product validation feedback for a creator. "
                    "Return concise, evidence-based JSON only. Do not use markdown."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Analyze this validation dataset and return the requested JSON schema.\n\n"
                    f"{summary_input}"
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "project_ai_summary",
                "strict": True,
                "schema": AI_SUMMARY_JSON_SCHEMA,
            }
        },
    }

    try:
        async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                json=request_payload,
                headers={
                    "Authorization": f"Bearer {AI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        response.raise_for_status()
        response_payload = response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=f"AI provider request failed: {error.response.text}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI provider request timed out")
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"AI provider request failed: {error}")

    raw_text = _extract_response_text(response_payload)
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="AI provider returned invalid JSON")


def _extract_gemini_text(response_payload: dict[str, Any]) -> str:
    candidates = response_payload.get("candidates")
    if not isinstance(candidates, list):
        raise HTTPException(status_code=502, detail="Gemini returned no candidates")

    chunks: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)

    if not chunks:
        raise HTTPException(status_code=502, detail="Gemini returned an empty response")
    return "\n".join(chunks)


async def _call_gemini_summary(summary_input: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=501, detail="Gemini API key is not configured")

    prompt = (
        "You summarize structured product validation feedback for a creator. "
        "Return concise, evidence-based JSON only. Do not use markdown.\n\n"
        "Analyze this validation dataset and return the requested JSON schema.\n\n"
        f"{summary_input}"
    )
    request_payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": _schema_with_property_ordering(AI_SUMMARY_JSON_SCHEMA),
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent"

    try:
        async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json=request_payload,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY,
                },
            )
        response.raise_for_status()
        response_payload = response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=f"Gemini request failed: {error.response.text}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Gemini request timed out")
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Gemini request failed: {error}")

    raw_text = _extract_gemini_text(response_payload)
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Gemini returned invalid JSON")


async def generate_ai_summary(summary_input: str) -> dict[str, Any]:
    if AI_PROVIDER == "disabled":
        raise HTTPException(status_code=501, detail="AI provider is not configured")
    if AI_PROVIDER == "openai":
        return await _call_openai_summary(summary_input)
    if AI_PROVIDER == "gemini":
        return await _call_gemini_summary(summary_input)
    raise HTTPException(status_code=501, detail=f"Unsupported AI provider: {AI_PROVIDER}")


async def _resolve_ai_summary_result(value: Any) -> dict[str, Any]:
    if inspect.isawaitable(value):
        return await value
    return value


def _can_creator_post_project(session: Session, user: User) -> bool:
    if user.subscription in {"creator_basic", "creator_plus"}:
        return True

    if user.subscription != "free":
        return False

    existing_project_ids = session.exec(
        select(Project.id)
        .where(
            Project.creator_id == user.id,
            Project.status == "active",
        )
        .limit(FREE_CREATOR_PROJECT_QUOTA)
    ).all()
    return len(existing_project_ids) < FREE_CREATOR_PROJECT_QUOTA


def migrate_legacy_question_answer_rows(session: Session) -> None:
    has_mutation = False

    projects = session.exec(select(Project)).all()
    question_map = _build_project_questions_map(session, [project.id for project in projects if project.id is not None])

    for project in projects:
        if project.id is None:
            continue
        if question_map.get(project.id):
            continue

        for idx, question_text in enumerate(decode_legacy_list(project.questions)):
            session.add(
                ProjectQuestion(
                    project_id=project.id,
                    position=idx,
                    text=question_text,
                )
            )
            has_mutation = True

    if has_mutation:
        session.flush()
        question_map = _build_project_questions_map(session, [project.id for project in projects if project.id is not None])

    existing_answer_response_ids = set(session.exec(select(ResponseAnswer.response_id)).all())
    responses = session.exec(select(Response)).all()

    for response in responses:
        if response.id is None or response.id in existing_answer_response_ids:
            continue

        project_questions = question_map.get(response.project_id, [])
        question_ids = [row.id for row in project_questions if row.id is not None]

        for idx, answer_text in enumerate(decode_legacy_list(response.answers)):
            question_id = question_ids[idx] if idx < len(question_ids) else None
            session.add(
                ResponseAnswer(
                    response_id=response.id,
                    question_id=question_id,
                    position=idx,
                    text=answer_text,
                )
            )
            has_mutation = True

    if has_mutation:
        session.commit()


def migrate_legacy_innovation_tags(session: Session) -> None:
    has_mutation = False
    innovations = session.exec(select(Innovation).where(Innovation.tags.is_not(None))).all()

    for innovation in innovations:
        normalized = encode_innovation_tags(decode_innovation_tags(innovation.tags))
        if innovation.tags != normalized:
            innovation.tags = normalized
            session.add(innovation)
            has_mutation = True

    if has_mutation:
        session.commit()


# ====================== Auth dependencies ======================


def get_bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    return token


def get_current_user(
    token: str = Depends(get_bearer_token),
    session: Session = Depends(get_session),
) -> User:
    payload = decode_access_token(token)
    user_id = payload["sub"]
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_optional_current_user(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> Optional[User]:
    if not authorization:
        return None

    token = get_bearer_token(authorization)
    payload = decode_access_token(token)
    user_id = payload["sub"]
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_creator(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "creator":
        raise HTTPException(status_code=403, detail="Creator account required")
    return current_user


def require_tester(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "tester":
        raise HTTPException(status_code=403, detail="Tester account required")
    return current_user


# ====================== Lifespan ======================


@asynccontextmanager
async def lifespan(_: FastAPI):
    if SECRET_KEY == "dev-secret-change-me":
        if APP_ENV in {"production", "prod"}:
            raise RuntimeError("XSOLIA_SECRET_KEY must be set before starting in production.")
        warnings.warn(
            "XSOLIA_SECRET_KEY is using the default dev value. Set it before deploying.",
            stacklevel=1,
        )
    create_db_and_tables()
    with Session(engine) as session:
        migrate_legacy_question_answer_rows(session)
        migrate_legacy_innovation_tags(session)
    yield


app = FastAPI(title="xsolia API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    fields: dict[str, str] = {}

    for error in exc.errors():
        loc = error.get("loc", ())
        field = next((str(item) for item in reversed(loc) if isinstance(item, str)), "general")
        if field == "body":
            field = "general"

        message = error.get("msg") or error.get("type") or "Invalid value"
        if isinstance(message, str) and message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")

        if field not in fields:
            fields[field] = str(message)

    detail = " · ".join(fields.values()) or "Invalid request"
    return JSONResponse(status_code=422, content={"detail": detail, "fields": fields})


# ====================== Routers ======================

from routes import auth_router, health_router, innovations_router, projects_router

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(innovations_router)
app.include_router(health_router)
