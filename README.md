# Second Brain

Python implementation of Second Brain.

## Local identity enrollment

This prototype supports one local Telegram bootstrap-admin enrollment flow. It
accepts only private-chat messages and callback buttons through local polling;
webhook mode and production ingestion are inactive.

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

Set four distinct database URLs and the three Telegram/invite secrets in `.env`.
`DATABASE_URL` and `TEST_DATABASE_URL` must use the non-superuser
`second_brain_app` role. `SCHEMA_DATABASE_URL` and
`TEST_SCHEMA_DATABASE_URL` are owner-only URLs used for explicit schema
initialization and isolated test schemas. These examples intentionally contain
no secret values:

```dotenv
DATABASE_URL=postgresql+asyncpg://second_brain_app@127.0.0.1:55432/second_brain
TEST_DATABASE_URL=postgresql+asyncpg://second_brain_app@127.0.0.1:55432/second_brain_test
SCHEMA_DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain
TEST_SCHEMA_DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain_test
TELEGRAM_BOT_TOKEN=
INVITE_TOKEN_PEPPER=
INVITE_TOKEN_PEPPER_KEY_ID=
```

### Initialize and enroll

This slice changes the prototype database-role contract, the allowed Telegram
receipt results, the pending-capture schema, and the allowed `tasks.status`
values. **A database created by an earlier prototype version must be reset
before running the actionable task bot.** `init-db` does not alter existing
constraints or rename the former pending-task table. The reset destroys all
local prototype data:

```bash
uv run --env-file .env second-brain-identity reset-db --confirm-prototype-reset
```

For an empty database, initialize the development schema instead:

```bash
uv run --env-file .env second-brain-identity init-db
```

Create the one-time bootstrap-admin invite after the bot has a Telegram
username. The command prints a sensitive enrollment link; share it only with
the intended administrator and do not retain it in logs or tickets.

```bash
uv run --env-file .env second-brain-identity create-bootstrap-admin-invite
```

Open the new one-time link in the private chat to enroll again. Then send
`/start` to receive the capture panel. Its `📋 Мои задачи` button opens the
actionable task list. Resetting the prototype database removes the previous
Telegram identity, so re-enrollment is required.

Start local polling only after initialization. It polls only Telegram
`message` and `callback_query` updates, handles only private chats, and
refuses to start when a webhook is configured.

```bash
uv run --env-file .env second-brain-local-polling
```

### Task panel delivery note

For this local prototype, `/start` first commits its redacted receipt and then
sends the one visible panel. While the poller remains running, a failed panel
send is retried before its Telegram offset advances. There is intentionally no
outbox yet: if the process crashes after the receipt commits but before the
panel reaches Telegram, a restart treats that old update as a duplicate and
does not resend it. Send `/start` again to request a new panel.

## Automated checks

```bash
uv run --env-file .env pytest -W error
uv run --env-file .env ruff check .
uv run --env-file .env ruff format --check .
uv run --env-file .env mypy src
```
