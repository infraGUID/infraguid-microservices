# agent-service

LangGraph ReAct DevOps agent exposing LangChain tools over Amazon Bedrock,
served as a FastAPI application.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Endpoints

- `GET /` — liveness probe returning service status.
- See `app/api/routes` for the agent invocation routes.

## Container

```bash
docker build -t agent-service .
```

CI/CD is handled by `.github/workflows/agent-service.yml`: pull requests run
validation (SAST → SCA → image build → Trivy); image publish to ECR happens only
via `workflow_dispatch`.
