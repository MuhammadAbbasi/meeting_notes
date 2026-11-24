# CLAUDE.md

Read `AGENTS.md` (how to work here) and `CONTRACTS.md` (normative wire/API contracts) before changing code.
`README.md` is the user-facing manual; keep it accurate when behaviour changes.

Quick checks before claiming anything works:

```powershell
.venv\Scripts\python -m pytest -q          # must stay green; add a test next to the code you touch
docker compose config --quiet              # after any compose/.env.example edit
```

Conventions: contract first, mock transcriber in tests, no new hub dependencies, settings in
`server/settings.py`, secrets masked, `# ponytail:` comments on deliberate shortcuts, conventional
commit prefixes. Production facts (hosts, ports in use, GPU sharing, SSH limits) are in `AGENTS.md`.
