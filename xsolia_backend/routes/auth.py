from fastapi import APIRouter
from main import *  # noqa: F401,F403
from main import (
    CREATOR_DECISION_NEXT_STEP,
    _build_project_questions_map,
    _build_response_answers_map,
    _enforce_auth_rate_limit,
    build_tester_reputation_map,
    compute_creator_decision_stage,
    serialize_user_out,
)

router = APIRouter()

@router.post("/register", response_model=UserOut)
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


@router.post("/login", response_model=LoginOut)
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
        username=user.username,
        avatar_url=user.avatar_url,
        access_token=access_token,
        token_type="bearer",
        expires_in=TOKEN_EXPIRE_SECONDS,
    )


@router.get("/me", response_model=UserOut)
def me(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return serialize_user_out(current_user, session)


@router.patch("/me/avatar", response_model=UserOut)
def update_avatar(
    payload: AvatarUpdate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    current_user.avatar_url = payload.avatar_url
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return serialize_user_out(current_user, session)


@router.patch("/me/username", response_model=UserOut)
def update_username(
    payload: UsernameUpdate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    existing = session.exec(
        select(User).where(
            User.username == payload.username,
            User.id != current_user.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username is already taken")

    current_user.username = payload.username
    session.add(current_user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Username is already taken")

    session.refresh(current_user)
    return serialize_user_out(current_user, session)


@router.get("/users/{username}/public", response_model=UserPublicOut)
def get_public_user_profile(username: str, session: Session = Depends(get_session)):
    normalized_username = username.strip().lower()
    user = session.exec(select(User).where(User.username == normalized_username)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Public profile not found")

    projects_count = 0
    total_responses = 0
    avg_interest = None
    top_project = None
    top_category = None
    tester_summary = TesterReputationOut()

    if user.role == "creator":
        projects_count = session.exec(
            select(func.count(Project.id)).where(
                Project.creator_id == user.id,
                Project.status == "active",
            )
        ).one()
        projects_count = int(projects_count or 0)

        response_stats = session.exec(
            select(func.count(Response.id), func.avg(Response.interest_level))
            .select_from(Project)
            .join(Response, Response.project_id == Project.id, isouter=True)
            .where(
                Project.creator_id == user.id,
                Project.status == "active",
            )
        ).one()
        total_responses = int(response_stats[0] or 0)
        avg_interest = float(response_stats[1]) if response_stats[1] is not None else None

        top_project_row = session.exec(
            select(Project.title, func.count(Response.id), func.avg(Response.interest_level))
            .select_from(Project)
            .join(Response, Response.project_id == Project.id, isouter=True)
            .where(
                Project.creator_id == user.id,
                Project.status == "active",
            )
            .group_by(Project.id, Project.title)
            .order_by(func.count(Response.id).desc(), func.avg(Response.interest_level).desc())
            .limit(1)
        ).first()
        if top_project_row:
            top_project = PublicTopProject(
                title=top_project_row[0],
                responses_count=int(top_project_row[1] or 0),
                avg_interest=float(top_project_row[2]) if top_project_row[2] is not None else None,
            )
    else:
        tester_summary = build_tester_reputation_map(session, [user.id]).get(user.id, TesterReputationOut())
        total_responses = tester_summary.responses_count
        avg_interest = tester_summary.avg_interest_given if tester_summary.responses_count else None
        top_category = tester_summary.best_categories[0] if tester_summary.best_categories else None

    return UserPublicOut(
        username=user.username,
        name=user.name,
        role=user.role,
        projects_count=projects_count,
        total_responses=total_responses,
        avg_interest=avg_interest,
        top_project=top_project,
        top_category=top_category,
        points=user.points,
        streak_current=user.streak_current,
        streak_best=user.streak_best,
        responses_count=tester_summary.responses_count,
        accepted_responses_count=tester_summary.accepted_responses_count,
        acceptance_rate=tester_summary.acceptance_rate,
        avg_interest_given=tester_summary.avg_interest_given,
        best_categories=tester_summary.best_categories,
        reliability_score=tester_summary.reliability_score,
    )


@router.get("/me/responses", response_model=list[ResponseOut])
def list_my_responses(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    responses = session.exec(
        select(Response)
        .where(Response.user_id == current_user.id)
        .order_by(Response.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    answer_map = _build_response_answers_map(
        session,
        [response.id for response in responses if response.id is not None],
    )
    project_ids = sorted({response.project_id for response in responses})
    projects = session.exec(select(Project).where(Project.id.in_(project_ids))).all() if project_ids else []
    project_title_map = {project.id: project.title for project in projects}
    return [
        serialize_response(
            response,
            answer_map.get(response.id, []),
            project_title=project_title_map.get(response.project_id),
        )
        for response in responses
    ]


@router.get("/me/responded-project-ids", response_model=list[int])
def list_my_responded_project_ids(
    current_user: User = Depends(require_tester),
    session: Session = Depends(get_session),
):
    project_ids = session.exec(
        select(Response.project_id)
        .where(Response.user_id == current_user.id)
        .order_by(Response.created_at.desc())
    ).all()
    seen: set[int] = set()
    ordered_unique: list[int] = []
    for project_id in project_ids:
        if project_id in seen:
            continue
        seen.add(project_id)
        ordered_unique.append(project_id)
    return ordered_unique


@router.get("/me/creator-dashboard", response_model=CreatorDashboardOut)
def get_creator_dashboard(
    current_user: User = Depends(require_creator),
    session: Session = Depends(get_session),
):
    projects = session.exec(
        select(Project)
        .where(Project.creator_id == current_user.id)
        .order_by(Project.id.desc())
    ).all()
    if not projects:
        return CreatorDashboardOut(
            summary=CreatorDashboardSummaryOut(),
            projects=[],
        )

    project_ids = [project.id for project in projects if project.id is not None]
    stats_rows = session.exec(
        select(
            Response.project_id,
            func.count(Response.id),
            func.avg(Response.interest_level),
            func.sum(case((Response.accepted_by_creator.is_(True), 1), else_=0)),
            func.avg(Response.price_min),
            func.avg(Response.price_max),
        )
        .where(Response.project_id.in_(project_ids))
        .group_by(Response.project_id)
    ).all()
    stats_map: dict[int, dict[str, Any]] = {}
    for row in stats_rows:
        stats_map[int(row[0])] = {
            "responses_count": int(row[1] or 0),
            "avg_interest": float(row[2]) if row[2] is not None else None,
            "accepted_count": int(row[3] or 0),
            "avg_price_min": float(row[4]) if row[4] is not None else None,
            "avg_price_max": float(row[5]) if row[5] is not None else None,
        }

    responses = session.exec(
        select(Response)
        .where(Response.project_id.in_(project_ids))
        .order_by(
            Response.project_id.asc(),
            Response.interest_level.desc(),
            Response.likes_count.desc(),
            Response.created_at.asc(),
        )
    ).all()
    top_response_by_project: dict[int, Response] = {}
    for response in responses:
        top_response_by_project.setdefault(response.project_id, response)
    answer_map = _build_response_answers_map(
        session,
        [response.id for response in top_response_by_project.values() if response.id is not None],
    )
    top_signal_map: dict[int, str] = {}
    for project_id, response in top_response_by_project.items():
        answers = [row for row in answer_map.get(response.id, []) if row.text and row.text.strip()]
        best_answer = max(answers, key=lambda a: len(a.text), default=None)
        if best_answer:
            top_signal_map[project_id] = best_answer.text

    total_responses = int(sum(item.get("responses_count", 0) for item in stats_map.values()))
    avg_interest_overall = session.exec(
        select(func.avg(Response.interest_level))
        .select_from(Project)
        .join(Response, Response.project_id == Project.id, isouter=True)
        .where(Project.creator_id == current_user.id)
    ).one()
    active_projects_count = int(sum(1 for project in projects if project.status == "active"))

    dashboard_projects: list[CreatorDashboardProjectOut] = []
    projects_needing_decision = 0
    for project in projects:
        stat = stats_map.get(project.id, {})
        responses_count = int(stat.get("responses_count", 0))
        avg_interest = stat.get("avg_interest")
        accepted_count = int(stat.get("accepted_count", 0))
        acceptance_rate = (accepted_count / responses_count) if responses_count else 0.0
        decision_stage = compute_creator_decision_stage(responses_count, avg_interest)
        if decision_stage in {"enough_signal", "weak_signal", "decision_needed"}:
            projects_needing_decision += 1

        dashboard_projects.append(
            CreatorDashboardProjectOut(
                id=project.id,
                title=project.title,
                status=project.status,
                visibility=project.visibility or "public",
                responses_count=responses_count,
                avg_interest=avg_interest,
                acceptance_rate=acceptance_rate,
                avg_price_min=stat.get("avg_price_min"),
                avg_price_max=stat.get("avg_price_max"),
                decision_stage=decision_stage,
                top_signal=top_signal_map.get(project.id, "Collect more high-quality responses to surface a clear signal."),
                next_step=CREATOR_DECISION_NEXT_STEP[decision_stage],
                source_innovation_id=project.source_innovation_id,
            )
        )

    return CreatorDashboardOut(
        summary=CreatorDashboardSummaryOut(
            total_projects=len(projects),
            active_projects=active_projects_count,
            total_responses=total_responses,
            avg_interest_overall=float(avg_interest_overall) if avg_interest_overall is not None else 0.0,
            projects_needing_decision=projects_needing_decision,
        ),
        projects=dashboard_projects,
    )


@router.get("/leaderboard/testers", response_model=list[TesterLeaderboardEntryOut])
def list_tester_leaderboard(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
):
    testers = session.exec(
        select(User).where(
            User.role == "tester",
            User.username.is_not(None),
            User.username != "",
        )
    ).all()
    if not testers:
        return []

    reputation_map = build_tester_reputation_map(session, [tester.id for tester in testers])
    ranked_rows: list[tuple[User, TesterReputationOut]] = [
        (tester, reputation_map.get(tester.id, TesterReputationOut()))
        for tester in testers
    ]
    ranked_rows.sort(
        key=lambda item: (
            -item[1].reliability_score,
            -item[1].accepted_responses_count,
            -int(item[0].points or 0),
            -int(item[0].streak_best or 0),
            int(item[0].id or 0),
        )
    )

    leaderboard: list[TesterLeaderboardEntryOut] = []
    for index, (tester, summary) in enumerate(ranked_rows[:limit], start=1):
        leaderboard.append(
            TesterLeaderboardEntryOut(
                rank=index,
                user_id=tester.id,
                username=tester.username,
                name=tester.name,
                points=int(tester.points or 0),
                streak_best=int(tester.streak_best or 0),
                responses_count=summary.responses_count,
                accepted_responses_count=summary.accepted_responses_count,
                acceptance_rate=summary.acceptance_rate,
                reliability_score=summary.reliability_score,
                best_categories=summary.best_categories,
            )
        )
    return leaderboard


@router.get("/me/daily-picks", response_model=DailyPicksOut)
def list_daily_picks(
    current_user: User = Depends(require_tester),
    session: Session = Depends(get_session),
):
    day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    responses_today = session.exec(
        select(func.count(Response.id)).where(
            Response.user_id == current_user.id,
            Response.created_at >= day_start,
            Response.created_at < day_end,
        )
    ).one()
    responses_today = int(responses_today or 0)
    if responses_today >= 3:
        return DailyPicksOut(completed_today=True, picks=[])

    responded_project_ids = session.exec(
        select(Response.project_id).where(Response.user_id == current_user.id)
    ).all()
    excluded_ids = set(responded_project_ids)
    selected_projects: list[Project] = []
    selected_ids: set[int] = set()

    category_rows = session.exec(
        select(Project.main_category, func.count(Response.id))
        .join(Response, Response.project_id == Project.id)
        .where(Response.user_id == current_user.id)
        .group_by(Project.main_category)
        .order_by(func.count(Response.id).desc())
    ).all()

    for category, _count in category_rows:
        if len(selected_projects) >= 3:
            break
        stmt = select(Project).where(
            Project.status == "active",
            Project.main_category == category,
            Project.visibility.in_(["public", "tester_only"]),
        )
        blocked_ids = excluded_ids | selected_ids
        if blocked_ids:
            stmt = stmt.where(Project.id.not_in(blocked_ids))
        picks = session.exec(stmt.order_by(Project.id.desc()).limit(3 - len(selected_projects))).all()
        for project in picks:
            selected_projects.append(project)
            selected_ids.add(project.id)

    if len(selected_projects) < 3:
        stmt = select(Project).where(
            Project.status == "active",
            Project.visibility.in_(["public", "tester_only"]),
        )
        blocked_ids = excluded_ids | selected_ids
        if blocked_ids:
            stmt = stmt.where(Project.id.not_in(blocked_ids))
        fallback_projects = session.exec(stmt.order_by(Project.id.desc()).limit(3 - len(selected_projects))).all()
        selected_projects.extend(fallback_projects)

    question_map = _build_project_questions_map(
        session,
        [project.id for project in selected_projects if project.id is not None],
    )
    return DailyPicksOut(
        completed_today=False,
        picks=[serialize_project(project, question_map.get(project.id, [])) for project in selected_projects],
    )


@router.get("/me/notifications", response_model=list[NotificationOut])
def list_my_notifications(
    unread_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    stmt = select(Notification).where(Notification.user_id == current_user.id)
    if unread_only:
        stmt = stmt.where(Notification.read.is_(False))
    notifications = session.exec(
        stmt.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return [serialize_notification(notification) for notification in notifications]


@router.post("/me/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    notification = session.get(Notification, notification_id)
    if not notification or notification.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read = True
    session.add(notification)
    session.commit()
    session.refresh(notification)
    return serialize_notification(notification)
