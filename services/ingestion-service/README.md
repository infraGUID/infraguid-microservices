# ingestion-service

Document ingestion FastAPI service. Owns the database schema (creates tables and
the `pgvector` extension on startup) and runs an SQS consumer alongside the API
to process ingestion jobs asynchronously.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The SQS consumer (`app/worker.py`) starts automatically when a queue is
configured.

## Container

```bash
docker build -t ingestion-service .
```

CI/CD is handled by `.github/workflows/ingestion-service.yml`: pull requests run
validation (SAST → SCA → image build → Trivy); image publish to ECR happens only
via `workflow_dispatch`.
