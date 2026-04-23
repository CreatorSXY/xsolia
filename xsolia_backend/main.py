from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import field_validator, model_validator
from sqlalchemy import UniqueConstraint
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

app = FastAPI(title="xsolia API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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


class ProjectCreate(SQLModel):
    title: str
    description: str
    target_audience: str
    questions: List[str] = Field(min_length=1, max_length=8)
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
    def validate_questions(cls, value: List[str]) -> List[str]:
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
    questions: List[str]
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
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ResponseCreate(SQLModel):
    interest_level: int = Field(ge=1, le=5)
    answers: List[str] = Field(min_length=1, max_length=12)
    price_min: Optional[int] = Field(default=None, ge=0)
    price_max: Optional[int] = Field(default=None, ge=0)

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value: List[str]) -> List[str]:
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
    answers: List[str]
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    accepted_by_creator: bool
    likes_count: int
    created_at: datetime


class ProjectStats(SQLModel):
    project_id: int
    responses_count: int
    avg_interest: Optional[float]
    avg_price_min: Optional[float]
    avg_price_max: Optional[float]


class Innovation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    author_id: int = Field(index=True)
    title: str
    description: str
    tags: Optional[str] = None
    intent: str = "open"
    status: str = "active"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    upvotes: int = 0


class InnovationCreate(SQLModel):
    title: str
    description: str
    tags: Optional[List[str]] = None
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
    def validate_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
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
    tags: List[str]
    intent: str
    status: str
    created_at: datetime
    upvotes: int


# ====================== Utilities ======================


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _sign(message: str) -> str:
    digest = hmac.new(SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


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
        "sub": user.id,
        "role": user.role,
        "exp": int(time.time()) + TOKEN_EXPIRE_SECONDS,
    }
    payload_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    payload_part = _b64url_encode(payload_text.encode("utf-8"))
    signature_part = _sign(payload_part)
    return f"{payload_part}.{signature_part}"


def decode_access_token(token: str) -> dict:
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token format")

    expected_signature = _sign(payload_part)
    if not hmac.compare_digest(signature_part, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise HTTPException(status_code=401, detail="Token expired")

    sub = payload.get("sub")
    if not isinstance(sub, int):
        raise HTTPException(status_code=401, detail="Invalid token subject")

    return payload


def serialize_project(project: Project) -> ProjectOut:
    questions = project.questions.split("\n") if project.questions else []
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


def serialize_response(response: Response) -> ResponseOut:
    answers = response.answers.split("\n") if response.answers else []
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
    tags = innovation.tags.split(",") if innovation.tags else []
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


# ====================== Startup ======================


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# ====================== Auth APIs ======================


@app.post("/register", response_model=UserOut)
def register(user: UserCreate, session: Session = Depends(get_session)):
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
def login(data: UserLogin, session: Session = Depends(get_session)):
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
    if current_user.subscription not in {"creator_basic", "creator_plus"}:
        raise HTTPException(status_code=403, detail="Creator subscription required to post")

    project = Project(
        creator_id=current_user.id,
        title=payload.title,
        description=payload.description,
        target_audience=payload.target_audience,
        questions="\n".join(payload.questions),
        reward_note=payload.reward_note,
        budget=payload.budget,
        main_category=payload.main_category,
        subcategory=payload.subcategory,
        status="active",
    )

    session.add(project)
    session.commit()
    session.refresh(project)
    return serialize_project(project)


@app.get("/projects/active", response_model=list[ProjectOut])
def list_active_projects(
    main_category: Optional[str] = None,
    subcategory: Optional[str] = None,
    session: Session = Depends(get_session),
):
    stmt = select(Project).where(Project.status == "active")

    if main_category:
        stmt = stmt.where(Project.main_category == main_category)
    if subcategory:
        stmt = stmt.where(Project.subcategory == subcategory)

    projects = session.exec(stmt.order_by(Project.id.desc())).all()
    return [serialize_project(project) for project in projects]


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

    response = Response(
        project_id=project_id,
        user_id=current_user.id,
        interest_level=payload.interest_level,
        answers="\n".join(payload.answers),
        price_min=payload.price_min,
        price_max=payload.price_max,
    )

    session.add(response)
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
    ).all()
    return [serialize_response(response) for response in responses]


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
    response.accepted_at = datetime.utcnow()

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

    responses = session.exec(select(Response).where(Response.project_id == project_id)).all()
    total = len(responses)

    if total == 0:
        return ProjectStats(
            project_id=project_id,
            responses_count=0,
            avg_interest=None,
            avg_price_min=None,
            avg_price_max=None,
        )

    avg_interest = sum(response.interest_level for response in responses) / total

    price_min_values = [response.price_min for response in responses if response.price_min is not None]
    price_max_values = [response.price_max for response in responses if response.price_max is not None]

    avg_price_min = sum(price_min_values) / len(price_min_values) if price_min_values else None
    avg_price_max = sum(price_max_values) / len(price_max_values) if price_max_values else None

    return ProjectStats(
        project_id=project_id,
        responses_count=total,
        avg_interest=avg_interest,
        avg_price_min=avg_price_min,
        avg_price_max=avg_price_max,
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

    return {"summary": "AI summary feature is coming soon."}


# ====================== Innovation APIs ======================


@app.post("/innovations", response_model=InnovationOut)
def create_innovation(
    payload: InnovationCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    tags_text = ",".join(payload.tags) if payload.tags else None

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
def list_innovations(session: Session = Depends(get_session)):
    innovations = session.exec(
        select(Innovation)
        .where(Innovation.status == "active")
        .order_by(Innovation.created_at.desc())
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
