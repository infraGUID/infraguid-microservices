# rag-service

Retrieval-augmented generation FastAPI service: embeds queries, retrieves
relevant chunks from `pgvector`, and generates grounded answers using LangChain
AWS over Amazon Bedrock.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Endpoints

See `app/api/routes` for the retrieval and generation routes; RAG logic lives in
`app/rag`.

## Container

```bash
docker build -t rag-service .
```

CI/CD is handled by `.github/workflows/rag-service.yml`: pull requests run
validation (SAST → SCA → image build → Trivy); image publish to ECR happens only
via `workflow_dispatch`.
