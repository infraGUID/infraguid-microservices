# frontend

Static web client (HTML / CSS / JS) for InfraGuidAI, served by nginx.

## Pages

- `index.html` — main chat UI.
- `login.html` / `signup.html` — authentication.
- `admin.html` — admin console.

Assets live under `css/` and `js/`; nginx routing is configured in
`nginx.conf`.

## Container

```bash
docker build -t frontend .
```

CI/CD is handled by `.github/workflows/frontend.yml`: pull requests run
validation; image publish to ECR happens only via `workflow_dispatch`.
