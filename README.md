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

**Reaching your memory from outside Telegram.** Press **🔑 API** in the panel to
manage your own access tokens — everyone has this button, because a token opens
*your* memory and nobody else's. The panel lists your tokens (label, state, when
each was created and last used) with **➕ New token** above them and a
**🗑 Revoke** button next to every live one. A new token is shown **once**, in a
single message: the bot stores only its hash and cannot ever show it again, so
save it somewhere safe (a password manager) and delete that message from the
chat. Lost it — issue a new one. Revoking is immediate and permanent for that
token; the row stays in the list as history.

## Local identity enrollment

The first person to enroll (via the bootstrap link below) becomes the **admin**.
The admin can invite additional **members** from the bot with **➕ Invite**; each
member gets an isolated space and cannot see other people's data or invite anyone
else. Enrollment accepts only private-chat messages and callback buttons, and
this section describes running the bot **locally, through polling**; production
receives the same updates through the webhook instead (see "Telegram webhook in
production" under Deploys).

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

Set four distinct database URLs, the three Telegram/invite secrets, the two
API-token secrets, and the OpenRouter API key in `.env`. `API_TOKEN_PEPPER` and
`API_TOKEN_PEPPER_KEY_ID` salt the **API access tokens** issued from the 🔑 API
button and are deliberately **separate** from the invite pepper: rotating the
invite pepper must not log out every issued API token, and rotating the API
pepper (bump `API_TOKEN_PEPPER_KEY_ID` with it) invalidates every issued token
at once without touching enrollment. Both are required — the app refuses to
start without them rather than silently salting tokens with someone else's
secret. `DATABASE_URL` and `TEST_DATABASE_URL` must use
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
API_TOKEN_PEPPER=
API_TOKEN_PEPPER_KEY_ID=
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

That last rule now bites in practice: the production bot token has a webhook
registered, so local polling on **the same token** refuses to start (and would
fight production for the single allowed update consumer anyway). Use a separate
test bot token for local work, or delete the webhook first and accept that
production stops receiving updates until it is restored.

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
`second-brain-inbox-status` prints that depth, the age of the head row and
Telegram's own view of the delivery — see "Looking at the queue" below. **The
webhook is the working door in
production** (cutover done, slice B3): the route is served by the `api` service
and reached over HTTPS through the server's shared traefik, with a per-IP rate
limit and a proxy-side body cap on top of the app's own — see "The `api` service
goes out through the server's SHARED traefik" under Deploys below. `polling` is
kept only as a rollback path — see "Telegram webhook in production" below.

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
stop the app services, run `init-db`, bring everything back up on the new image,
and wait for `worker` and `api` to report healthy. `polling` is not part of that
cycle any more (see "Telegram webhook in production" below): a plain `up -d` does
not start it and the script does not wait for it.
The stop is required because `init-db` briefly REVOKEs and re-GRANTs the app
role's privileges, so a running bot would hit permission errors mid-reconcile.
`api` is stopped for the same reason: a webhook arriving mid-reconcile would fail
its INSERT on permissions, whereas a stopped `api` simply does not answer and
Telegram retries the update after the cutover.
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

### The `api` service goes out through the server's SHARED traefik (epic API-1, slice B2)

**Do not install a reverse proxy for this project. There already is one, and it
is not ours.** Ports 80 and 443 on the VPS belong to a single `traefik` container
that lives in its own compose project (`/docker/traefik`, host network,
entrypoints `web:80` + `websecure:443`, global 80→443 redirect, `letsencrypt`
HTTP-01 resolver, docker provider with `exposedbydefault=false`). It has been
running for months and it also fronts a **second, unrelated project of the
owner** on the same machine. Adding a Caddy/nginx/second traefik would fight for
those ports and take the neighbour down with it.

`api` therefore attaches to that traefik purely through labels in
`docker-compose.prod.yml` — `traefik.enable=true`, a `Host(...)` router on the
`websecure` entrypoint, `tls.certresolver=letsencrypt`, and a
`loadbalancer.server.port`. Nothing else is needed and nothing else may be added.

* **Host name: `yousaid.srv1492259.hstgr.cloud`.** The ROOT name
  `srv1492259.hstgr.cloud` is **taken by the neighbouring project** — claiming it
  would break a working bot. The wildcard `*.srv1492259.hstgr.cloud` already
  resolves to this server's IP, so the sub-name needs no DNS work and Let's
  Encrypt issues its certificate over HTTP-01 on first request.
* **No `ports:` section, on purpose.** traefik runs in the host network and
  dials the container's bridge IP directly, so nothing needs publishing. A
  published port would be a second, plaintext door into the webhook —
  bypassing TLS, the rate limit and the body cap below.
* **Rate limit:** 10 req/s sustained, burst 20, per client IP (traefik's default
  source criterion is the remote address, and on the host network that is the
  real client IP). Real traffic is a handful of small requests per minute — the
  owner's Telegram updates plus the future `/v1` — so the limit is invisible to
  legitimate use, including a retry burst after a restart, while floods and
  secret-guessing are cut to a trickle.
* **Body cap:** `buffering.maxRequestBodyBytes=2097152` (2 MiB). This is a
  SECOND line of defence, not a replacement for the app's
  `WEBHOOK_MAX_BODY_BYTES` (1 MiB by default), which still owns the normal 413.
  It sits deliberately above the app cap: the app stays the authority on what a
  valid update may weigh, and traefik only refuses the absurd before it reaches
  a python process.

  Note the interaction, because it is easy to misread: `buffering` reads the
  whole body before proxying, so behind this proxy the route's own incremental
  read never sees a body larger than 2 MiB, and the memory a request can cost is
  capped by traefik rather than by the route. The route's streaming cap still
  matters — it is what protects any deployment reached without this proxy, and
  it is the behaviour the tests pin — but in production traefik is the first
  wall, not the route.
* **Port:** uvicorn listens on `API_PORT` (default `8000`) inside the container —
  an in-network port, never published. Set `API_PORT` in the server's `.env` to
  change it; the command, the healthcheck and the traefik label all follow that
  one variable.
* **Health:** the container healthcheck is an HTTP `GET /health` (via python —
  the runtime image has no curl), not the `second-brain-healthcheck` database
  ping used by `polling`/`worker`. `/health` does not touch the database, and
  the webhook route builds its DB runtime lazily on the first update, so a
  database ping would report on the wrong subject for this service.

### Telegram webhook in production (epic API-1, slice B3)

**The bot receives updates through the webhook. Nothing calls `getUpdates`.**
Telegram delivers every update to `POST /telegram/webhook` on
`https://yousaid.srv1492259.hstgr.cloud`, the `api` service writes it into
`telegram_update_inbox`, and the `worker`'s inbox step processes it. The webhook
is registered with `max_connections=1` and
`allowed_updates=["message","callback_query"]`.

**`polling` still exists in `docker-compose.prod.yml`, behind the `rollback`
profile, and is the rollback path — do not delete it.** Telegram allows exactly
ONE update consumer: a polling container started next to a live webhook gets
`409 Conflict` on every `getUpdates` and, with `restart: unless-stopped`, spins
in a restart loop. The profile is what prevents that — a service in a non-active
profile is not started by a plain `up -d`, so a deploy cannot resurrect it by
accident, while `--profile rollback up -d polling` still brings it back in one
command.

Two keys live in the server's `.env` (never in Git) for this:

| Key | What it is |
| --- | --- |
| `TELEGRAM_WEBHOOK_SECRET` | Random secret compared against the `X-Telegram-Bot-Api-Secret-Token` header on every delivery. Empty → the route answers 503 and the door is closed. Also passed to `setWebhook`; it lives only in the header, never in the URL. |
| `APP_IMAGE_POLLING_FALLBACK` | Pinned image reference of the last build whose polling is known to work (e.g. `ghcr.io/timurtsedik/double-brained:sha-02f9906`). Read by nothing automatic — it exists so the rollback below does not have to guess a tag at 3 a.m. |

#### Looking at the queue

The webhook door can stand still without anything shouting: updates keep
landing in `telegram_update_inbox` and simply stop being processed, or Telegram
stops being able to reach us at all. One command on the server answers both
halves — what the queue holds, and what Telegram thinks of the delivery:

```bash
docker compose -f docker-compose.prod.yml run --rm worker second-brain-inbox-status
```

(`worker` is used only as a container to run in — it needs the same image and
`.env`, and the command touches nothing the running worker owns. `--rm` leaves
no container behind.)

```text
Telegram inbox queue (bot 8154739021)
  waiting to be processed (pending): 0
  gave up permanently (failed):      0
  head of the queue waiting:         -
  stuck above:                       300s

Telegram side (getWebhookInfo)
  url:                               https://yousaid.srv1492259.hstgr.cloud/telegram/webhook
  waiting at Telegram:               0
  max connections:                   1
  last delivery error:               none

OK: nothing is stuck and Telegram reports no recent delivery error.
```

What the numbers mean:

| Line | Reading |
| --- | --- |
| `waiting to be processed (pending)` | Updates written by `api` that the worker has not finished yet. A handful for a moment is normal; **a number that grows between two runs of this command is the rollback signal** below. |
| `gave up permanently (failed)` | Rows that exhausted `INBOX_MAX_ATTEMPTS` and will never be retried. Anything above 0 means updates were lost to the user — worth reading the worker log. |
| `head of the queue waiting` | Age of the OLDEST unprocessed update. This is the sharper signal of the two: strict `update_id` order means a stuck head blocks everything behind it, so **seconds are normal and minutes are not**. `-` means the queue is empty. |
| `stuck above` | The threshold the age above is judged against — see the knobs below. |
| `waiting at Telegram` | `pending_update_count` from `getWebhookInfo`: updates Telegram is still holding because it could not hand them to us. Ours (`pending`) and theirs are different queues — a backlog on Telegram's side with an empty inbox means the door itself is shut. |
| `last delivery error` | **The field to read first.** `getWebhookInfo`'s `last_error_date` / `last_error_message` — if Telegram cannot deliver to us, this is where it says so (bad certificate, 502 from traefik, timeout). Marked `FRESH` inside the error window and `history` outside it, because Telegram does NOT clear this field after a successful delivery — only `setWebhook` does — so an old message here is not an incident. |

The last line is the verdict, and the exit code follows it, so the same command
can be dropped into any external scheduler later without changes:

| Exit | Verdict | Meaning |
| --- | --- | --- |
| `0` | `OK` | Nothing stuck, no fresh delivery error. |
| `1` | `PROBLEM` | The head is older than the threshold, and/or there are `failed` rows, and/or Telegram reports a fresh delivery error. The line names which. |
| `2` | `UNKNOWN` | The state could not be established: the database is unreachable, or Telegram is, or the command never got that far (a broken `.env`, for instance). A dead network does not kill the command — if only `getWebhookInfo` fails, the queue numbers are still printed and the Telegram block says `UNREACHABLE` with the failure's class name. |

The bot token never appears in the output, by either route. **Failures print the
exception class and nothing else** — never a traceback — because the message
text may carry the request URL (a Bot API URL contains the token) or the
database DSN with its password. **Strings that come back from Telegram** — the
webhook `url` and `last delivery error` — are printed through a redactor: the
bot token, `TELEGRAM_WEBHOOK_SECRET` and anything shaped like a Telegram token
come out as `<redacted>`. Our own webhook keeps its secret in the header, not in
the path, but "token as the webhook path" is a common enough misconfiguration
that this command has to catch it rather than print it.

Two knobs, both in the server's `.env` (defaults are fine for this bot):

| Key | Default | What it does |
| --- | --- | --- |
| `INBOX_HEAD_AGE_ALERT_SECONDS` | `300` | Head age above which the queue counts as stuck (exit `1`). |
| `INBOX_WEBHOOK_ERROR_WINDOW_SECONDS` | `3600` | How recent Telegram's `last_error` must be to count as a live problem rather than history. |

**This is the command behind the rollback rule.** Run it twice a minute apart:
if `pending` and the head age are both climbing while the worker is up, the
webhook path is not moving updates — roll back to polling with the procedure
below. A fresh `last delivery error` with a rising `waiting at Telegram` and an
empty `pending` points the other way: the updates are not even reaching `api`,
so check traefik, the certificate and `TELEGRAM_WEBHOOK_SECRET` before rolling
back.

There is deliberately no HTTP endpoint for this: it would face the internet and
drag in the question of authorising it. `/health` is deliberately untouched too —
the container healthcheck calls it, and a deep queue must not paint the
container unhealthy.

#### Two traps that already cost time

* **Changing `TELEGRAM_WEBHOOK_SECRET` in `.env` does nothing until `api`
  restarts.** The route resolves "is the webhook configured?" once and caches it
  for the life of the process, so an `api` started with an empty secret keeps
  answering 503 with the new secret sitting in the file. `setWebhook` then
  registers happily against a door that refuses every delivery. **Always
  `docker compose -f docker-compose.prod.yml restart api` after touching that
  variable.**
* **Prove the door is open before calling `setWebhook`.** A request with no
  secret header must answer **401** (wrong/missing secret), not **503** (webhook
  not configured):

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' \
    -X POST https://yousaid.srv1492259.hstgr.cloud/telegram/webhook \
    -H 'Content-Type: application/json' -d '{}'
  # 401 = configured and rejecting the unauthenticated call — good.
  # 503 = still unconfigured; the api process has not picked up the secret.
  ```

#### Rolling back to polling

Use this when the webhook is broken and the bot must receive updates again —
confirm it with `second-brain-inbox-status` above first, so the rollback is a
decision and not a guess.
Run everything from the app directory on the VPS (`cd` there first). The steps
are ordered so the two doors never overlap.

**(a) Stop the webhook at Telegram.** Until this returns, polling cannot work —
Telegram would answer every `getUpdates` with `409 Conflict`.

```bash
set -a; . ./.env; set +a   # also loads APP_IMAGE_POLLING_FALLBACK for step (b)
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"; echo
# expect: {"ok":true,"result":true,...}
```

**(b) Point `APP_IMAGE` at the pinned fallback build.**

```bash
export APP_IMAGE="$APP_IMAGE_POLLING_FALLBACK"
echo "$APP_IMAGE"   # must NOT be empty
```

**(c) Start `polling` with its profile.**

```bash
docker compose -f docker-compose.prod.yml --profile rollback up -d polling
```

**(d) Confirm the polling door actually works.**

```bash
# 1. the container is up and healthy (give it ~40s for the start period)
docker compose -f docker-compose.prod.yml ps polling

# 2. no 409 Conflict in the log — that would mean the webhook is still registered
docker compose -f docker-compose.prod.yml logs --tail 50 polling

# 3. Telegram agrees the webhook is gone: "url" must be empty
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

Then send the bot a message and check that it replies.

**(e) Returning to the webhook.** Stop polling FIRST, then register the webhook,
and remove the container so the next deploy has nothing to warn about.

```bash
docker compose -f docker-compose.prod.yml stop polling

set -a; . ./.env; set +a
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://yousaid.srv1492259.hstgr.cloud/telegram/webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -d "max_connections=1" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
# expect: {"ok":true,"result":true,"description":"Webhook was set"}

docker compose -f docker-compose.prod.yml rm -sf polling
```

Send a message and confirm the reply; `SELECT status, attempt_count FROM
telegram_update_inbox ORDER BY id DESC LIMIT 5;` should show fresh `done` rows.

#### Do NOT deploy while a rollback is running

A push to `main` deploys automatically, and `scripts/deploy_prod.sh` **stops
`polling` and does not bring it back** — it stops it on purpose (during a
rollback polling is the live bot, and `init-db` briefly REVOKEs the app role's
privileges under it), but the restart afterwards only covers services in the
active profiles. The result is a bot with **both doors shut**: the webhook is
deleted and nothing is polling. The deploy still reports success, so the script
prints a loud `WARNING` when it finds a stopped `polling` container.

Before deploying during a rollback, pick one:

* **preferred** — finish the rollback first: do step (e) above, then deploy
  normally; or
* deploy, and immediately re-run steps (b) and (c) to bring polling back on the
  fallback image.

One more version-dependent detail, so it is not a surprise: `up -d
--remove-orphans` in the deploy script does **not** delete the `polling`
container on current Compose (verified on v2.32.4 — a defined service in an
inactive profile is not treated as an orphan), but Compose versions before
~2.21 did delete it. On such a version the container is gone rather than
stopped, the script's warning cannot fire, and only step (c) restores it. The
rule above covers both cases.

## Automated checks

```bash
uv run --env-file .env pytest -W error
uv run --env-file .env ruff check .
uv run --env-file .env ruff format --check .
uv run --env-file .env mypy src
```
