from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine
import asyncio
import json

import main


def _make_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def get_session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[main.get_session] = get_session_override
    return TestClient(main.app)


def _register(client: TestClient, payload: dict):
    res = client.post("/register", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "points" in data
    return data


def _login(client: TestClient, email: str, password: str):
    res = client.post("/login", json={"email": email, "password": password})
    assert res.status_code == 200
    data = res.json()
    assert data["access_token"]
    return data


def _auth_headers(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_interest_level_validation_and_duplicate_guard():
    client = _make_client()
    try:
        creator_payload = {
            "email": "creator@example.com",
            "name": "Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        tester_payload = {
            "email": "tester@example.com",
            "name": "Tester",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }

        _register(client, creator_payload)
        _register(client, tester_payload)

        creator_login = _login(client, creator_payload["email"], creator_payload["password"])
        tester_login = _login(client, tester_payload["email"], tester_payload["password"])

        project_payload = {
            "title": "Test Project",
            "description": "A short but valid description for this project.",
            "target_audience": "Everyone",
            "questions": ["Q1"],
            "budget": 100,
            "main_category": "testing",
            "subcategory": "software",
        }
        project_res = client.post(
            "/projects",
            json=project_payload,
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert project_res.status_code == 200
        project_id = project_res.json()["id"]

        bad_response = {
            "interest_level": 0,
            "answers": ["A1"],
        }
        res = client.post(
            f"/projects/{project_id}/respond",
            json=bad_response,
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert res.status_code == 422

        good_response = {
            "interest_level": 3,
            "answers": ["A1"],
        }
        res = client.post(
            f"/projects/{project_id}/respond",
            json=good_response,
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True

        duplicate = client.post(
            f"/projects/{project_id}/respond",
            json=good_response,
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert duplicate.status_code == 400
    finally:
        main.app.dependency_overrides = {}


def test_project_response_permissions_and_accept_flow():
    client = _make_client()
    try:
        creator_a = {
            "email": "creator-a@example.com",
            "name": "Creator A",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_plus",
        }
        creator_b = {
            "email": "creator-b@example.com",
            "name": "Creator B",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        tester = {
            "email": "tester-2@example.com",
            "name": "Tester",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }

        _register(client, creator_a)
        _register(client, creator_b)
        _register(client, tester)

        creator_a_login = _login(client, creator_a["email"], creator_a["password"])
        creator_b_login = _login(client, creator_b["email"], creator_b["password"])
        tester_login = _login(client, tester["email"], tester["password"])

        project_payload = {
            "title": "Creator A topic",
            "description": "This is a valid project description for permission tests.",
            "target_audience": "Remote workers",
            "questions": ["Would you use this?"],
            "budget": 80,
            "main_category": "digital",
            "subcategory": "digital_consumer_app",
        }
        create_project = client.post(
            "/projects",
            json=project_payload,
            headers=_auth_headers(creator_a_login["access_token"]),
        )
        assert create_project.status_code == 200
        project_id = create_project.json()["id"]

        respond = client.post(
            f"/projects/{project_id}/respond",
            json={"interest_level": 4, "answers": ["Yes"]},
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert respond.status_code == 200

        forbidden_list = client.get(
            f"/projects/{project_id}/responses",
            headers=_auth_headers(creator_b_login["access_token"]),
        )
        assert forbidden_list.status_code == 403

        owner_list = client.get(
            f"/projects/{project_id}/responses",
            headers=_auth_headers(creator_a_login["access_token"]),
        )
        assert owner_list.status_code == 200
        responses = owner_list.json()
        assert len(responses) == 1

        response_id = responses[0]["id"]
        accept = client.post(
            f"/responses/{response_id}/accept",
            headers=_auth_headers(creator_a_login["access_token"]),
        )
        assert accept.status_code == 200
        assert accept.json()["ok"] is True
    finally:
        main.app.dependency_overrides = {}


def test_price_range_validation():
    client = _make_client()
    try:
        creator_payload = {
            "email": "creator-price@example.com",
            "name": "Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        tester_payload = {
            "email": "tester-price@example.com",
            "name": "Tester",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }

        _register(client, creator_payload)
        _register(client, tester_payload)

        creator_login = _login(client, creator_payload["email"], creator_payload["password"])
        tester_login = _login(client, tester_payload["email"], tester_payload["password"])

        create_project = client.post(
            "/projects",
            json={
                "title": "Pricing validation",
                "description": "Valid description for pricing range validation checks.",
                "target_audience": "Students",
                "questions": ["Price range?"],
                "budget": 30,
                "main_category": "testing",
                "subcategory": "other",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert create_project.status_code == 200
        project_id = create_project.json()["id"]

        invalid_price = client.post(
            f"/projects/{project_id}/respond",
            json={
                "interest_level": 4,
                "answers": ["Maybe"],
                "price_min": 100,
                "price_max": 20,
            },
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert invalid_price.status_code == 422
    finally:
        main.app.dependency_overrides = {}


def test_free_creator_quota_allows_first_project_only():
    client = _make_client()
    try:
        creator_payload = {
            "email": "creator-free@example.com",
            "name": "Free Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "free",
        }

        _register(client, creator_payload)
        creator_login = _login(client, creator_payload["email"], creator_payload["password"])

        base_project = {
            "title": "Free quota topic",
            "description": "A valid description for the free creator first project quota.",
            "target_audience": "Indie makers",
            "questions": ["Would you pay for this?"],
            "budget": 20,
            "main_category": "digital",
            "subcategory": "other",
        }

        first = client.post(
            "/projects",
            json=base_project,
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert first.status_code == 200

        second_payload = dict(base_project)
        second_payload["title"] = "Second post should be blocked"
        second = client.post(
            "/projects",
            json=second_payload,
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert second.status_code == 403
        assert "Free creator quota used" in second.json()["detail"]
    finally:
        main.app.dependency_overrides = {}


def test_free_creator_quota_counts_only_active_projects():
    client = _make_client()
    try:
        creator_payload = {
            "email": "creator-free-active@example.com",
            "name": "Free Creator Active",
            "password": "pass1234",
            "role": "creator",
            "subscription": "free",
        }

        _register(client, creator_payload)
        creator_login = _login(client, creator_payload["email"], creator_payload["password"])

        first_project = client.post(
            "/projects",
            json={
                "title": "First free topic",
                "description": "A valid description for the first free topic post.",
                "target_audience": "Designers",
                "questions": ["Q1"],
                "budget": 10,
                "main_category": "digital",
                "subcategory": "other",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert first_project.status_code == 200
        first_project_id = first_project.json()["id"]

        # Mark first project as inactive; free creator should be allowed to post again.
        override = main.app.dependency_overrides[main.get_session]
        session_gen = override()
        session = next(session_gen)
        project_row = session.get(main.Project, first_project_id)
        project_row.status = "closed"
        session.add(project_row)
        session.commit()
        session_gen.close()

        second_project = client.post(
            "/projects",
            json={
                "title": "Second free topic",
                "description": "A valid description for second free post after closure.",
                "target_audience": "Designers",
                "questions": ["Q1"],
                "budget": 10,
                "main_category": "digital",
                "subcategory": "other",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert second_project.status_code == 200
    finally:
        main.app.dependency_overrides = {}


def test_project_stats_distribution_percentiles_and_acceptance_rate():
    client = _make_client()
    try:
        creator = {
            "email": "creator-stats@example.com",
            "name": "Creator Stats",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_plus",
        }
        tester1 = {
            "email": "tester-stats-1@example.com",
            "name": "Tester One",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        tester2 = {
            "email": "tester-stats-2@example.com",
            "name": "Tester Two",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }

        _register(client, creator)
        _register(client, tester1)
        _register(client, tester2)

        creator_login = _login(client, creator["email"], creator["password"])
        tester1_login = _login(client, tester1["email"], tester1["password"])
        tester2_login = _login(client, tester2["email"], tester2["password"])

        project = client.post(
            "/projects",
            json={
                "title": "Stats topic",
                "description": "Valid project description for stats validation coverage.",
                "target_audience": "B2B SaaS founders",
                "questions": ["Q1", "Q2"],
                "budget": 120,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert project.status_code == 200
        project_id = project.json()["id"]

        r1 = client.post(
            f"/projects/{project_id}/respond",
            json={
                "interest_level": 2,
                "answers": ["a1", "a2"],
                "price_min": 50,
                "price_max": 100,
            },
            headers=_auth_headers(tester1_login["access_token"]),
        )
        assert r1.status_code == 200

        r2 = client.post(
            f"/projects/{project_id}/respond",
            json={
                "interest_level": 5,
                "answers": ["b1", "b2"],
                "price_min": 200,
                "price_max": 300,
            },
            headers=_auth_headers(tester2_login["access_token"]),
        )
        assert r2.status_code == 200

        list_responses = client.get(
            f"/projects/{project_id}/responses?limit=10&offset=0",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert list_responses.status_code == 200
        payload = list_responses.json()
        assert len(payload) == 2

        accept = client.post(
            f"/responses/{payload[0]['id']}/accept",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert accept.status_code == 200

        stats = client.get(
            f"/projects/{project_id}/stats",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert stats.status_code == 200
        data = stats.json()
        assert data["responses_count"] == 2
        assert data["interest_distribution"]["2"] == 1
        assert data["interest_distribution"]["5"] == 1
        assert data["price_percentiles"]["p50"] == 162.5
        assert data["acceptance_rate"] == 0.5
        assert data["interest_stddev"] > 0
    finally:
        main.app.dependency_overrides = {}


def test_pagination_for_projects_innovations_and_responses():
    client = _make_client()
    try:
        creator = {
            "email": "creator-pagination@example.com",
            "name": "Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        tester_a = {
            "email": "tester-pagination-a@example.com",
            "name": "Tester A",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        tester_b = {
            "email": "tester-pagination-b@example.com",
            "name": "Tester B",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }

        _register(client, creator)
        _register(client, tester_a)
        _register(client, tester_b)

        creator_login = _login(client, creator["email"], creator["password"])
        tester_a_login = _login(client, tester_a["email"], tester_a["password"])
        tester_b_login = _login(client, tester_b["email"], tester_b["password"])

        created_project_ids = []
        for i in range(3):
            create_project = client.post(
                "/projects",
                json={
                    "title": f"Topic {i}",
                    "description": "Valid description for pagination tests in active projects list.",
                    "target_audience": "Developers",
                    "questions": ["Q1"],
                    "budget": 50,
                    "main_category": "testing",
                    "subcategory": "other",
                },
                headers=_auth_headers(creator_login["access_token"]),
            )
            assert create_project.status_code == 200
            created_project_ids.append(create_project.json()["id"])

        projects_page = client.get("/projects/active?limit=2&offset=1")
        assert projects_page.status_code == 200
        projects_data = projects_page.json()
        assert len(projects_data) == 2
        assert projects_data[0]["id"] < created_project_ids[-1]

        my_projects = client.get(
            "/projects/mine?limit=2&offset=0",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert my_projects.status_code == 200
        assert len(my_projects.json()) == 2

        for i in range(3):
            innovation = client.post(
                "/innovations",
                json={
                    "title": f"Innovation {i}",
                    "description": "A valid innovation description for pagination checks.",
                    "tags": ["alpha"],
                    "intent": "open",
                },
                headers=_auth_headers(creator_login["access_token"]),
            )
            assert innovation.status_code == 200

        innovations_page = client.get("/innovations?limit=2&offset=1")
        assert innovations_page.status_code == 200
        assert len(innovations_page.json()) == 2
        innovations_by_intent = client.get("/innovations?intent=open&limit=10&offset=0")
        assert innovations_by_intent.status_code == 200
        assert all(item["intent"] == "open" for item in innovations_by_intent.json())
        invalid_intent = client.get("/innovations?intent=unknown")
        assert invalid_intent.status_code == 422

        project_id = created_project_ids[0]
        response_a = client.post(
            f"/projects/{project_id}/respond",
            json={"interest_level": 4, "answers": ["a1"]},
            headers=_auth_headers(tester_a_login["access_token"]),
        )
        assert response_a.status_code == 200
        response_b = client.post(
            f"/projects/{project_id}/respond",
            json={"interest_level": 3, "answers": ["b1"]},
            headers=_auth_headers(tester_b_login["access_token"]),
        )
        assert response_b.status_code == 200

        projects_active_sorted = client.get("/projects/active?sort=active&limit=3&offset=0")
        assert projects_active_sorted.status_code == 200
        sorted_payload = projects_active_sorted.json()
        assert sorted_payload[0]["id"] == project_id
        invalid_project_sort = client.get("/projects/active?sort=unknown")
        assert invalid_project_sort.status_code == 422

        paged_responses = client.get(
            f"/projects/{project_id}/responses?limit=1&offset=0",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert paged_responses.status_code == 200
        assert len(paged_responses.json()) == 1
    finally:
        main.app.dependency_overrides = {}


def test_project_status_update_and_active_search():
    client = _make_client()
    try:
        creator = {
            "email": "creator-status@example.com",
            "name": "Creator Status",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        other_creator = {
            "email": "other-creator-status@example.com",
            "name": "Other Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        _register(client, creator)
        _register(client, other_creator)

        creator_login = _login(client, creator["email"], creator["password"])
        other_login = _login(client, other_creator["email"], other_creator["password"])

        project = client.post(
            "/projects",
            json={
                "title": "Solar validation topic",
                "description": "A valid description about solar panels for search.",
                "target_audience": "Homeowners",
                "questions": ["Q1"],
                "budget": 10,
                "main_category": "physical",
                "subcategory": "energy",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert project.status_code == 200
        project_id = project.json()["id"]

        search_hit = client.get("/projects/active?q=solar")
        assert search_hit.status_code == 200
        assert [item["id"] for item in search_hit.json()] == [project_id]

        forbidden = client.patch(
            f"/projects/{project_id}/status",
            json={"status": "closed"},
            headers=_auth_headers(other_login["access_token"]),
        )
        assert forbidden.status_code == 403

        invalid_status = client.patch(
            f"/projects/{project_id}/status",
            json={"status": "deleted"},
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert invalid_status.status_code == 422

        closed = client.patch(
            f"/projects/{project_id}/status",
            json={"status": "closed"},
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert closed.status_code == 200
        assert closed.json()["status"] == "closed"

        search_miss = client.get("/projects/active?q=solar")
        assert search_miss.status_code == 200
        assert search_miss.json() == []
    finally:
        main.app.dependency_overrides = {}


def test_response_like_comment_and_me_responses_flow():
    client = _make_client()
    try:
        creator = {
            "email": "creator-response-tools@example.com",
            "name": "Creator Tools",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        tester_a = {
            "email": "tester-response-a@example.com",
            "name": "Tester A",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        tester_b = {
            "email": "tester-response-b@example.com",
            "name": "Tester B",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        _register(client, creator)
        _register(client, tester_a)
        _register(client, tester_b)

        creator_login = _login(client, creator["email"], creator["password"])
        tester_a_login = _login(client, tester_a["email"], tester_a["password"])
        tester_b_login = _login(client, tester_b["email"], tester_b["password"])

        project = client.post(
            "/projects",
            json={
                "title": "Response tooling topic",
                "description": "A valid description for response interaction tests.",
                "target_audience": "Operators",
                "questions": ["Q1"],
                "budget": 10,
                "main_category": "service",
                "subcategory": "ops",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert project.status_code == 200
        project_id = project.json()["id"]

        response = client.post(
            f"/projects/{project_id}/respond",
            json={"interest_level": 4, "answers": ["Useful"]},
            headers=_auth_headers(tester_a_login["access_token"]),
        )
        assert response.status_code == 200
        response_id = response.json()["response_id"]

        mine = client.get(
            "/me/responses",
            headers=_auth_headers(tester_a_login["access_token"]),
        )
        assert mine.status_code == 200
        assert len(mine.json()) == 1
        assert mine.json()[0]["id"] == response_id

        self_like = client.post(
            f"/responses/{response_id}/like",
            headers=_auth_headers(tester_a_login["access_token"]),
        )
        assert self_like.status_code == 403

        like = client.post(
            f"/responses/{response_id}/like",
            headers=_auth_headers(tester_b_login["access_token"]),
        )
        assert like.status_code == 200
        assert like.json()["likes_count"] == 1

        duplicate_like = client.post(
            f"/responses/{response_id}/like",
            headers=_auth_headers(tester_b_login["access_token"]),
        )
        assert duplicate_like.status_code == 400

        comment = client.post(
            f"/responses/{response_id}/comments",
            json={"text": "Can you clarify your workflow?"},
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert comment.status_code == 200
        assert comment.json()["text"] == "Can you clarify your workflow?"
        assert comment.json()["author_name"] == creator["name"]

        author_comments = client.get(
            f"/responses/{response_id}/comments",
            headers=_auth_headers(tester_a_login["access_token"]),
        )
        assert author_comments.status_code == 200
        assert len(author_comments.json()) == 1
        assert author_comments.json()[0]["author_name"] == creator["name"]

        unrelated_comments = client.get(
            f"/responses/{response_id}/comments",
            headers=_auth_headers(tester_b_login["access_token"]),
        )
        assert unrelated_comments.status_code == 403
    finally:
        main.app.dependency_overrides = {}


def test_ai_summary_generation_cache_refresh_and_disabled_provider():
    client = _make_client()
    original_generate = main.generate_ai_summary
    try:
        creator = {
            "email": "creator-ai@example.com",
            "name": "Creator AI",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_plus",
        }
        tester = {
            "email": "tester-ai@example.com",
            "name": "Tester AI",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        _register(client, creator)
        _register(client, tester)

        creator_login = _login(client, creator["email"], creator["password"])
        tester_login = _login(client, tester["email"], tester["password"])

        project = client.post(
            "/projects",
            json={
                "title": "AI summary topic",
                "description": "A valid description for AI summary coverage.",
                "target_audience": "Founders",
                "questions": ["What is strongest?", "What blocks adoption?"],
                "budget": 100,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert project.status_code == 200
        project_id = project.json()["id"]

        response = client.post(
            f"/projects/{project_id}/respond",
            json={
                "interest_level": 5,
                "answers": ["Clear pricing signal", "Needs integrations"],
                "price_min": 25,
                "price_max": 75,
            },
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert response.status_code == 200

        calls = {"count": 0}

        def fake_generate(summary_input: str):
            calls["count"] += 1
            assert "AI summary topic" in summary_input
            assert "Clear pricing signal" in summary_input
            return {
                "summary": f"Generated summary {calls['count']}",
                "key_signals": ["pricing signal"],
                "pricing_insight": {
                    "willingness": "medium",
                    "observed_range": "25-75",
                    "notes": "Tester provided a bounded range.",
                },
                "interest_insight": {
                    "distribution_note": "One high-interest response.",
                    "best_segment": "Founders",
                },
                "top_objections": ["Needs integrations"],
                "suggested_next_steps": ["Validate integrations"],
            }

        main.generate_ai_summary = fake_generate

        first = client.get(
            f"/projects/{project_id}/ai-summary",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["cached"] is False
        assert first_payload["responses_count"] == 1
        assert first_payload["summary"]["summary"] == "Generated summary 1"
        assert calls["count"] == 1

        second = client.get(
            f"/projects/{project_id}/ai-summary",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert second.status_code == 200
        second_payload = second.json()
        assert second_payload["cached"] is True
        assert second_payload["summary"]["summary"] == "Generated summary 1"
        assert calls["count"] == 1

        refreshed = client.get(
            f"/projects/{project_id}/ai-summary?refresh=true",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert refreshed.status_code == 200
        refreshed_payload = refreshed.json()
        assert refreshed_payload["cached"] is False
        assert refreshed_payload["summary"]["summary"] == "Generated summary 2"
        assert calls["count"] == 2

        main.generate_ai_summary = original_generate
        uncached_project = client.post(
            "/projects",
            json={
                "title": "Disabled AI provider",
                "description": "A valid description for disabled AI provider checks.",
                "target_audience": "Founders",
                "questions": ["Q1"],
                "budget": 100,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert uncached_project.status_code == 200
        disabled = client.get(
            f"/projects/{uncached_project.json()['id']}/ai-summary",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert disabled.status_code == 501
    finally:
        main.generate_ai_summary = original_generate
        main.app.dependency_overrides = {}


def test_gemini_summary_provider_request_and_parse(monkeypatch):
    original_provider = main.AI_PROVIDER
    original_model = main.AI_MODEL
    original_key = main.GEMINI_API_KEY

    captured = {}

    class FakeHTTPXResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "summary": "Gemini summary",
                                            "key_signals": ["signal"],
                                            "pricing_insight": {
                                                "willingness": "medium",
                                                "observed_range": "10-20",
                                                "notes": "price noted",
                                            },
                                            "interest_insight": {
                                                "distribution_note": "high interest",
                                                "best_segment": "founders",
                                            },
                                            "top_objections": ["objection"],
                                            "suggested_next_steps": ["next"],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return FakeHTTPXResponse()

    try:
        main.AI_PROVIDER = "gemini"
        main.AI_MODEL = "gemini-2.0-flash"
        main.GEMINI_API_KEY = "test-gemini-key"
        monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

        result = asyncio.run(main.generate_ai_summary("Project: test"))
        assert result["summary"] == "Gemini summary"
        assert "models/gemini-2.0-flash:generateContent" in captured["url"]
        assert captured["headers"]["x-goog-api-key"] == "test-gemini-key"
        assert captured["payload"]["generationConfig"]["responseMimeType"] == "application/json"
        schema = captured["payload"]["generationConfig"]["responseJsonSchema"]
        assert schema["propertyOrdering"][0] == "summary"
        assert "pricing_insight" in schema["properties"]
    finally:
        main.AI_PROVIDER = original_provider
        main.AI_MODEL = original_model
        main.GEMINI_API_KEY = original_key


def test_project_images_support_and_limit():
    client = _make_client()
    try:
        creator = {
            "email": "creator-images@example.com",
            "name": "Creator Images",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        _register(client, creator)
        creator_login = _login(client, creator["email"], creator["password"])

        image_a = "https://cdn.example.com/a.png"
        image_b = "https://cdn.example.com/b.png"

        created = client.post(
            "/projects",
            json={
                "title": "Topic with images",
                "description": "A valid description to verify project image support.",
                "target_audience": "Founders",
                "questions": ["Do images help understanding?"],
                "image_urls": [image_a, image_b],
                "budget": 100,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert created.status_code == 200
        payload = created.json()
        assert len(payload["image_urls"]) == 2
        assert payload["image_urls"][0] == image_a

        invalid_data_url = client.post(
            "/projects",
            json={
                "title": "Invalid base64 images",
                "description": "A valid description to verify only URL images are accepted.",
                "target_audience": "Founders",
                "questions": ["Question"],
                "image_urls": ["data:image/png;base64,abcd"],
                "budget": 80,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert invalid_data_url.status_code == 422

        too_many = client.post(
            "/projects",
            json={
                "title": "Too many images",
                "description": "A valid description to trigger image count validation.",
                "target_audience": "Founders",
                "questions": ["Question"],
                "image_urls": [image_a, image_b, "https://cdn.example.com/c.png", "https://cdn.example.com/d.png"],
                "budget": 80,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert too_many.status_code == 422
    finally:
        main.app.dependency_overrides = {}


def test_project_visibility_validation_and_access_rules():
    client = _make_client()
    try:
        creator = {
            "email": "visibility-creator@example.com",
            "name": "Visibility Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_plus",
        }
        tester = {
            "email": "visibility-tester@example.com",
            "name": "Visibility Tester",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        _register(client, creator)
        _register(client, tester)
        creator_login = _login(client, creator["email"], creator["password"])
        tester_login = _login(client, tester["email"], tester["password"])

        invalid_visibility = client.post(
            "/projects",
            json={
                "title": "Bad visibility",
                "description": "A valid description for visibility validation.",
                "target_audience": "Builders",
                "questions": ["Question?"],
                "budget": 50,
                "main_category": "digital",
                "subcategory": "saas",
                "visibility": "hidden",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert invalid_visibility.status_code == 422

        invalid_detail = client.post(
            "/projects",
            json={
                "title": "Bad detail",
                "description": "A valid description for detail level validation.",
                "target_audience": "Builders",
                "questions": ["Question?"],
                "budget": 50,
                "main_category": "digital",
                "subcategory": "saas",
                "detail_level": "everything",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert invalid_detail.status_code == 422

        def create_topic(title: str, visibility: str):
            response = client.post(
                "/projects",
                json={
                    "title": title,
                    "description": "A valid description for project visibility coverage.",
                    "target_audience": "Builders",
                    "questions": ["Would you use this?"],
                    "budget": 90,
                    "main_category": "digital",
                    "subcategory": "saas",
                    "visibility": visibility,
                },
                headers=_auth_headers(creator_login["access_token"]),
            )
            assert response.status_code == 200
            return response.json()["id"]

        public_id = create_topic("Public topic", "public")
        tester_only_id = create_topic("Tester-only topic", "tester_only")
        invite_only_id = create_topic("Invite-only topic", "invite_only")
        unlisted_id = create_topic("Unlisted topic", "unlisted")

        public_list = client.get("/projects/active?limit=50&offset=0")
        assert public_list.status_code == 200
        public_ids = [item["id"] for item in public_list.json()]
        assert public_id in public_ids
        assert tester_only_id not in public_ids
        assert invite_only_id not in public_ids
        assert unlisted_id not in public_ids

        tester_list = client.get(
            "/projects/active?limit=50&offset=0",
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert tester_list.status_code == 200
        tester_ids = [item["id"] for item in tester_list.json()]
        assert public_id in tester_ids
        assert tester_only_id in tester_ids
        assert invite_only_id not in tester_ids
        assert unlisted_id not in tester_ids

        tester_only_anon_detail = client.get(f"/projects/{tester_only_id}")
        assert tester_only_anon_detail.status_code == 401
        tester_only_auth_detail = client.get(
            f"/projects/{tester_only_id}",
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert tester_only_auth_detail.status_code == 200

        invite_detail = client.get(
            f"/projects/{invite_only_id}",
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert invite_detail.status_code == 403

        unlisted_detail = client.get(f"/projects/{unlisted_id}")
        assert unlisted_detail.status_code == 200
    finally:
        main.app.dependency_overrides = {}


def test_creator_dashboard_summary_and_decision_stages():
    client = _make_client()
    try:
        creator = {
            "email": "dashboard-creator@example.com",
            "name": "Dashboard Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_plus",
        }
        _register(client, creator)
        creator_login = _login(client, creator["email"], creator["password"])

        tester_tokens: list[str] = []
        for idx in range(1, 21):
            tester = {
                "email": f"dashboard-tester-{idx}@example.com",
                "name": f"Tester {idx}",
                "password": "pass1234",
                "role": "tester",
                "subscription": "free",
            }
            _register(client, tester)
            tester_tokens.append(_login(client, tester["email"], tester["password"])["access_token"])

        project_ids: list[int] = []
        for idx in range(1, 4):
            created = client.post(
                "/projects",
                json={
                    "title": f"Decision Project {idx}",
                    "description": "A valid description for creator decision dashboard test coverage.",
                    "target_audience": "Builders",
                    "questions": ["Would you use this?"],
                    "budget": 100,
                    "main_category": "digital",
                    "subcategory": "saas",
                },
                headers=_auth_headers(creator_login["access_token"]),
            )
            assert created.status_code == 200
            project_ids.append(created.json()["id"])

        low_signal_project, enough_signal_project, decision_project = project_ids

        for token in tester_tokens[:3]:
            response = client.post(
                f"/projects/{low_signal_project}/respond",
                json={"interest_level": 3, "answers": ["Needs work"]},
                headers=_auth_headers(token),
            )
            assert response.status_code == 200

        for token in tester_tokens[:15]:
            response = client.post(
                f"/projects/{enough_signal_project}/respond",
                json={"interest_level": 5, "answers": ["Strong demand"]},
                headers=_auth_headers(token),
            )
            assert response.status_code == 200

        for token in tester_tokens:
            response = client.post(
                f"/projects/{decision_project}/respond",
                json={"interest_level": 2, "answers": ["Low priority"]},
                headers=_auth_headers(token),
            )
            assert response.status_code == 200

        dashboard = client.get(
            "/me/creator-dashboard",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert dashboard.status_code == 200
        payload = dashboard.json()
        assert payload["summary"]["total_projects"] == 3
        assert payload["summary"]["active_projects"] == 3
        assert payload["summary"]["total_responses"] == 38
        assert payload["summary"]["avg_interest_overall"] > 0

        stage_map = {item["id"]: item["decision_stage"] for item in payload["projects"]}
        assert stage_map[low_signal_project] == "draft_or_low_signal"
        assert stage_map[enough_signal_project] == "enough_signal"
        assert stage_map[decision_project] == "decision_needed"
        assert payload["summary"]["projects_needing_decision"] >= 2
        assert all("next_step" in item for item in payload["projects"])
    finally:
        main.app.dependency_overrides = {}


def test_response_payload_includes_responder_reputation():
    client = _make_client()
    try:
        creator = {
            "email": "response-reputation-creator@example.com",
            "name": "Creator",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_plus",
        }
        tester = {
            "email": "response-reputation-tester@example.com",
            "name": "Tester",
            "password": "pass1234",
            "role": "tester",
            "subscription": "free",
        }
        _register(client, creator)
        _register(client, tester)
        creator_login = _login(client, creator["email"], creator["password"])
        tester_login = _login(client, tester["email"], tester["password"])

        created = client.post(
            "/projects",
            json={
                "title": "Response summary topic",
                "description": "A valid description for response reputation payload checks.",
                "target_audience": "Builders",
                "questions": ["Would this help?"],
                "budget": 100,
                "main_category": "digital",
                "subcategory": "saas",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert created.status_code == 200
        project_id = created.json()["id"]

        responded = client.post(
            f"/projects/{project_id}/respond",
            json={"interest_level": 4, "answers": ["Looks useful"]},
            headers=_auth_headers(tester_login["access_token"]),
        )
        assert responded.status_code == 200

        response_list = client.get(
            f"/projects/{project_id}/responses?limit=20&offset=0",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert response_list.status_code == 200
        rows = response_list.json()
        assert len(rows) == 1
        reputation = rows[0]["responder_reputation"]
        assert reputation["responses_count"] >= 1
        assert "reliability_score" in reputation
        assert rows[0]["responder_name"] == "Tester"
    finally:
        main.app.dependency_overrides = {}