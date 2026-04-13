# Why slowquery-demo-backend?

## The obvious version

The obvious version of a demo for a query-analysis library is a Docker Compose file with a toy app and a Postgres container. Seed some data, run a few queries, point at the logs, and say "see, it works." The problem is that "see, it works" requires trust — the reader has to believe your screenshots are real and your setup is reproducible.

## Why I built it differently

This repo exists to make the observability pipeline reproducible on a live URL against a real seeded Postgres, with no faith required. The trick is Neon's branching: two branches, identical rows (100k orders, 500k order_items), one with missing indexes and one with them in place. The demo service installs the middleware and a traffic generator drives realistic commerce load through the endpoints. Within seconds of a burst, the live `/_slowquery/queries` surface holds real fingerprints with rolling p50/p95/p99 percentiles, captured EXPLAIN plans, and rule-engine suggestions like `CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(created_at)`. Every suggestion is a runnable DDL statement produced by a deterministic rule, not an LLM guess. The live URL is the proof — anyone can hit it and verify the claims without cloning anything.

## What I'd change if I did it again

The biggest lesson was that the library did not survive first contact with a real async engine. Four library-compatibility shims landed in `core/observability.py` because unit tests with mock sessions never touched the code paths where asyncpg cursors and SQLAlchemy's `AsyncAdapt_asyncpg_cursor` disagree with the library's assumptions. Shim 4 — a sync-hook to async-store bridge plus a direct EXPLAIN path using real captured parameters instead of the library's broken placeholder synthesizer — is the one that made fingerprints, plans, and suggestions actually populate. If I did it again, I'd test against a real async engine from day one instead of mocking. Mocks hide exactly the kind of integration seams that matter most, and discovering four shims late is four shims too many.
