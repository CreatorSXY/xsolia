from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

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

        paged_responses = client.get(
            f"/projects/{project_id}/responses?limit=1&offset=0",
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert paged_responses.status_code == 200
        assert len(paged_responses.json()) == 1
    finally:
        main.app.dependency_overrides = {}


def test_innovation_tags_preserve_commas_via_json_storage():
    client = _make_client()
    try:
        creator = {
            "email": "creator-tags@example.com",
            "name": "Creator Tags",
            "password": "pass1234",
            "role": "creator",
            "subscription": "creator_basic",
        }
        _register(client, creator)
        creator_login = _login(client, creator["email"], creator["password"])

        created = client.post(
            "/innovations",
            json={
                "title": "Comma tag test",
                "description": "Valid innovation description for comma tag test.",
                "tags": ["alpha,beta", "gamma"],
                "intent": "open",
            },
            headers=_auth_headers(creator_login["access_token"]),
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["tags"] == ["alpha,beta", "gamma"]

        innovation_id = payload["id"]
        fetched = client.get(f"/innovations/{innovation_id}")
        assert fetched.status_code == 200
        assert fetched.json()["tags"] == ["alpha,beta", "gamma"]

        # Verify raw storage is JSON text, not comma-joined.
        override = main.app.dependency_overrides[main.get_session]
        session_gen = override()
        session = next(session_gen)
        innovation_row = session.get(main.Innovation, innovation_id)
        assert innovation_row.tags.startswith("[")
        session_gen.close()
    finally:
        main.app.dependency_overrides = {}
