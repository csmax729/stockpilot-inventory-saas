# StockPilot v1.0 — Design package

This package specifies the planned **StockPilot multi-tenant order and inventory SaaS** before any application code has been written.

## Start here

1. **`TECHNICAL_DESIGN.md`** — full technical blueprint with requirements, Mermaid architecture/ER/state diagrams, business rules, transactions, security architecture, API conventions, testing, and implementation sequence. The full SQL schema is also included in Appendix A.
2. **`erd.svg`** (or `erd.png`) — rendered PostgreSQL ER diagram showing all 12 main entities.
3. **`schema.sql`** — proposed baseline DDL for PostgreSQL **16+**. Implement as Alembic migrations; do not run blindly against a database containing data.
4. **`openapi.yaml`** — OpenAPI 3.1 REST contract with 24 paths/30 operations and schemas. This is a target contract; verify it against FastAPI-generated OpenAPI during implementation.
5. **`erd.dot`** — editable Graphviz source for the ER diagram (`dot -Tsvg erd.dot -o erd.svg`).

## Design assumptions

- Internal B2B workflows, not a public storefront or payment gateway.
- One warehouse per order; whole-unit inventory; one currency per organization.
- Orders reserve stock at creation. Fulfillment decrements both on-hand and reserved; cancellation decrements reserved only.
- PostgreSQL is authoritative; Redis is an optional read cache.
- Use membership + tenant-scoped queries at all levels. PostgreSQL RLS is deferred and not enabled by baseline DDL.
- Never report benchmark results until measured.

## Validation limitations

The YAML file has been checked for syntax and local references, and the ER diagram has been rendered; these are **design assets**, not a functioning application. The PostgreSQL schema has not been executed against a running PostgreSQL server in this environment. Validate all DDL in a fresh Postgres 16+ container, run Alembic upgrade/downgrade tests, then test actual concurrency behavior before claiming production readiness.

## First coding milestone

Set up FastAPI, PostgreSQL, SQLAlchemy 2.x, Alembic, Docker Compose and Pytest; implement user registration/login, organization creation and current-member checks. Build inventory and order flows only after database migrations and tenant-isolation tests are passing.
