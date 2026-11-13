# StockPilot — Multi-Tenant Inventory & Order Management SaaS

StockPilot is an inventory and order management application for teams working in separate organizations. It tracks products and warehouses, reserves stock when orders are created, and records stock movements when inventory changes. Prices are stored in minor currency units; available stock is calculated as on-hand minus reserved.

## Start locally

Install and start Docker Desktop, then from the project directory run:

```sh
docker compose up --build
```

Open [http://localhost:5173](http://localhost:5173). Register an account, sign in, and create a workspace. Add a warehouse and a product, receive stock, and create an order. Managers and admins can fulfill an order or cancel it; staff can cancel orders they created. The API documentation is at [http://localhost:8000/docs](http://localhost:8000/docs).

The database is stored in the `postgres_data` Docker volume. `docker compose down` stops the services while preserving data. The defaults are for local development only. To customize them, copy `.env.example` to `.env` before starting Compose.

## Development and tests

The backend uses Python 3.12, FastAPI, SQLAlchemy and PostgreSQL 16+. From `backend`, install the locked dependencies with `pip install -r requirements.lock`, set `DATABASE_URL` and `SECRET_KEY`, run `alembic upgrade head`, then start `uvicorn app.main:app --reload`. The frontend uses Node 22; run `npm ci` and `npm run dev` in `frontend`. Vite proxies API requests to port 8000, or to `VITE_PROXY_TARGET` when set.

Dependency versions are recorded in `backend/requirements.lock` and `frontend/package-lock.json`. After changing Python requirements, regenerate the lock with `uv pip compile --universal --python-version 3.12 backend/requirements.txt -o backend/requirements.lock` from the repository root. After changing frontend dependencies, update the npm lockfile with `npm install` in `frontend`; commit the manifest and lockfile together.

The integration tests require a separate, migrated PostgreSQL database. Set `TEST_DATABASE_URL` to its SQLAlchemy URL and run `python -m pytest` from `backend`. When this variable is absent, the database integration tests skip locally; set `REQUIRE_POSTGRES_TESTS=1` to make missing configuration a test failure instead. CI sets this guard so missing PostgreSQL configuration cannot silently pass. The tests create uniquely named records and exercise tenant permissions, inventory transactions, idempotency and concurrent orders. Never point the tests at a production database. The frontend production build can be checked with `npm run build`. CI runs the migration and integration suite against PostgreSQL and builds the frontend.

## Configuration and deployment

The included Compose configuration and Vite server are intended for local development, not a production deployment. Before serving real users, configure HTTPS, backups, monitoring and appropriate secret management, and validate the security and concurrency tests against the intended environment. Production performance benchmarking remains deployment work.

For a deployment, use a managed PostgreSQL instance, run `alembic upgrade head` before starting the API, and serve the built frontend behind HTTPS with `/api` and `/health` routed to the API. Set `APP_ENV=production`, a unique random `SECRET_KEY` of at least 32 characters, `DATABASE_URL`, the exact HTTPS browser origin in `FRONTEND_ORIGIN`, and `COOKIE_SECURE=true`. Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and store it in your hosting provider's secret manager. Never commit real credentials or use the local defaults in production. Set up encrypted database backups and test restoration before relying on the service.

Authentication rate limits are stored in PostgreSQL and shared across API workers. Configure the reverse proxy and ASGI server to accept client IP information only from trusted proxies; otherwise requests may share the proxy's IP limit. Do not trust arbitrary forwarded headers from the public internet. Validate the limits and proxy configuration in the target environment.

The GitHub Actions workflow installs locked dependencies, migrates an isolated PostgreSQL service, runs the backend suite, and builds the frontend. It runs when changes are pushed or a pull request is opened; check its result on GitHub after the first push.

The original design package is in [docs](docs/TECHNICAL_DESIGN.md), including the reference schema, API contract and ER diagram. The API's live `/openapi.json` describes the implementation; the supplied contract is the design baseline and should be checked for differences before relying on it for an integration.
