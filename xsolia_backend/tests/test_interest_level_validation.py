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
    return res.json()


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
