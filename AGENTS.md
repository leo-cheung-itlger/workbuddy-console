# WorkBuddy Console

- This is the public, credential-free console source. Runtime data, private server inventory and credentials never belong here.
- Python app modules use the existing flat imports under `app/`; run tests with `PYTHONPATH=app` and the pinned upstream virtualenv.
- Keep upstream hawklithm/workbuddy2api unmodified. Do not copy AGPL Go implementation into this MIT console; use documented upstream interfaces or implement independent adapters.
- Preserve API key auth, HTTPS-only administrator sessions, CSRF, protocol streaming and old SQLite data during migration.
- Each assigned agent writes only its specified files. Root integrates modules and owns UI, deployment and documentation. No agent may push or deploy independently.
- Public defaults use `http://localhost` or `https://console.example.com`; deployment-specific values come from environment/state. Do not include personal domains, user IDs, email addresses or home paths.
