# chat-service

User-facing FastAPI chat API. Handles authentication, chat sessions, document
references, and admin operations, backed by PostgreSQL and Redis.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Routes

- `auth` — signup / login.
- `chat` — chat completions.
- `documents` — document references for a session.
- `admin` — administrative operations.
- `health` — readiness / liveness.

## Container

```bash
docker build -t chat-service .
```

CI/CD is handled by `.github/workflows/chat-service.yml`: pull requests run
validation (SAST → SCA → image build → Trivy); image publish to ECR happens only
via `workflow_dispatch`.
