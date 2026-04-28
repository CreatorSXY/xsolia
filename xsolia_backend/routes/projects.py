import main
from fastapi import APIRouter
from main import *  # noqa: F401,F403
from main import (
    _build_project_questions_map,
    _build_response_answers_map,
    _can_creator_post_project,
    _compute_price_percentiles,
    _resolve_ai_summary_result,
    build_tester_reputation_map,
)

router = APIRouter()
_PUBLIC_GUEST_WINDOW_SECONDS = 600
_PUBLIC_GUEST_MAX_SUBMISSIONS_PER_WINDOW = 2
_PUBLIC_GUEST_ATTEMPTS: dict[str, list[float]] = {}
_PUBLIC_GUEST_ATTEMPTS_LOCK = Lock()


def _get_response_answer_texts(session: Session, response: Response) -> list[str]:
    answer_rows = session.exec(
        select(ResponseAnswer.text)
        .where(ResponseAnswer.response_id == response.id)
        .order_by(ResponseAnswer.position.asc())
    ).all()
    if answer_rows:
        return [str(value).strip() for value in answer_rows if str(value).strip()]
    return decode_legacy_list(response.answers)


def _is_guest_rate_limited(project_id: int, client_host: str) -> bool:
    key = f"{project_id}:{client_host}"
    now = time.time()
    with _PUBLIC_GUEST_ATTEMPTS_LOCK:
        attempts = [ts for ts in _PUBLIC_GUEST_ATTEMPTS.get(key, []) if now - ts <= _PUBLIC_GUEST_WINDOW_SECONDS]
        if len(attempts) >= _PUBLIC_GUEST_MAX_SUBMISSIONS_PER_WINDOW:
            _PUBLIC_GUEST_ATTEMPTS[key] = attempts
            return True
        attempts.append(now)
        _PUBLIC_GUEST_ATTEMPTS[key] = attempts
    return False


