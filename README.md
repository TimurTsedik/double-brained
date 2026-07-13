# Second Brain

Python implementation of Second Brain.

## Local identity enrollment

This prototype supports one local Telegram bootstrap-admin enrollment flow. It
accepts only private-chat messages through local polling; webhook mode and
capture/production ingestion are inactive.

### Start PostgreSQL

```bash
docker compose up -d postgres
uv sync --python 3.13
```

PostgreSQL is exposed only on `127.0.0.1:55432`. The compose service creates
the `second_brain` development database. Create a separate test database once:

```bash
docker compose exec postgres createdb -U second_brain second_brain_test
```

### Configure local environment

Create a local `.env` from `.env.example`; it is required for the identity
commands. Keep it local: never commit it, paste its values into issues, or put
tokens, peppers, or database credentials in logs.

```bash
cp .env.example .env
```

Set distinct database URLs and the three Telegram/invite secrets in `.env`.
These examples intentionally contain no secret values:

```dotenv
DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain
TEST_DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain_test
TELEGRAM_BOT_TOKEN=
INVITE_TOKEN_PEPPER=
INVITE_TOKEN_PEPPER_KEY_ID=
```

### Initialize and enroll

Initialize the development schema:

```bash
uv run --env-file .env second-brain-identity init-db
```

Resetting destroys all prototype data. Only run it when intended, with the
explicit confirmation flag:

```bash
uv run --env-file .env second-brain-identity reset-db --confirm-prototype-reset
```

Create the one-time bootstrap-admin invite after the bot has a Telegram
username. The command prints a sensitive enrollment link; share it only with
the intended administrator and do not retain it in logs or tickets.

```bash
uv run --env-file .env second-brain-identity create-bootstrap-admin-invite
```

Start local polling only after initialization. It polls only Telegram
`message` updates, handles only private chats, and refuses to start when a
webhook is configured.

```bash
uv run --env-file .env second-brain-local-polling
```

## Automated checks

```bash
DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain \
TEST_DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain_test \
uv run pytest -W error
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
