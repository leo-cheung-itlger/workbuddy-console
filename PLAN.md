# Console v2 implementation

Approved scope: a unified account registry with independent API/check-in flags; one credential owner; sticky sessions plus round-robin, least-in-flight, earliest-expiry and fixed routing; API key management; request account/reasoning/cache metadata; expiry overview; SMTP alerts and recovery; manual benchmark import and recommendations; 12-hour catalog cache. Preserve existing public API and administrator password, and migrate existing ledger without data loss.

1. Prepare a clean Git source tree from console-owned files, retaining upstream attribution. No runtime data is copied.
2. Independent modules: account routing/check-in; telemetry/client-key authentication; SMTP alerting. Root integrates the console API, model metadata, benchmarks and responsive UI.
3. Focused tests exercise credential ownership, disabled flags, session affinity, key revocation, cache/reasoning metadata, old database migrations, alert de-duplication and masked settings. Preserve streaming frame bytes.
4. Stage on N1 with backups. Migrate the current single account and preserve API/console credentials. Switch daily check-in to unified registry and stop old Go check-in scheduling only after new check-in verifies success. Retain the old Go binary/state for rollback.
5. Verify real ordinary/SSE inference, console login and mobile layouts, missing/wrong key refusal, valid key/revocation, expiry data and manual benchmark matching. No paid automatic model probes. Send one explicitly authorized SMTP test email.
6. Run a redacted pre-publication leak scan; commit and push only the generic source to the user's specified public repository. Record deployed commit and rollback instructions.

Mail policy: default passive API/service monitoring, check-in failure retries, no recurring inference probe. Distinguish rate limiting, network errors, credential expiry and explicit restrictions; no speculative ban claims. Send once per incident, re-notify after a cooldown, and send recovery once.

Benchmarks: manual update only, retain source/date/model variant/reasoning and uncertain mappings. Prefer coding/agentic metrics and keep general scoring separate. Unknown matches stay unknown. WorkBuddy credits measured locally are separate from external API dollar prices; never divide by zero to manufacture a value score.