@router.post("/projects", response_model=ProjectOut)
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

    if payload.source_innovation_id is not None:
        source_innovation = session.get(Innovation, payload.source_innovation_id)
        if not source_innovation:
            raise HTTPException(status_code=404, detail="Source innovation not found")

    project = Project(
        creator_id=current_user.id,
        title=payload.title,
        description=payload.description,
        target_audience=payload.target_audience,
        questions=encode_legacy_list(payload.questions),
        image_urls=encode_legacy_list(payload.image_urls),
        reward_note=payload.reward_note,
        reward_type=payload.reward_type,
        budget=payload.budget,
        main_category=payload.main_category,
        subcategory=payload.subcategory,
        status="active",
        visibility=payload.visibility,
        detail_level=payload.detail_level,
        allow_indexing=payload.allow_indexing,
        source_innovation_id=payload.source_innovation_id,
        share_token=secrets.token_urlsafe(16),
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
    return serialize_project(project, question_rows, viewer=current_user)


@router.get("/projects/active", response_model=list[ProjectOut])
def list_active_projects(
    main_category: Optional[str] = None,
    subcategory: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=100),
    sort: str = Query(default="new"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: Optional[User] = Depends(get_optional_current_user),
    session: Session = Depends(get_session),
):
    normalized_sort = sort.strip().lower()
    if normalized_sort not in {"new", "active"}:
        raise HTTPException(status_code=422, detail="Invalid sort")

    conditions = [Project.status == "active"]
    if current_user and current_user.role in {"tester", "creator"}:
        conditions.append(Project.visibility.in_(["public", "tester_only"]))
    else:
        conditions.append(Project.visibility == "public")

    if main_category:
        conditions.append(Project.main_category == main_category)
    if subcategory:
        conditions.append(Project.subcategory == subcategory)
    if q:
        query = q.strip()
        if query:
            conditions.append(Project.title.contains(query) | Project.description.contains(query))

    if normalized_sort == "active":
        ranked_rows = session.exec(
            select(Project.id, func.count(Response.id))
            .select_from(Project)
            .join(Response, Response.project_id == Project.id, isouter=True)
            .where(*conditions)
            .group_by(Project.id)
            .order_by(func.count(Response.id).desc(), Project.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        ranked_ids = [row[0] for row in ranked_rows]
        if ranked_ids:
            project_rows = session.exec(select(Project).where(Project.id.in_(ranked_ids))).all()
            project_map = {project.id: project for project in project_rows}
            projects = [project_map[project_id] for project_id in ranked_ids if project_id in project_map]
        else:
            projects = []
    else:
        projects = session.exec(
            select(Project)
            .where(*conditions)
            .order_by(Project.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    question_map = _build_project_questions_map(
        session,
        [project.id for project in projects if project.id is not None],
    )

    return [serialize_project(project, question_map.get(project.id, []), viewer=current_user) for project in projects]


@router.get("/projects/trending", response_model=list[ProjectOut])
def list_trending_projects(
    limit: int = Query(default=10, ge=1, le=20),
    session: Session = Depends(get_session),
):
    candidates = session.exec(
        select(Project, func.count(Response.id), func.avg(Response.interest_level))
        .select_from(Project)
        .join(Response, Response.project_id == Project.id, isouter=True)
        .where(
            Project.status == "active",
            Project.visibility == "public",
        )
        .group_by(Project.id)
        .order_by(func.count(Response.id).desc(), Project.id.desc())
        .limit(50)
    ).all()
    if not candidates:
        return []

    project_ids = [row[0].id for row in candidates if row[0].id is not None]
    recent_cutoff = utc_now() - timedelta(days=7)
    recent_rows = session.exec(
        select(Response.project_id, func.count(Response.id))
        .where(
            Response.project_id.in_(project_ids),
            Response.created_at >= recent_cutoff,
        )
        .group_by(Response.project_id)
    ).all() if project_ids else []
    recent_map = {int(row[0]): int(row[1] or 0) for row in recent_rows}

    scored_rows: list[tuple[float, int, float, Project]] = []
    for project, total_count, avg_interest in candidates:
        total = int(total_count or 0)
        recent = recent_map.get(project.id, 0)
        avg_interest_value = float(avg_interest) if avg_interest is not None else 0.0
        score = (recent * 3) + total + (avg_interest_value * 10)
        scored_rows.append((score, total, avg_interest_value, project))

    scored_rows.sort(key=lambda item: (item[0], item[1], item[3].id or 0), reverse=True)
    selected_rows = scored_rows[:limit]
    selected = [row[3] for row in selected_rows]
    question_map = _build_project_questions_map(
        session,
        [project.id for project in selected if project.id is not None],
    )
    result: list[ProjectOut] = []
    for _, total_count, avg_interest_value, project in selected_rows:
        serialized = serialize_project(project, question_map.get(project.id, []), viewer=None)
        result.append(
            serialized.model_copy(
                update={
                    "responses_count": int(total_count or 0),
                    "avg_interest": float(avg_interest_value) if avg_interest_value else None,
                }
            )
        )
    return result


@router.get("/projects/mine", response_model=list[ProjectOut])
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
    return [serialize_project(project, question_map.get(project.id, []), viewer=current_user) for project in projects]


@router.patch("/projects/{project_id}/status", response_model=ProjectOut)
def update_project_status(
    project_id: int,
    payload: ProjectStatusUpdate,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can update project status")

    previous_status = project.status
    project.status = payload.status
    session.add(project)
    if previous_status != "closed" and payload.status == "closed" and payload.launched:
        create_launch_notifications(session, project)
    session.commit()
    session.refresh(project)

    question_rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project_id)
        .order_by(ProjectQuestion.position)
    ).all()
    return serialize_project(project, question_rows, viewer=current_user)


@router.post("/projects/{project_id}/early-access/{tester_id}", response_model=EarlyAccessGrantOut)
def grant_project_early_access(
    project_id: int,
    tester_id: int,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can grant early access")
    if (project.reward_type or "points") != "early_access":
        raise HTTPException(status_code=400, detail="Project reward type is not early_access")

    tester = session.get(User, tester_id)
    if not tester or tester.role != "tester":
        raise HTTPException(status_code=404, detail="Tester not found")

    grant = EarlyAccessGrant(
        project_id=project.id,
        tester_id=tester.id,
        granted_by=current_user.id,
    )
    notification_payload = {
        "project_id": project.id,
        "project_title": project.title,
    }

    session.add(grant)
    session.add(
        Notification(
            user_id=tester.id,
            type="early_access_granted",
            payload_json=json.dumps(notification_payload, ensure_ascii=False),
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing_grant = session.exec(
            select(EarlyAccessGrant).where(
                EarlyAccessGrant.project_id == project.id,
                EarlyAccessGrant.tester_id == tester.id,
            )
        ).first()
        if not existing_grant:
            raise HTTPException(status_code=400, detail="Failed to grant early access")
        return serialize_early_access_grant(existing_grant, project.title)

    session.refresh(grant)
    return serialize_early_access_grant(grant, project.title)


@router.get("/me/early-access", response_model=list[EarlyAccessGrantOut])
def list_my_early_access(
    current_user: User = Depends(require_tester),
    session: Session = Depends(get_session),
):
    grant_rows = session.exec(
        select(EarlyAccessGrant, Project.title)
        .join(Project, Project.id == EarlyAccessGrant.project_id)
        .where(EarlyAccessGrant.tester_id == current_user.id)
        .order_by(EarlyAccessGrant.granted_at.desc())
    ).all()

    return [
        serialize_early_access_grant(grant=row[0], project_title=row[1])
        for row in grant_rows
    ]


@router.get("/projects/{project_id}/top-contributors", response_model=list[TopContributorOut])
def list_project_top_contributors(
    project_id: int,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can view top contributors")

    responses = session.exec(
        select(Response)
        .where(Response.project_id == project_id)
        .order_by(
            Response.contribution_score.desc(),
            Response.likes_count.desc(),
            Response.created_at.asc(),
        )
        .limit(20)
    ).all()
    if not responses:
        return []

    tester_ids = sorted({response.user_id for response in responses if response.user_id is not None})
    tester_rows = session.exec(
        select(User.id, User.name, User.username).where(User.id.in_(tester_ids))
    ).all()
    tester_map = {int(row[0]): {"name": row[1], "username": row[2]} for row in tester_rows}

    accepted_rows = session.exec(
        select(
            Response.user_id,
            func.sum(case((Response.accepted_by_creator.is_(True), 1), else_=0)),
        )
        .where(
            Response.project_id == project_id,
            Response.user_id.in_(tester_ids),
        )
        .group_by(Response.user_id)
    ).all() if tester_ids else []
    accepted_map = {int(row[0]): int(row[1] or 0) for row in accepted_rows}
    reputation_map = build_tester_reputation_map(session, tester_ids)

    contributors: list[TopContributorOut] = []
    for response in responses:
        if response.user_id is None:
            continue
        tester_profile = tester_map.get(response.user_id)
        if not tester_profile:
            continue
        reputation = reputation_map.get(response.user_id, TesterReputationOut())
        contributors.append(
            TopContributorOut(
                user_id=response.user_id,
                name=tester_profile["name"],
                username=tester_profile["username"],
                reliability_score=reputation.reliability_score,
                contribution_score=response.contribution_score,
                accepted_count=accepted_map.get(response.user_id, 0),
            )
        )
    return contributors


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    current_user: Optional[User] = Depends(get_optional_current_user),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != "active" and (not current_user or current_user.id != project.creator_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project(project, current_user):
        if project.visibility == "tester_only" and current_user is None:
            raise HTTPException(status_code=401, detail="Login required to view this project")
        raise HTTPException(status_code=403, detail="Project is private")

    question_rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project_id)
        .order_by(ProjectQuestion.position)
    ).all()
    return serialize_project(project, question_rows, viewer=current_user)


@router.get("/projects/by-token/{share_token}", response_model=ProjectOut)
def get_project_by_share_token(
    share_token: str,
    current_user: Optional[User] = Depends(get_optional_current_user),
    session: Session = Depends(get_session),
):
    cleaned_token = share_token.strip()
    if not cleaned_token:
        raise HTTPException(status_code=404, detail="Project not found")

    project = session.exec(
        select(Project).where(Project.share_token == cleaned_token)
    ).first()
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found")

    question_rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project.id)
        .order_by(ProjectQuestion.position)
    ).all()
    return serialize_project(project, question_rows, viewer=current_user)


@router.get("/public/projects/{project_id}", response_model=ProjectOut)
def get_public_project(
    project_id: int,
    source_ref: Optional[int] = Query(default=None),
    utm_source: Optional[str] = Query(default=None, max_length=64),
    current_user: Optional[User] = Depends(get_optional_current_user),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_view_project_public_page(project, current_user):
        raise HTTPException(status_code=404, detail="Project not found")

    project.external_views = int(project.external_views or 0) + 1
    session.add(project)
    session.commit()
    session.refresh(project)

    question_rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project.id)
        .order_by(ProjectQuestion.position)
    ).all()
    return serialize_project(project, question_rows, viewer=current_user)


@router.post("/public/projects/{project_id}/responses")
def respond_to_project_public(
    project_id: int,
    payload: PublicResponseCreate,
    request: Request,
    source_ref: Optional[int] = Query(default=None),
    utm_source: Optional[str] = Query(default=None, max_length=64),
    current_user: Optional[User] = Depends(get_optional_current_user),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found or inactive")
    if not can_view_project_public_page(project, current_user):
        raise HTTPException(status_code=404, detail="Project not found")

    visibility = (project.visibility or "public").strip().lower()
    if visibility == "tester_only" and current_user is None:
        raise HTTPException(status_code=401, detail="Login required to submit answers")

    responder_user_id: Optional[int] = None
    is_guest = current_user is None
    if current_user is None:
        client_host = request.client.host if request.client and request.client.host else "unknown"
        if _is_guest_rate_limited(project_id, client_host):
            raise HTTPException(status_code=429, detail="Too many submissions from this IP, please try later")
        if payload.guest_email:
            duplicate_by_email = session.exec(
                select(Response.id).where(
                    Response.project_id == project_id,
                    Response.guest_email == payload.guest_email,
                )
            ).first()
            if duplicate_by_email:
                raise HTTPException(status_code=400, detail="A response with this email already exists")
    else:
        if not can_answer_project(project, current_user):
            if project.creator_id == current_user.id:
                raise HTTPException(status_code=403, detail="Project creators cannot respond to their own project")
            raise HTTPException(status_code=403, detail="Project is private or tester-only")
        responder_user_id = current_user.id
        existing = session.exec(
            select(Response.id).where(
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

    normalized_utm_source = (utm_source or "").strip().lower() or None
    response = Response(
        project_id=project_id,
        user_id=responder_user_id,
        interest_level=payload.interest_level,
        answers=encode_legacy_list(payload.answers),
        price_min=payload.price_min,
        price_max=payload.price_max,
        contribution_score=compute_contribution_score(
            answer_texts=payload.answers,
            accepted=False,
            likes_count=0,
        ),
        is_guest=is_guest,
        guest_email=payload.guest_email if is_guest else None,
        guest_name=payload.guest_name if is_guest else None,
        source_ref=source_ref,
        utm_source=normalized_utm_source,
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

    if current_user and current_user.role == "tester":
        update_tester_streak(current_user)
        session.add(current_user)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Failed to submit response")

    session.refresh(response)
    return {"ok": True, "response_id": response.id, "is_guest": is_guest}


@router.get("/projects/{project_id}/share-metrics", response_model=ShareMetricsOut)
def get_project_share_metrics(
    project_id: int,
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the project creator can view share metrics")

    external_responses = session.exec(
        select(func.count(Response.id)).where(
            Response.project_id == project_id,
            Response.is_guest.is_(True),
        )
    ).one()
    return ShareMetricsOut(
        project_id=project_id,
        external_views=int(project.external_views or 0),
        external_responses=int(external_responses or 0),
    )


@router.post("/projects/{project_id}/respond")
def respond_to_project(
    project_id: int,
    payload: ResponseCreate,
    share_token: Optional[str] = Query(default=None),
    current_user: Optional[User] = Depends(get_optional_current_user),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found or inactive")

    responder_user_id: Optional[int] = None
    guest_mode = current_user is None
    if current_user is None:
        cleaned_token = (share_token or "").strip()
        if not cleaned_token:
            raise HTTPException(status_code=403, detail="Login required or use a share link")
        token_project_id = session.exec(
            select(Project.id).where(Project.share_token == cleaned_token)
        ).first()
        if token_project_id != project_id:
            raise HTTPException(status_code=403, detail="Invalid share link")
    else:
        if not can_answer_project(project, current_user):
            if project.creator_id == current_user.id:
                raise HTTPException(status_code=403, detail="Project creators cannot respond to their own project")
            raise HTTPException(status_code=403, detail="Project is private or tester-only")
        responder_user_id = current_user.id

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
        user_id=responder_user_id,
        interest_level=payload.interest_level,
        answers=encode_legacy_list(payload.answers),
        price_min=payload.price_min,
        price_max=payload.price_max,
        contribution_score=compute_contribution_score(
            answer_texts=payload.answers,
            accepted=False,
            likes_count=0,
        ),
        is_guest=guest_mode,
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

    if current_user and current_user.role == "tester":
        update_tester_streak(current_user)
        session.add(current_user)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if guest_mode:
            raise HTTPException(status_code=400, detail="Failed to save guest response")
        raise HTTPException(status_code=400, detail="User already responded to this project")

    session.refresh(response)
    return {"ok": True, "response_id": response.id}


@router.get("/projects/{project_id}/responses", response_model=list[ResponseOut])
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
    responder_ids = sorted({response.user_id for response in responses if response.user_id is not None})
    reputation_map = build_tester_reputation_map(session, responder_ids)
    responder_rows = session.exec(
        select(User.id, User.name).where(User.id.in_(responder_ids))
    ).all() if responder_ids else []
    responder_name_map = {int(row[0]): row[1] for row in responder_rows}

    return [
        serialize_response(
            response,
            answer_map.get(response.id, []),
            responder_name=responder_name_map.get(response.user_id) if response.user_id is not None else None,
            responder_reputation=reputation_map.get(response.user_id) if response.user_id is not None else None,
        )
        for response in responses
    ]


@router.post("/responses/{response_id}/accept")
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
    response.contribution_score = compute_contribution_score(
        answer_texts=_get_response_answer_texts(session, response),
        accepted=True,
        likes_count=response.likes_count,
    )

    responder = session.get(User, response.user_id)
    if responder:
        responder.points += 10
        session.add(
            Notification(
                user_id=response.user_id,
                type="response_accepted",
                payload_json=json.dumps(
                    {
                        "project_id": project.id,
                        "project_title": project.title,
                        "response_id": response.id,
                    },
                    ensure_ascii=False,
                ),
            )
        )
    current_user.points += 2

    session.add(response)
    session.add(current_user)
    if responder:
        session.add(responder)

    session.commit()
    session.refresh(response)

    return {"ok": True, "response_id": response.id}


@router.post("/responses/{response_id}/like")
def like_response(
    response_id: int,
    current_user: User = Depends(require_tester),
    session: Session = Depends(get_session),
):
    response = session.get(Response, response_id)
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")
    project = session.get(Project, response.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if response.user_id == current_user.id:
        raise HTTPException(status_code=403, detail="Users cannot like their own response")

    existing = session.exec(
        select(ResponseLike).where(
            ResponseLike.response_id == response_id,
            ResponseLike.user_id == current_user.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already liked this response")

    response_like = ResponseLike(response_id=response_id, user_id=current_user.id)
    response.likes_count += 1
    response.contribution_score = compute_contribution_score(
        answer_texts=_get_response_answer_texts(session, response),
        accepted=response.accepted_by_creator,
        likes_count=response.likes_count,
    )

    session.add(response_like)
    session.add(response)
    if response.user_id and response.user_id != current_user.id:
        session.add(
            Notification(
                user_id=response.user_id,
                type="response_liked",
                payload_json=json.dumps(
                    {
                        "project_id": project.id,
                        "project_title": project.title,
                        "response_id": response.id,
                    },
                    ensure_ascii=False,
                ),
            )
        )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="User already liked this response")

    session.refresh(response)
    return {"ok": True, "response_id": response.id, "likes_count": response.likes_count}


@router.post("/responses/{response_id}/comments", response_model=ResponseCommentOut)
def create_response_comment(
    response_id: int,
    payload: ResponseCommentCreate,
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
        raise HTTPException(status_code=403, detail="Only the project creator can comment on this response")

    comment = ResponseComment(
        response_id=response_id,
        author_id=current_user.id,
        text=payload.text,
    )
    session.add(comment)
    session.commit()
    session.refresh(comment)
    return serialize_response_comment(comment, author_name=current_user.name)


@router.get("/responses/{response_id}/comments", response_model=list[ResponseCommentOut])
def list_response_comments(
    response_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    response = session.get(Response, response_id)
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")

    project = session.get(Project, response.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user.id not in {project.creator_id, response.user_id}:
        raise HTTPException(status_code=403, detail="Only the project creator or response author can view comments")

    comments = session.exec(
        select(ResponseComment)
        .where(ResponseComment.response_id == response_id)
        .order_by(ResponseComment.created_at.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    author_ids = sorted({comment.author_id for comment in comments})
    author_map: dict[int, str] = {}
    if author_ids:
        author_rows = session.exec(
            select(User.id, User.name).where(User.id.in_(author_ids))
        ).all()
        author_map = {row[0]: row[1] for row in author_rows}
    return [
        serialize_response_comment(comment, author_name=author_map.get(comment.author_id))
        for comment in comments
    ]


@router.get("/projects/{project_id}/stats", response_model=ProjectStats)
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
        acceptance_rate = 0.0
        validation_score = compute_validation_score(
            responses_count=0,
            avg_interest=None,
            acceptance_rate=acceptance_rate,
            avg_price_min=None,
        )
        return ProjectStats(
            project_id=project_id,
            responses_count=0,
            interest_distribution=interest_distribution,
            avg_interest=None,
            interest_stddev=None,
            avg_price_min=None,
            avg_price_max=None,
            price_percentiles=None,
            acceptance_rate=acceptance_rate,
            validation_score=validation_score,
            decision_suggestion=compute_decision_suggestion(validation_score, None),
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

    acceptance_rate = (accepted_count / total) if total > 0 else 0.0
    avg_price_min_value = float(avg_price_min) if avg_price_min is not None else None
    validation_score = compute_validation_score(
        responses_count=total,
        avg_interest=avg_interest,
        acceptance_rate=acceptance_rate,
        avg_price_min=avg_price_min_value,
    )

    return ProjectStats(
        project_id=project_id,
        responses_count=total,
        interest_distribution=interest_distribution,
        avg_interest=avg_interest,
        interest_stddev=interest_stddev,
        avg_price_min=avg_price_min_value,
        avg_price_max=float(avg_price_max) if avg_price_max is not None else None,
        price_percentiles=_compute_price_percentiles(price_rows),
        acceptance_rate=acceptance_rate,
        validation_score=validation_score,
        decision_suggestion=compute_decision_suggestion(validation_score, avg_interest),
    )


@router.get("/projects/{project_id}/ai-summary", response_model=ProjectAISummaryOut)
async def ai_summary(
    project_id: int,
    refresh: bool = False,
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

    question_rows = session.exec(
        select(ProjectQuestion)
        .where(ProjectQuestion.project_id == project_id)
        .order_by(ProjectQuestion.position)
    ).all()
    responses = session.exec(
        select(Response)
        .where(Response.project_id == project_id)
        .order_by(Response.created_at.asc())
    ).all()
    answer_map = _build_response_answers_map(
        session,
        [response.id for response in responses if response.id is not None],
    )

    summary_input = build_ai_summary_input(project, question_rows, responses, answer_map)
    input_hash = hashlib.sha256(summary_input.encode("utf-8")).hexdigest()

    cached_summary = session.exec(
        select(ProjectAISummary)
        .where(
            ProjectAISummary.project_id == project_id,
            ProjectAISummary.input_hash == input_hash,
        )
        .order_by(ProjectAISummary.created_at.desc())
    ).first()
    if cached_summary and not refresh:
        return serialize_project_ai_summary(
            cached_summary,
            responses_count=len(responses),
            cached=True,
        )

    summary_payload = await _resolve_ai_summary_result(main.generate_ai_summary(summary_input))
    summary = ProjectAISummary(
        project_id=project_id,
        model=AI_MODEL,
        input_hash=input_hash,
        summary_json=json.dumps(summary_payload, ensure_ascii=False),
    )
    session.add(summary)
    session.commit()
    session.refresh(summary)

    return serialize_project_ai_summary(
        summary,
        responses_count=len(responses),
        cached=False,
    )
