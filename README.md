# Second Brain

Your personal memory in Telegram: send a note or a voice message and it is saved,
organized, and searchable — then ask questions in plain language and get grounded
answers with sources.

## Using the bot

**First time.** Open your one-time enrollment link, press **Start**, and choose a
language (🇷🇺 Русский / 🇬🇧 English). Everything after that is in your language;
switch anytime with **🌐 Язык / Language**. Send `/start` to open the panel.

**Two independent things — don't mix them up:**

- **Type** = *what* the record is: **📝 Note**, **✅ Task**, **💡 Idea**,
  **⚖️ Decision**, **❓ Question**. The default is **Note**. Press a type button,
  then send your text or voice, and it is saved as that type.
- **Project** = *where* it goes. Press **📁 Projects** to create/select/clear a
  project. The selected project becomes *current* and stays current (sticky) until
  you switch or clear it — everything you capture meanwhile is attached to it.

They stack. Select **Project X**, press **✅ Task**, then dictate → a *Task in
Project X*. Just dictate after selecting a project (no type button) → a *Note in
Project X* (Note is the default). A voice message is always a record; you cannot
name a project by voice — project names are typed.

**Capturing.** Just send text or a voice message. Voice is transcribed
automatically and the transcript becomes the record. Telegram only ever shows you
fixed status lines, never a copy of your content.

**Photos.** Send a photo **with a caption** and the caption becomes an ordinary
record (type button / time-in-text / default Note all work as usual); the
original image is downloaded and stored immutably next to it. Opening the
record in full sends the photo itself right after the text (Telegram `file_id`
as the fast path, the stored bytes as the fallback), and search results,
digests, and "similar" lists mark such records with 📷. Send a photo **without
a caption** and the bot honestly replies "📷 Сохранено": the image and the
capture journal are kept, but no record is invented on your behalf — the photo
will become searchable when the on-device OCR layer arrives.

**Links.** Hyperlinks in your message (both a word hiding a URL and a bare URL)
are preserved alongside the record — the text itself stays exactly as you sent
it. When you open a record in full, a "🔗 Links" block appears under the text,
and bare URLs gain the page's title once the bot has quietly fetched it in the
background.

**Editing.** Open a record in full and press "✏️ Править" — your next message
replaces the record's text (a note's text, a task's title). The capture journal
keeps the original forever; the record's header gains "(изменено)". The new
text is re-indexed for search and its links block is rebuilt, but nothing else
is re-run: the type is not re-classified and no time is extracted from the new
text — an existing reminder stays exactly where it was (the confirmation says
"⏰ напоминание осталось на …" for a task with a live reminder). Want a new
time — create a new task.

