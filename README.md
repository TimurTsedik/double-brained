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

This slice changes the prototype database-role contract, Telegram voice
attachments, processing/transcript state, the allowed Telegram receipt
results, pending-capture and pending-search schemas, the allowed
`tasks.status` values, and full-text indexes. **A database created by an
earlier prototype version must be reset before running the current bot and
voice worker.** `init-db` does not alter existing constraints or rename former
tables. The reset destroys all local prototype data:

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
`/start` to receive the capture panel. `📋 Мои задачи` opens the actionable
task list. `🔎 Поиск` makes the next private text a one-shot exact search across
your Notes, Tasks, Ideas, Decisions, and Questions instead of saving it as new
content. Resetting the prototype database removes the previous Telegram
identity, so re-enrollment is required.

Start local polling only after initialization. It polls only Telegram
`message` and `callback_query` updates, handles only private chats, and
refuses to start when a webhook is configured.

```bash
uv run --env-file .env second-brain-local-polling
```

## Local voice transcription

Voice messages are downloaded through the Telegram Bot API and transcribed on
the local machine with `mlx-whisper`. Audio and transcript text are not sent to
an inference API. The current adapter works with supported MLX installations
on Apple Silicon macOS and Linux; the exact Linux MLX CPU/CUDA package depends
on the production hardware and is intentionally not guessed by this local
prototype.

Install FFmpeg before starting the worker. On macOS with Homebrew:

```bash
brew install ffmpeg
```

The defaults in `.env.example` store controlled audio below `.data/voice` and
use `mlx-community/whisper-large-v3-turbo`. The first transcription downloads
and caches roughly 1.6 GB of model weights. A smaller compatible model can be
selected with `MLX_WHISPER_MODEL`.

Run polling and transcription as two separate long-running processes:

```bash
# terminal 1: receive and durably queue Telegram updates
uv run --env-file .env second-brain-local-polling

# terminal 2: download, store, and transcribe queued voice messages
uv run --env-file .env second-brain-local-voice-worker
```

The current capture button determines whether the transcript becomes a Note,
Task, Idea, Decision, or Question; without a preceding type selection it
becomes a Note. Telegram receives only fixed processing statuses, never a copy
of personal transcript content:

```text
🎙️ Голос сохранён. Расшифровываю…
🎙️ Расшифровано и сохранено: 📝 Заметка.
```

Processing retries twice after the initial attempt. After the third failure,
the bot sends one failure status with the safe root Trace ID. Originals remain
namespaced by the internally resolved `UserSpace`; neither a command-line
argument nor an environment variable can choose another user's space.

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
