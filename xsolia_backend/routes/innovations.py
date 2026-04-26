from fastapi import APIRouter
from main import *  # noqa: F401,F403

router = APIRouter()

@router.post("/innovations", response_model=InnovationOut)
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


@router.get("/innovations", response_model=list[InnovationOut])
def list_innovations(
    q: Optional[str] = Query(default=None, max_length=100),
    tag: Optional[str] = Query(default=None, max_length=32),
    intent: Optional[str] = Query(default=None, max_length=32),
    sort: str = Query(default="new"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    stmt = select(Innovation).where(Innovation.status == "active")

    if q:
        query = q.strip()
        if query:
            stmt = stmt.where(
                Innovation.title.contains(query)
                | Innovation.description.contains(query)
                | Innovation.tags.contains(query)
            )
    if tag:
        normalized_tag = tag.strip().lower()
        if normalized_tag:
            stmt = stmt.where(Innovation.tags.contains(normalized_tag))
    if intent:
        normalized_intent = intent.strip().lower()
        if normalized_intent not in INNOVATION_INTENTS:
            raise HTTPException(status_code=422, detail="Invalid intent filter")
        stmt = stmt.where(Innovation.intent == normalized_intent)

    if sort == "top":
        stmt = stmt.order_by(Innovation.upvotes.desc(), Innovation.created_at.desc())
    else:
        stmt = stmt.order_by(Innovation.created_at.desc())

    innovations = session.exec(stmt.offset(offset).limit(limit)).all()
    return [serialize_innovation(innovation) for innovation in innovations]


@router.get("/innovations/saved", response_model=list[InnovationOut])
def list_saved_innovations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    saves = session.exec(
        select(InnovationSave)
        .where(InnovationSave.user_id == current_user.id)
        .order_by(InnovationSave.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    innovation_ids = [save.innovation_id for save in saves]
    if not innovation_ids:
        return []

    innovations = session.exec(
        select(Innovation).where(
            Innovation.id.in_(innovation_ids),
            Innovation.status == "active",
        )
    ).all()
    innovation_map = {innovation.id: innovation for innovation in innovations}
    return [
        serialize_innovation(innovation_map[innovation_id])
        for innovation_id in innovation_ids
        if innovation_id in innovation_map
    ]


@router.post("/innovations/{innovation_id}/vote")
def vote_innovation(
    innovation_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")
    if innovation.author_id == current_user.id:
        raise HTTPException(status_code=403, detail="Users cannot vote for their own innovation")

    existing = session.exec(
        select(InnovationVote).where(
            InnovationVote.innovation_id == innovation_id,
            InnovationVote.user_id == current_user.id,
        )
    ).first()
    if existing:
        return {"ok": True, "innovation_id": innovation_id, "upvotes": innovation.upvotes}

    vote = InnovationVote(innovation_id=innovation_id, user_id=current_user.id)
    innovation.upvotes += 1
    session.add(vote)
    session.add(innovation)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        refreshed = session.get(Innovation, innovation_id)
        return {"ok": True, "innovation_id": innovation_id, "upvotes": refreshed.upvotes if refreshed else 0}

    session.refresh(innovation)
    return {"ok": True, "innovation_id": innovation_id, "upvotes": innovation.upvotes}


@router.post("/innovations/{innovation_id}/save")
def save_innovation(
    innovation_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")

    existing = session.exec(
        select(InnovationSave).where(
            InnovationSave.innovation_id == innovation_id,
            InnovationSave.user_id == current_user.id,
        )
    ).first()
    if existing:
        return {"ok": True, "innovation_id": innovation_id, "saved": True}

    session.add(InnovationSave(innovation_id=innovation_id, user_id=current_user.id))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    return {"ok": True, "innovation_id": innovation_id, "saved": True}


@router.delete("/innovations/{innovation_id}/save")
def unsave_innovation(
    innovation_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    existing = session.exec(
        select(InnovationSave).where(
            InnovationSave.innovation_id == innovation_id,
            InnovationSave.user_id == current_user.id,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
    return {"ok": True, "innovation_id": innovation_id, "saved": False}


@router.patch("/innovations/{innovation_id}/status", response_model=InnovationOut)
def update_innovation_status(
    innovation_id: int,
    payload: InnovationStatusUpdate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    innovation = session.get(Innovation, innovation_id)
    if not innovation:
        raise HTTPException(status_code=404, detail="Innovation not found")
    if innovation.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the innovation author can update status")

    innovation.status = payload.status
    session.add(innovation)
    session.commit()
    session.refresh(innovation)
    return serialize_innovation(innovation)


@router.get("/innovations/{innovation_id}", response_model=InnovationOut)
def get_innovation(innovation_id: int, session: Session = Depends(get_session)):
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")
    return serialize_innovation(innovation)


@router.get("/innovations/{innovation_id}/validation-draft", response_model=InnovationValidationDraftOut)
def get_innovation_validation_draft(
    innovation_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    _ = current_user
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")

    return InnovationValidationDraftOut(
        title=innovation.title,
        description=innovation.description,
        main_category=infer_validation_category_from_innovation(innovation),
        subcategory=None,
        target_audience="",
        questions=INNOVATION_VALIDATION_DRAFT_QUESTIONS,
        source_innovation_id=innovation.id,
    )


@router.post("/innovations/{innovation_id}/comments", response_model=InnovationCommentOut)
def create_innovation_comment(
    innovation_id: int,
    payload: InnovationCommentCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")

    comment = InnovationComment(
        innovation_id=innovation_id,
        author_id=current_user.id,
        text=payload.text,
    )
    session.add(comment)
    session.commit()
    session.refresh(comment)
    return serialize_innovation_comment(comment, author_name=current_user.name)


@router.get("/innovations/{innovation_id}/comments", response_model=list[InnovationCommentOut])
def list_innovation_comments(
    innovation_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    innovation = session.get(Innovation, innovation_id)
    if not innovation or innovation.status != "active":
        raise HTTPException(status_code=404, detail="Innovation not found")

    comments = session.exec(
        select(InnovationComment)
        .where(InnovationComment.innovation_id == innovation_id)
        .order_by(InnovationComment.created_at.asc())
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
        serialize_innovation_comment(comment, author_name=author_map.get(comment.author_id))
        for comment in comments
    ]
