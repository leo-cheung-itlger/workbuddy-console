# Upstream and benchmark attribution

This console is an independent MIT-licensed wrapper around the unmodified
[hawklithm/workbuddy2api](https://github.com/hawklithm/workbuddy2api) Python project,
pinned to `d5847723dc95474e2ab56e2aadb2bd1f32cc30ca` (2.0.7).
That project is MIT licensed, Copyright (c) 2026 Mayer. Its license must accompany
any distribution containing the upstream package; it is retained in installations.
The console calls its account store, authentication and protocol adapters.

Earlier personal deployments used the separate
[yreidev/workbuddy2api-panel](https://github.com/yreidev/workbuddy2api-panel) Go
program for check-in. Its protocol documentation informed investigation. No Go
source or rewritten Go implementation is incorporated or distributed here.
The Go project has its own AGPL-3.0-only / NOTICE terms. This console does not
require it: one Python account store owns inference and check-in credentials.

Benchmark updates fetch the official [LiveBench](https://livebench.github.io/)
public leaderboard and category definitions only when the administrator requests
an update. See [LiveBench source](https://github.com/LiveBench/livebench.github.io)
and [methodology](https://github.com/LiveBench/LiveBench). Source/date/model variant
are retained at runtime; benchmark datasets are not bundled in this repository.
The displayed coding/agentic aggregates are means of the source task scores;
the general aggregate is the mean of its available category means. They are
reference comparisons, not a measurement of this gateway.

[Artificial Analysis](https://artificialanalysis.ai/data-api/docs/reference)
can be used as an optional manually imported attributed source. No access key or
copied dataset is bundled. Users must have the appropriate rights for imported
data. Exact model variant and reasoning effort mappings remain explicit.
