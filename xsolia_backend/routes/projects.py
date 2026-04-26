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
        budget=payload.budget,
        main_category=payload.main_category,
        subcategory=payload.subcategory,
        status="active",
        visibility=payload.visibility,
        detail_level=payload.detail_level,
        allow_indexing=payload.allow_indexing,
        source_innovation_id=payload.source_innovation_id,
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


@router.post("/projects/{project_id}/respond")
def respond_to_project(
    project_id: int,
    payload: ResponseCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.status != "active":
        raise HTTPException(status_code=404, detail="Project not found or inactive")

    if not can_answer_project(project, current_user):
        if project.creator_id == current_user.id:
            raise HTTPException(status_code=403, detail="Project creators cannot respond to their own project")
        raise HTTPException(status_code=403, detail="Project is private or tester-only")

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

    if current_user.role == "tester":
        update_tester_streak(current_user)
        session.add(current_user)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
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
    responder_ids = sorted({response.user_id for response in responses})
    reputation_map = build_tester_reputation_map(session, responder_ids)
    responder_rows = session.exec(
        select(User.id, User.name).where(User.id.in_(responder_ids))
    ).all() if responder_ids else []
    responder_name_map = {int(row[0]): row[1] for row in responder_rows}

    return [
        serialize_response(
            response,
            answer_map.get(response.id, []),
            responder_name=responder_name_map.get(response.user_id),
            responder_reputation=reputation_map.get(response.user_id),
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


@router.post("/responses/{response_id}/like")
def like_response(
    response_id: int,
    current_user: User = Depends(require_tester),
    session: Session = Depends(get_session),
):
    response = session.get(Response, response_id)
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")
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

    session.add(response_like)
    session.add(response)
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
