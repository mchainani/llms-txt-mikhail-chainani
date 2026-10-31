# llms-txt-gen

The Python API and background workers live in [`backend/`](backend/).
See the [backend README](backend/README.md) for setup, Docker, database, and testing instructions.

The React SPA lives in [`frontend/`](frontend/). See the [frontend README](frontend/README.md) for setup, API proxy configuration, and production hosting.

```bash
cd frontend
pnpm install
pnpm dev
```

Run backend commands from `backend/`, using its virtual environment:

```bash
cd backend
source .venv/bin/activate
python -m pytest -q
```
