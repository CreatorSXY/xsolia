# xsolia (krotka)

A lightweight idea-validation platform:
- **Backend**: FastAPI + SQLModel
- **Frontend**: static multi-page HTML/CSS/JS
- **Core flow**: creators publish topics -> testers respond -> creators review responses and stats

## Project Structure

```text
krotka/
├─ xsolia_backend/          # FastAPI service
│  ├─ main.py
│  ├─ requirements.txt
│  └─ tests/
├─ xsolia_frontend/         # Static frontend pages and assets
│  ├─ *.html
│  ├─ js/
│  ├─ css/
│  └─ assets/
└─ deploy.sh                # Server deploy script (systemd + nginx)
```

## Backend Quick Start

```bash
cd xsolia_backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Required in real deployment
export XSOLIA_SECRET_KEY='replace-with-a-strong-secret'

# Optional (defaults shown)
# export XSOLIA_DATABASE_URL='sqlite:///./database.db'
# export XSOLIA_TOKEN_EXPIRE_SECONDS='604800'
# export XSOLIA_FREE_CREATOR_PROJECT_QUOTA='1'
# export XSOLIA_AUTH_RATE_LIMIT_WINDOW_SECONDS='300'
# export XSOLIA_AUTH_RATE_LIMIT_MAX_ATTEMPTS='20'
# export XSOLIA_AUTH_RATE_LIMIT_SWEEP_INTERVAL_SECONDS='120'

uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Frontend Quick Start (Static)

Frontend is pure static files (no build required).

```bash
cd xsolia_frontend
python3 -m http.server 5500
```

Open:
- `http://127.0.0.1:5500/index.html`

### Connect frontend to local backend

Current API base is hardcoded in `xsolia_frontend/js/app.js`:

```js
const API_BASE = "https://api.xsolia.com";
```

For local development, change it to:

```js
const API_BASE = "http://127.0.0.1:8000";
```

## Main API Endpoints

Auth:
- `POST /register`
- `POST /login`
- `GET /me`

Projects:
- `POST /projects`
- `GET /projects/active?limit=&offset=&main_category=&subcategory=`
- `GET /projects/mine?status=&limit=&offset=`
- `GET /projects/{project_id}`
- `POST /projects/{project_id}/respond`
- `GET /projects/{project_id}/responses?limit=&offset=`
- `POST /responses/{response_id}/accept`
- `GET /projects/{project_id}/stats`
- `GET /projects/{project_id}/ai-summary` (currently returns `501`)

Innovations:
- `POST /innovations`
- `GET /innovations?limit=&offset=`
- `GET /innovations/{innovation_id}`

## Product/Behavior Notes

- Free creator accounts can post a limited number of **active** projects (default quota: `1`).
- Access tokens are standard JWT (HS256).
- Register/login endpoints have in-memory rate limiting.
- Project stats include:
  - response count
  - interest distribution (1~5)
  - average interest + stddev
  - average price min/max
  - price percentiles (p25/p50/p75)
  - acceptance rate
- `Project.questions` and `Response.answers` are persisted in normalized tables, with legacy data migration on startup.
- Innovation tags are stored as JSON text, with migration for legacy comma-separated tags.

## Running Tests

```bash
cd xsolia_backend
python3 -m pytest -q
```

If your local Python environment has `pytest` plugin/runtime issues, use a clean venv and rerun.

## Deployment

Use the root script:

```bash
./deploy.sh
```

It performs:
- git pull
- backend venv/install
- optional alembic migration (if `alembic.ini` exists)
- systemd service restart
- frontend sync to nginx web root
- health checks

You can override deployment paths and service name via env vars in `deploy.sh`.
