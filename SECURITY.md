# Security and publication boundaries

Never commit runtime state, account sessions, SMTP passwords, administrator
credentials, SQLite databases, actual service logs or private infrastructure data.
Use the ignored state directory, restricted file modes and systemd credentials.

All public `/v1/` requests require an active client key. Administrator sessions and
API keys are independent. The `/internal/` daemon endpoint is not part of the public
proxy allowlist; it additionally requires a separate key and direct loopback peer.
Do not forward arbitrary paths or expose upstream `/admin` directly.

This source was checked with Gitleaks 8.30.1 in redacted directory and Git modes.
The additional local generic-assignment heuristic was reviewed manually: its
matches were context-variable tokens, random-token constructors, interactive input
calls and form-field reads, not embedded credentials. Compiled Python files are
ignored and excluded from release archives. The public repository contains only
generic code, documentation, tests and deployment templates.

The application stores metadata only in its ledger. The upstream package may have
its own diagnostic logs; configure their retention separately and protect their
state directory. Read-only admin data still contains private account metadata and
must remain authenticated.

The independent watchdog consumes SMTP settings via systemd `LoadCredential` and
keeps separate incident state. It can report a stopped web process while the host
and outbound mail are working. Whole-host/network outages need an external monitor.
