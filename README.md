# Kash

**Kash** is a self-hosted personal finance and budgeting app built with Python, FastAPI, Jinja2, HTMX, Tailwind CSS, DaisyUI, and SQLite or PostgreSQL. It has a mobile-first layout with a responsive desktop view.

## Features
- **Dashboard**: Balances, net worth, month-to-date income and expenses (compared with the same point last month), budget progress, upcoming bills, and recent transactions.
- **Accounts**: Checking, savings, credit cards, cash, investments, and loans. Bank-linked accounts take their balance from the bank; manual accounts keep a running balance.
- **Transactions**: Search, filters, manual entry, quick "make a rule from this" flow, and CSV export.
- **Categories**: Two-level groups and categories with icons, colors, and hide/transfer flags.
- **Rules engine**: Automatic categorization and description cleanup with nested and/or conditions, safe regex, and a live preview before saving.
- **Bills**: Upcoming due dates, monthly paid/left-to-pay progress, automatic bill detection with review, and payment matching.
- **Budgets**: Monthly per-category budgets.
- **Reports**: Income, spending, and savings rate by category for any month or date range, income vs. expenses by month, and net worth over time.
- **Settings**: Display currency, number/date format, and Light / Dark / Follow-system theme.
- **Notifications**: Push alerts to the installed app for overdue and upcoming bills, budgets, transactions needing a category, bank sync problems, large transactions and low balances. Each person chooses their own (needs HTTPS).
- **Bank sync**: SimpleFIN Bridge, with credentials encrypted at rest (AES-256-GCM); optional automatic sync at fixed hours.
- **Login**: One household per instance, with a login for each person: the owner invites others by a one-time link and they share the same data (Argon2id passwords, secure session cookies); password reset by link or from the server console.

## Documentation
- [Project Plan & Roadmap](docs/PROJECT_PLAN.md)
- [Feature Specifications](docs/FEATURES.md)
- [System Architecture](docs/ARCHITECTURE.md)
- [Database Schema & Data Model](docs/DATA_MODEL.md)
- [Decision Records](docs/DECISIONS.md)
- [Progress Log](docs/PROGRESS.md)
- [Testing Strategy](docs/TESTING.md)
- [Deploying with Docker](docs/DEPLOYMENT.md)

## Quick start (development)
```bash
git clone https://github.com/Fahmula/kash.git
cd kash

uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env   # for local use, uncomment the "Local development" lines
uvicorn kash.main:app --reload --port 8000
```
Open [http://localhost:8000](http://localhost:8000). On first start Kash creates the database tables and a default set of categories and rules, and asks you to create your account.

To run it on a server with Docker and PostgreSQL (including automatic bank sync every 4 hours), see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Run the tests with `pytest` (they use a throwaway SQLite database, never your real one).

### Styles (Tailwind CSS)
The compiled `src/kash/web/static/css/tailwind.min.css` is committed, so running Kash needs no Node or build step. After adding or changing Tailwind classes in templates, rebuild it:
```bash
./scripts/build_css.sh          # one-off build (downloads the standalone Tailwind CLI once into .cache/)
./scripts/build_css.sh --watch  # rebuild while you edit
```
`tests/test_css_build.py` fails if a template uses a class that isn't in the built file.

### Database changes
Migrations live in `src/kash/db/migrations/` and run automatically at startup. After changing a model:
```bash
alembic revision --autogenerate -m "describe the change"
```
Review the generated file and commit it; `tests/test_migrations.py` fails if models and migrations differ.

## License
MIT. See [LICENSE](LICENSE).
