# Development

```bash
cd /opt/projects/proxy-hub
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q
```

- Backend package: `backend/app` (run with cwd `backend/`: `../.venv/bin/uvicorn app.main:app`).
- Frontend: static files in `frontend/` — no build, no Node. Edit and refresh.
- Env for dev: copy `.env.example` to `.env` (git-ignored) or export vars.
- Conventions: routes stay thin (see `backend/app/api/`), logic lives in
  `backend/app/services/`; no shell commands outside `services/cores.py`;
  no secrets in logs/API (tests assert this for the subscription URL).
- Add a parser: extend `SCHEME_PARSERS` in `services/parsers.py` + fixtures in
  `tests/test_parsers.py`. One bad URI must never break a sync (test it).
