from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import sqrt
from threading import Lock
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from pydantic import field_validator, model_validator
from sqlalchemy import UniqueConstraint, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel, Session, create_engine, select

# ========= Database =========
DATABASE_URL = (
    os.getenv("XSOLIA_DATABASE_URL")
    or os.getenv("KROTKA_DATABASE_URL")
    or "sqlite:///./database.db"
)
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

# ========= Security constants =========
ROLES = {"creator", "tester"}
SUBSCRIPTIONS = {"free", "creator_basic", "creator_plus"}
INNOVATION_INTENTS = {"open", "looking_for_team", "just_idea"}

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


class UserOut(SQLModel):
    id: int
    email: str
    name: str
    role: str
    subscription: str
    points: int


class LoginOut(SQLModel):
    user_id: int
    role: str
    name: str
    subscription: str
    access_token: str
    token_type: str
    expires_in: int


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    creator_id: int = Field(index=True)
    title: str
    description: str
    target_audience: str
    questions: str
    reward_note: Optional[str] = None
    budget: int
    main_category: str = "testing"
    subcategory: Optional[str] = None
    status: str = "active"


class ProjectQuestion(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("project_id", "position", name="uq_project_question_position"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    position: int = Field(ge=0)
    text: str


class ProjectCreate(SQLModel):
    title: str
    description: str
    target_audience: str
    questions: list[str] = Field(min_length=1, max_length=8)
    reward_note: Optional[str] = None
    budget: int = Field(ge=0, le=1_000_000_000)
    main_category: str = "testing"
    subcategory: Optional[str] = None

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


class ProjectOut(SQLModel):
    id: int
    creator_id: int
    title: str
    description: str
    target_audience: str
    questions: list[str]
    reward_note: Optional[str]
    budget: int
    main_category: str
    subcategory: Optional[str]
    status: str


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


class ResponseOut(SQLModel):
    id: int
    project_id: int
    user_id: int
    interest_level: int
    answers: list[str]
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    accepted_by_creator: bool
    likes_count: int
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


# ====================== Utilities ======================


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


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


def serialize_project(project: Project, question_rows: Optional[list[ProjectQuestion]] = None) -> ProjectOut:
    if question_rows is None:
        questions = decode_legacy_list(project.questions)
    else:
        questions = [row.text for row in question_rows]
        if not questions:
            questions = decode_legacy_list(project.questions)

    return ProjectOut(
        id=project.id,
        creator_id=project.creator_id,
        title=project.title,
        description=project.description,
        target_audience=project.target_audience,
        questions=questions,
        reward_note=project.reward_note,
        budget=project.budget,
        main_category=project.main_category,
        subcategory=project.subcategory,
        status=project.status,
    )


def serialize_response(response: Response, answer_rows: Optional[list[ResponseAnswer]] = None) -> ResponseOut:
    if answer_rows is None:
        answers = decode_legacy_list(response.answers)
    else:
        answers = [row.text for row in answer_rows]
        if not answers:
            answers = decode_legacy_list(response.answers)

    return ResponseOut(
        id=response.id,
        project_id=response.project_id,
        user_id=response.user_id,
        interest_level=response.interest_level,
        answers=answers,
        price_min=response.price_min,
        price_max=response.price_max,
        accepted_by_creator=response.accepted_by_creator,
        likes_count=response.likes_count,
        created_at=response.created_at,
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


def require_creator(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "creator":
        raise HTTPException(status_code=403, detail="Creator account required")
    return current_user


# ====================== Lifespan ======================


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db_and_tables()
    with Session(engine) as session:
        migrate_legacy_question_answer_rows(session)
        migrate_legacy_innovation_tags(session)
    yield


app = FastAPI(title="xsolia API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ====================== Auth APIs ======================


@app.post("/register", response_model=UserOut)
def register(
    user: UserCreate,
    request: Request,
    session: Session = Depends(get_session),
):
    _enforce_auth_rate_limit(request, user.email)

    existing = session.exec(select(User).where(User.email == user.email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    db_user = User(
        email=user.email,
        name=user.name,
        password_hash=hash_password(user.password),
        role=user.role,
        subscription=user.subscription,
    )
    session.add(db_user)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Email already registered")

    session.refresh(db_user)
    return UserOut.model_validate(db_user)


@app.post("/login", response_model=LoginOut)
def login(
    data: UserLogin,
    request: Request,
    session: Session = Depends(get_session),
):
    _enforce_auth_rate_limit(request, data.email)

    user = session.exec(select(User).where(User.email == data.email)).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Invalid email or password")

    # Opportunistic password migration for old sha256 hashes.
    if "$" not in user.password_hash:
        user.password_hash = hash_password(data.password)
        session.add(user)
        session.commit()

    access_token = create_access_token(user)
    return LoginOut(
        user_id=user.id,
        role=user.role,
        name=user.name,
        subscription=user.subscription,
        access_token=access_token,
        token_type="bearer",
        expires_in=TOKEN_EXPIRE_SECONDS,
    )


@app.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return UserOut.model_validate(current_user)


# ====================== Project APIs ======================


@app.post("/projects", response_model=ProjectOut)
def create_project(
    payload: ProjectCreate,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    if not _can_creator_post_project(session, current_user):
        if current_user.subscription == "free":
            raise HTTPException(
                status_code=403,
                detail=(
                    "Free creator quota used. Upgrade to creator_basic or creator_plus "
                    "to publish more projects"
                ),
            )
        raise HTTPException(status_code=403, detail="Creator subscription required to post")

    project = Project(
        creator_id=current_user.id,
        title=payload.title,
        description=payload.description,
        target_audience=payload.target_audience,
        questions=encode_legacy_list(payload.questions),
        reward_note=payload.reward_note,
        budget=payload.budget,
        main_category=payload.main_category,
        subcategory=payload.subcategory,
        status="active",
    )

    session.add(project)
    session.flush()

    question_rows = [
        ProjectQuestion(project_id=project.id, position=idx, text=question)
        for idx, question in enumerate(payload.questions)
    ]
    for row in question_rows:
        session.add(row)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Failed to create project")

    session.refresh(project)
    return serialize_project(project, question_rows)


@app.get("/projects/active", response_model=list[ProjectOut])
def list_active_projects(
    main_category: Optional[str] = None,
    subcategory: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    stmt = select(Project).where(Project.status == "active")

    if main_category:
        stmt = stmt.where(Project.main_category == main_category)
    if subcategory:
        stmt = stmt.where(Project.subcategory == subcategory)

    projects = session.exec(stmt.order_by(Project.id.desc()).offset(offset).limit(limit)).all()
    question_map = _build_project_questions_map(
        session,
        [project.id for project in projects if project.id is not None],
    )

    return [serialize_project(project, question_map.get(project.id, [])) for project in projects]


@app.get("/projects/mine", response_model=list[ProjectOut])
def list_my_projects(
    status: Optional[str] = "active",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    stmt = select(Project).where(Project.creator_id == current_user.id)
    if status:
        stmt = stmt.where(Project.status == status)

    projects = session.exec(stmt.order_by(Project.id.desc()).offset(offset).limit(limit)).all()
    question_map = _build_project_questions_map(
        session,
        [project.id for project in projects if project.id is not None],
    )
    return [serialize_project(project, question_map.get(project.id, [])) for project in projects]


@app.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found")

    question_rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project_id)
        .order_by(ProjectQuestion.position)
    ).all()
    return serialize_project(project, question_rows)


@app.post("/projects/{project_id}/respond")
def respond_to_project(
    project_id: int,
    payload: ResponseCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found or inactive")

    if project.creator_id == current_user.id:
        raise HTTPException(status_code=403, detail="Project creators cannot respond to their own project")

    existing = session.exec(
        select(Response).where(
            Response.project_id == project_id,
            Response.user_id == current_user.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already responded to this project")

    project_questions = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project_id)
        .order_by(ProjectQuestion.position)
    ).all()

    if project_questions and len(payload.answers) != len(project_questions):
        raise HTTPException(
            status_code=422,
            detail="answers length must match the number of project questions",
        )

    response = Response(
        project_id=project_id,
        user_id=current_user.id,
        interest_level=payload.interest_level,
        answers=encode_legacy_list(payload.answers),
        price_min=payload.price_min,
        price_max=payload.price_max,
    )

    session.add(response)
    session.flush()

    for idx, answer in enumerate(payload.answers):
        question_id = project_questions[idx].id if idx < len(project_questions) else None
        session.add(
            ResponseAnswer(
                response_id=response.id,
                question_id=question_id,
                position=idx,
                text=answer,
            )
        )

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="User already responded to this project")

    session.refresh(response)
    return {"ok": True, "response_id": response.id}


@app.get("/projects/{project_id}/responses", response_model=list[ResponseOut])
def list_project_responses(
    project_id: int,
    current_user: User = Depends(require_creator),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can view responses")

    responses = session.exec(
        select(Response)
        .where(Response.project_id == project_id)
        .order_by(Response.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    answer_map = _build_response_answers_map(
        session,
        [response.id for response in responses if response.id is not None],
    )
    return [serialize_response(response, answer_map.get(response.id, [])) for response in responses]


@app.post("/responses/{response_id}/accept")
def accept_response(
    response_id: int,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    response = session.get(Response, response_id)
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")

    project = session.get(Project, response.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can accept a response")

    if response.accepted_by_creator:
        raise HTTPException(status_code=400, detail="Response already accepted")

    response.accepted_by_creator = True
    response.accepted_at = utc_now()

    responder = session.get(User, response.user_id)
    if responder:
        responder.points += 10
    current_user.points += 2

    session.add(response)
    session.add(current_user)
    if responder:
        session.add(responder)

    session.commit()
    session.refresh(response)

    return {"ok": True, "response_id": response.id}


@app.get("/projects/{project_id}/stats", response_model=ProjectStats)
def project_stats(
    project_id: int,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can view project stats")

    total = session.exec(
        select(func.count(Response.id)).where(Response.project_id == project_id)
    ).one()
    total = int(total or 0)
    interest_distribution = {score: 0 for score in range(1, 6)}

    if total == 0:
        return ProjectStats(
            project_id=project_id,
            responses_count=0,
            interest_distribution=interest_distribution,
            avg_interest=None,
            interest_stddev=None,
            avg_price_min=None,
            avg_price_max=None,
            price_percentiles=None,
            acceptance_rate=0.0,
        )

    interest_rows = session.exec(
        select(Response.interest_level, func.count(Response.id))
        .where(Response.project_id == project_id)
        .group_by(Response.interest_level)
    ).all()
    for level, count in interest_rows:
        if level in interest_distribution:
            interest_distribution[level] = int(count)

    sum_interest = session.exec(
        select(func.sum(Response.interest_level)).where(Response.project_id == project_id)
    ).one()
    sum_interest_sq = session.exec(
        select(func.sum(Response.interest_level * Response.interest_level)).where(Response.project_id == project_id)
    ).one()

    avg_interest = float(sum_interest) / total
    variance = max((float(sum_interest_sq) / total) - (avg_interest * avg_interest), 0.0)
    interest_stddev = sqrt(variance)

    avg_price_min = session.exec(
        select(func.avg(Response.price_min)).where(
            Response.project_id == project_id,
            Response.price_min.is_not(None),
        )
    ).one()
    avg_price_max = session.exec(
        select(func.avg(Response.price_max)).where(
            Response.project_id == project_id,
            Response.price_max.is_not(None),
        )
    ).one()

    accepted_count = session.exec(
        select(func.count(Response.id)).where(
            Response.project_id == project_id,
            Response.accepted_by_creator.is_(True),
        )
    ).one()
    accepted_count = int(accepted_count or 0)

    price_rows = session.exec(
        select(Response.price_min, Response.price_max).where(
            Response.project_id == project_id,
            (Response.price_min.is_not(None)) | (Response.price_max.is_not(None)),
        )
    ).all()

    return ProjectStats(
        project_id=project_id,
        responses_count=total,
        interest_distribution=interest_distribution,
        avg_interest=avg_interest,
        interest_stddev=interest_stddev,
        avg_price_min=float(avg_price_min) if avg_price_min is not None else None,
        avg_price_max=float(avg_price_max) if avg_price_max is not None else None,
        price_percentiles=_compute_price_percentiles(price_rows),
        acceptance_rate=accepted_count / total,
    )


@app.get("/projects/{project_id}/ai-summary")
def ai_summary(
    project_id: int,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can access AI summary")
    if current_user.subscription != "creator_plus":
        raise HTTPException(status_code=403, detail="Plus subscription required")

    raise HTTPException(status_code=501, detail="AI summary is not available yet")


# ====================== Innovation APIs ======================


@app.post("/innovations", response_model=InnovationOut)
def create_innovation(
    payload: InnovationCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    tags_text = encode_innovation_tags(payload.tags)

    innovation = Innovation(
        author_id=current_user.id,
        title=payload.title,
        description=payload.description,
        tags=tags_text,
        intent=payload.intent,
        status="active",
    )
    session.add(innovation)
    session.commit()
    session.refresh(innovation)

    return serialize_innovation(innovation)


@app.get("/innovations", response_model=list[InnovationOut])
def list_innovations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    innovations = session.exec(
        select(Innovation)
        .where(Innovation.status == "active")
        .order_by(Innovation.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [serialize_innovation(innovation) for innovation in innovations]


@app.get("/innovations/{innovation_id}", response_model=InnovationOut)
def get_innovation(innovation_id: int, session: Session = Depends(get_session)):
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")
    return serialize_innovation(innovation)


# ====================== Health ======================


@app.get("/health")
def health():
    return {"status": "ok"}