**Reminders.** Type the time right inside a task — nothing special to press. If
the task text names an explicit clock time in the future, the bot confirms
"⏰ Напомню …" and messages you "⏰ Напоминание: …" at that moment (in your
space's timezone). Examples, relative and absolute, RU and EN:

- «Позвонить в банк завтра в 10:00»
- «Купить билеты через 2 часа»
- «Отчёт 20 июля в 9:00»
- "Call the bank tomorrow at 10am"

A date without a clock time («завтра», "tomorrow", «20 июля») is just a task — no
reminder, no error. Completing a task cancels its pending reminder.

**Finding things — two ways:**

- **🔎 Search** — exact word search across your Notes, Tasks, Ideas, Decisions,
  and Questions. Fast and literal. The next message you send is treated as the
  search query (not saved as new content).
- **🧠 Ask memory** — ask a question in plain language. The bot finds the relevant
  pieces *by meaning* (not just exact words) and answers, leading with a
  confidence badge — **✅ Straight from notes**, **🧩 Pieced together**,
  **💭 A guess**, or **∅ Nothing in memory** — and the stored records it used as
  sources. It says "nothing in memory" instead of inventing an answer.

That is the whole loop: capture freely, organize with types and projects, and ask
your memory when you need it.

**Inviting someone.** Each person gets their own private memory — nobody sees
anyone else's notes. If you are the admin, press **➕ Invite** in the panel: the
bot replies with a one-time link. Send that link to the person you want to add;
when they open it, they get their own empty space and pick their language. The
link works for whoever opens it first and expires in 24 hours, so share it only
with the intended person. If you ever lose it, just press **➕ Invite** again.

## Local identity enrollment

The first person to enroll (via the bootstrap link below) becomes the **admin**.
The admin can invite additional **members** from the bot with **➕ Invite**; each
member gets an isolated space and cannot see other people's data or invite anyone
else. Enrollment accepts only private-chat messages and callback buttons through
local polling; webhook mode and production ingestion are inactive.

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

Set four distinct database URLs, the three Telegram/invite secrets, and the
OpenRouter API key in `.env`. `DATABASE_URL` and `TEST_DATABASE_URL` must use
the non-superuser `second_brain_app` role. `SCHEMA_DATABASE_URL` and
`TEST_SCHEMA_DATABASE_URL` are owner-only URLs used for explicit schema
initialization and isolated test schemas. `OPEN_ROUTER_AI_KEY` holds your
OpenRouter API key; it is the only place the key is stored — the code reads it
from this environment variable (`bootstrap/settings.py`) and never hardcodes or
logs it, so it is not in Git and must be set anew on any other host. These
examples intentionally contain no secret values:

```dotenv
DATABASE_URL=postgresql+asyncpg://second_brain_app@127.0.0.1:55432/second_brain
TEST_DATABASE_URL=postgresql+asyncpg://second_brain_app@127.0.0.1:55432/second_brain_test
SCHEMA_DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain
TEST_SCHEMA_DATABASE_URL=postgresql+asyncpg://second_brain@127.0.0.1:55432/second_brain_test
TELEGRAM_BOT_TOKEN=
INVITE_TOKEN_PEPPER=
INVITE_TOKEN_PEPPER_KEY_ID=
OPEN_ROUTER_AI_KEY=
```

### Initialize and enroll

This slice changes the prototype database-role contract, Telegram voice
attachments, processing/transcript state, the allowed Telegram receipt
results, pending-capture and pending-search schemas, the allowed
`tasks.status` values, classification steps/results, and full-text indexes.
**A database created by an earlier prototype version must be reset before
running the current bot and worker.** `init-db` does not alter existing
constraints or rename former tables. The reset destroys all local prototype
data:

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
`/start` to receive the capture panel. `📋 My tasks` opens the actionable
task list. `🔎 Search` makes the next private text a one-shot exact search across
your Notes, Tasks, Ideas, Decisions, and Questions instead of saving it as new
content. Resetting the prototype database removes the previous Telegram
identity, so re-enrollment is required.

Start local polling only after initialization. It polls only Telegram
`message` and `callback_query` updates, handles only private chats, and
refuses to start when a webhook is configured.

```bash
uv run --env-file .env second-brain-local-polling
```

### Telegram webhook + Postgres INBOX (epic API-1, slice B1)

The FastAPI app also exposes `POST /telegram/webhook` (static path). The route
validates the `X-Telegram-Bot-Api-Secret-Token` header against
`TELEGRAM_WEBHOOK_SECRET` (empty secret → the route answers 503 and the
webhook door stays closed), caps the request body at
`WEBHOOK_MAX_BODY_BYTES`, and does exactly ONE thing: an idempotent INSERT of
the raw update into the `telegram_update_inbox` table (unique per
`bot_id + update_id`, so Telegram retries collapse into one row). No
processing happens inside the HTTP request, and request bodies are never
logged.

The body cap holds without a `Content-Length` header too: the route reads the
request stream chunk by chunk and answers 413 as soon as the accumulated size
passes the cap, so a chunked upload cannot walk around the limit by filling
memory. Being the only door facing the internet, the route also runs the same
fail-closed database role check as local polling, the worker, and the identity
CLI — once, while it lazily builds its runtime, before the first INSERT. A
privileged (owner/superuser/`BYPASSRLS`) `DATABASE_URL` is a configuration
error, not a disabled webhook: the route fails with 500 and the reason in the
server log instead of answering the misleading "not configured" 503.

Processing is an *inbox step* of the existing voice worker cycle: it claims
its bot's inbox rows strictly in `update_id` order (one row per transaction,
touching only that bot's pending head — never the full table), runs each
through the same normalization → `LocalUpdateProcessor` → `TelegramPresenter`
pipeline as polling (replies are byte-for-byte identical, including the
best-effort callback ack and the delayed panel follow-up), and marks the row
`done`. Failures retry with a linear backoff (`INBOX_RETRY_BACKOFF_SECONDS`)
up to `INBOX_MAX_ATTEMPTS`, then the row becomes `failed`; a failed head does
not block the rest of the queue, but a pending head waiting for its backoff
does (strict order matters more than throughput because of pending modes).
`PostgresTelegramInboxQueue.read_status` reports pending/failed depth and the
age of the head row for monitoring. Polling remains the working door until
the cutover (slice B3); the reverse proxy and rate limits arrive with B2.

## Local voice transcription and OpenRouter classification

Voice messages are downloaded through the Telegram Bot API and transcribed on
the local machine with `faster-whisper`. Audio is not sent to an inference API.
After transcription, the current source text is sent to OpenRouter for
structured classification; the request contains no Telegram ID, internal User
or UserSpace ID, trace ID, history, or other records. `faster-whisper` (with
`ctranslate2`) is cross-platform, so voice transcription works on both Apple
Silicon macOS and the Linux server.

Install FFmpeg before starting the worker. On macOS with Homebrew:

```bash
brew install ffmpeg
```

The defaults in `.env.example` store controlled audio below `.data/voice` and
use the `small` Whisper model. The first transcription downloads and caches the
model weights once. A different model can be selected with `WHISPER_MODEL`.

Photo originals are downloaded by the same worker process (an `image_download`
step of the shared processing cycle) into `IMAGE_STORAGE_ROOT`
(default `.data/images`), keyed `{space}/{capture}/original.<ext>` with a
sha256 checksum; the file type is sniffed from the bytes (JPEG/PNG/WebP
whitelist) and downloads above `IMAGE_MAX_FILE_SIZE_BYTES` are refused softly.

Set `OPEN_ROUTER_AI_KEY` in `.env`. Classification asks OpenRouter to try these
strict-structured-output models in order:

1. `nvidia/nemotron-3-super-120b-a12b:free`;
2. `openai/gpt-oss-20b:free`.

The selected Free providers may log prompts and outputs. This personal
prototype consciously accepts that policy and must not be used to capture
secrets. Before the model call, a deterministic scanner blocks common
credentials. Model output remains untrusted: only exact source quotes with a
valid type/modality pair and confidence of at least `0.90` may create
additional records. The original record selected with the Telegram button is
never replaced.

Run polling and the combined voice/classification worker as two separate
long-running processes:

```bash
# terminal 1: receive and durably queue Telegram updates
uv run --env-file .env second-brain-local-polling

# terminal 2: transcribe voice locally and classify text through OpenRouter
uv run --env-file .env second-brain-local-voice-worker
```

The current capture button determines whether the transcript becomes a Note,
Task, Idea, Decision, or Question; without a preceding type selection it
becomes a Note. Telegram receives only fixed processing statuses, never a copy
of personal transcript content:

```text
🎙️ Voice saved. Transcribing…
🎙️ Transcribed and saved: 📝 Note.
```

Processing retries twice after the initial attempt. After the third failure,
the bot sends one generic failure status with the safe root Trace ID.
Successful classification is silent. Originals remain namespaced by the
internally resolved `UserSpace`; neither a command-line argument nor an
environment variable can choose another user's space.

### Task panel delivery note

For this local prototype, `/start` first commits its redacted receipt and then
sends the one visible panel. While the poller remains running, a failed panel
send is retried before its Telegram offset advances. There is intentionally no
outbox yet: if the process crashes after the receipt commits but before the
panel reaches Telegram, a restart treats that old update as a duplicate and
does not resend it. Send `/start` again to request a new panel.

## Deploys and schema changes (production)

Every push to `main` deploys automatically (`.github/workflows/deploy_vps_manual.yml`):
tests and the GHCR image build run in parallel, then the VPS redeploy runs
`scripts/deploy_prod.sh`. That script does a short cutover on **every** deploy —
stop `polling`+`worker`, run `init-db`, bring everything back up on the new image.
The stop is required because `init-db` briefly REVOKEs and re-GRANTs the app
role's privileges, so a running bot would hit permission errors mid-reconcile.
`init-db` is idempotent and ADD-only, so on a release with no schema change it is
a near-instant no-op; the cost is a few seconds of downtime per deploy. A
schema-adding release therefore needs no manual step — the columns are grown in
place before the new code starts. (First-time initialisation — creating the role
and owner enrollment — is still manual, once; the script's guard aborts on an
uninitialised database.)

**Data-meaning changes still need a one-time manual command.** `init-db` only
adds structure; it never rewrites rows. When a release changes what EXISTING data
means, run the fix-up once by hand. The button-authority release is the current
example: an explicit type button is now consumed by DELETING its
`pending_capture_selections` row, so a leftover `'note'` row from an older build
reads as an explicit "Note" (suppressing the time→reminder default) until the
owner's next message clears it. Reset it once, using the schema-owner DSN from
`.env` and `psql` inside the `postgres` service:

```bash
set -a; . ./.env; set +a
docker compose -f docker-compose.prod.yml run --rm -T postgres \
  psql "${SCHEMA_DATABASE_URL/+asyncpg/}" -c "DELETE FROM pending_capture_selections;"
```

(Skip it and the glitch self-heals after one message per space.)

## Automated checks

```bash
uv run --env-file .env pytest -W error
uv run --env-file .env ruff check .
uv run --env-file .env ruff format --check .
uv run --env-file .env mypy src
```
