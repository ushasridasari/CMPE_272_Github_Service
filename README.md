# GitHub Issues Gateway

A contract-first HTTP service that wraps the GitHub Issues REST API for a
single repository, receives signed GitHub webhooks, and exposes a debugging log
of everything it has received.

Built with Python 3.11+ and FastAPI. Ships with an OpenAPI 3.1 contract, a test
suite at 98% line coverage, a Docker image, and CI.

- **Contract:** [`openapi.yaml`](openapi.yaml) — also served live at `/docs`
- **Design decisions:** [`DESIGN.md`](DESIGN.md)

> **On "delete":** GitHub's REST API cannot delete an issue. The **D** of CRUD
> is a state transition to `closed`. `DELETE /issues/{number}` exists as an
> alias for that close and returns the closed issue plus
> `X-Delete-Semantics: closed-not-deleted`, so nobody is misled into thinking
> data was destroyed.

---

## Table of contents

- [Quick start](#quick-start)
- [Environment variables](#environment-variables)
- [Token setup](#token-setup)
- [Running without Docker](#running-without-docker)
- [Running with Docker](#running-with-docker)
- [API reference with examples](#api-reference-with-examples)
- [Webhook setup](#webhook-setup)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
git clone <your-repo-url> && cd issues-gw

cp .env.example .env
make secret                 # generate a WEBHOOK_SECRET, paste it into .env
$EDITOR .env                # fill in GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO

make install
make test                   # 190 tests, no network needed
make run
```

Then:

```bash
curl -s localhost:8080/healthz
# {"status":"ok","repo":"you/cmpe272-issues-gw","version":"1.0.0"}
```

Interactive docs: <http://localhost:8080/docs>

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GITHUB_TOKEN` | **yes** | — | Fine-grained PAT or App installation token |
| `GITHUB_OWNER` | **yes** | — | Repository owner, e.g. `andrewbond` |
| `GITHUB_REPO` | **yes** | — | Repository name, e.g. `cmpe272-issues-gw` |
| `WEBHOOK_SECRET` | **yes** | — | Shared secret for webhook HMAC |
| `PORT` | no | `8080` | Port this service listens on |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `EVENTS_DB_PATH` | no | `data/events.db` | SQLite file for webhook deliveries |
| `ENABLE_ETAG_CACHE` | no | `true` | Conditional GET (extra credit) |
| `REQUEST_TIMEOUT_SECONDS` | no | `15` | Upstream timeout |

`.env` is gitignored. `.env.example` is committed with placeholders only.
The service refuses to start if a required variable is missing, which is
deliberate — failing at boot beats failing on the first request.

---

## Token setup

Create a **fine-grained** PAT at
<https://github.com/settings/personal-access-tokens/new>:

1. **Repository access** → *Only select repositories* → pick your test repo.
2. **Permissions** → Repository permissions → **Issues: Read and write**.
   (*Metadata: Read* is added automatically and is required.)
3. Nothing else. That is the entire scope this service uses.

Verify before writing any code:

```bash
source .env
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
     -H "Accept: application/vnd.github+json" \
     "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/issues" | head -c 200
```

Generate the webhook secret with `make secret` (32 random bytes, hex). It must
match the value in your repository's webhook settings **exactly**.

---

## Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

make run          # production-style, no autoreload
make dev          # autoreload for development
```

Or directly:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Requires Python 3.11 or newer.

---

## Running with Docker

```bash
make docker-build

docker run --rm -p 8080:8080 \
  --env-file .env \
  -v issues-gw-data:/data \
  -e EVENTS_DB_PATH=/data/events.db \
  --name issues-gw \
  issues-gw:latest
```

Or with compose:

```bash
docker compose up --build                # service only
docker compose --profile tunnel up       # service + smee webhook tunnel
```

The image is multi-stage, runs as non-root (uid 10001), and has a healthcheck
against `/healthz`. The named volume keeps the webhook event log across
restarts.

A `.devcontainer/` is included for VS Code / Codespaces one-click setup.

---

## API reference with examples

Base URL: `http://localhost:${PORT}`

Callers of this API present **no** GitHub token — the service holds it
server-side.

### `POST /issues` — create

```bash
curl -i -X POST localhost:8080/issues \
  -H 'Content-Type: application/json' \
  -d '{
        "title": "Login button is misaligned on mobile",
        "body": "On iOS Safari the button overlaps the footer.",
        "labels": ["bug", "ui"]
      }'
```

```
HTTP/1.1 201 Created
Location: /issues/42
X-Request-ID: 4f2a9c1e88b04d77
```

```json
{
  "number": 42,
  "title": "Login button is misaligned on mobile",
  "body": "On iOS Safari the button overlaps the footer.",
  "state": "open",
  "labels": ["bug", "ui"],
  "html_url": "https://github.com/you/cmpe272-issues-gw/issues/42",
  "user": "you",
  "comments": 0,
  "created_at": "2026-09-08T17:04:11Z",
  "updated_at": "2026-09-08T17:04:11Z",
  "closed_at": null
}
```

HTTPie: `http POST :8080/issues title="Bug report" labels:='["bug"]'`

### `GET /issues` — list

```bash
curl -s 'localhost:8080/issues?state=open&per_page=10'
curl -s 'localhost:8080/issues?state=all&labels=bug,ui&page=2&per_page=50'
curl -sD- -o/dev/null 'localhost:8080/issues?per_page=1'   # inspect headers
```

Query: `state` (`open`|`closed`|`all`, default `open`), `labels`
(comma-separated), `page` (≥1), `per_page` (1–100).

Response headers: `Link` (rewritten to point at *this* service), `ETag`,
`X-Cache` (`HIT`/`MISS`), `X-Total-Count`, `X-Page`, `X-Per-Page`,
`X-RateLimit-Remaining`.

Pull requests are filtered out — GitHub returns them from this endpoint, but
they are not issues.

HTTPie: `http :8080/issues state==open per_page==10`

### `GET /issues/{number}` — read

```bash
curl -s localhost:8080/issues/42
curl -s localhost:8080/issues/99999999      # 404
```

### `PATCH /issues/{number}` — update, close, reopen

```bash
# rename
curl -s -X PATCH localhost:8080/issues/42 \
  -H 'Content-Type: application/json' \
  -d '{"title": "Login button misaligned on iOS Safari"}'

# close  (the "D" of CRUD)
curl -s -X PATCH localhost:8080/issues/42 \
  -H 'Content-Type: application/json' -d '{"state": "closed"}'

# reopen
curl -s -X PATCH localhost:8080/issues/42 \
  -H 'Content-Type: application/json' -d '{"state": "open"}'
```

At least one of `title`, `body`, `state` is required; an empty patch is a 400
rather than a silent no-op.

### `DELETE /issues/{number}` — close (alias)

```bash
curl -i -X DELETE localhost:8080/issues/42
# HTTP/1.1 200 OK
# X-Delete-Semantics: closed-not-deleted
```

### `POST /issues/{number}/comments` — comment

```bash
curl -i -X POST localhost:8080/issues/42/comments \
  -H 'Content-Type: application/json' \
  -d '{"body": "Reproduced on iPhone 14, iOS 17.4."}'
```

```
HTTP/1.1 201 Created
Location: /issues/42/comments
```

### `GET /issues/{number}/comments` — list comments

```bash
curl -s 'localhost:8080/issues/42/comments?per_page=20'
```

### `POST /webhook` — receive a delivery

Called by GitHub, not by you. To exercise it by hand:

```bash
SECRET='your-webhook-secret'
BODY='{"action":"opened","issue":{"number":7,"title":"Test"},"sender":{"login":"you"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"

curl -i -X POST localhost:8080/webhook \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: $(uuidgen)" \
  -H "X-Hub-Signature-256: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
# HTTP/1.1 204 No Content
```

Send the same `X-GitHub-Delivery` twice: still `204`, still **one** entry in
`/events`. That is the idempotency guarantee.

| Situation | Status |
|---|---|
| Valid signature, known event | `204` |
| Duplicate delivery | `204`, ignored |
| `ping` | `204`, not stored |
| Missing/invalid signature, tampered body | `401` |
| Unknown event or action, malformed JSON | `400` |

### `GET /events` — inspect deliveries

```bash
curl -s 'localhost:8080/events?limit=20' | python -m json.tool
```

```json
[
  {
    "id": "8f2c1e40-9b3a-11f0-8d1e-2f9a7c4b1a55",
    "event": "issues",
    "action": "opened",
    "issue_number": 42,
    "sender": "you",
    "timestamp": "2026-09-08T17:04:12+00:00"
  }
]
```

### `GET /healthz`

```bash
curl -s localhost:8080/healthz
```

Does not call GitHub — a liveness probe that depends on a third party flaps
when that third party has a bad minute.

### Error format

Every non-2xx response uses one envelope:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Request failed validation.",
    "details": [{"field": "title", "message": "Field required"}]
  }
}
```

Codes: `invalid_request`, `unauthorized`, `invalid_signature`,
`missing_event_header`, `unsupported_event`, `unsupported_action`,
`malformed_payload`, `forbidden`, `not_found`, `rate_limited`,
`upstream_error`, `upstream_unavailable`, `http_error`, `internal_error`.

`429` and `503` carry `Retry-After`. Full mapping table in
[`DESIGN.md`](DESIGN.md#1-error-mapping).

---

## Webhook setup

GitHub cannot reach a laptop directly, so you need a public tunnel.

### 1. Start a tunnel

**smee** (no account, survives URL changes best):

```bash
npm install -g smee-client
# create a channel at https://smee.io, then:
smee --url https://smee.io/YOUR_CHANNEL --target http://localhost:8080/webhook
```

**ngrok:**

```bash
ngrok http 8080
# use the https://xxxx.ngrok-free.app URL below
```

**cloudflared:**

```bash
cloudflared tunnel --url http://localhost:8080
```

### 2. Register the webhook

Repository → **Settings** → **Webhooks** → **Add webhook**:

| Field | Value |
|---|---|
| Payload URL | smee channel URL, or `https://<tunnel-host>/webhook` |
| Content type | `application/json` |
| Secret | the same value as `WEBHOOK_SECRET` in `.env` |
| SSL verification | Enable |
| Events | *Let me select individual events* → **Issues** + **Issue comments** |

Save. GitHub immediately sends a `ping`; your service answers `204`.

> With smee, the Payload URL is the **smee.io channel URL** — the smee client
> forwards to `/webhook` locally. With ngrok or cloudflared, the Payload URL
> must include the `/webhook` path.

### 3. Verify

```bash
# create an issue through the gateway
curl -s -X POST localhost:8080/issues \
  -H 'Content-Type: application/json' -d '{"title":"Webhook probe"}'

# a moment later
curl -s localhost:8080/events | python -m json.tool
```

You should see an `issues` / `opened` entry with GitHub's delivery UUID.

### 4. Redelivery

In **Settings → Webhooks → your webhook → Recent Deliveries**, pick any
delivery and press **Redeliver**. Expected result: `204`, and **no new row** in
`/events` — the `(delivery_id, action)` key makes replay a no-op.

The service logs `webhook_duplicate_ignored` when this happens.

### 5. After the demo

Rotate the secret: `make secret`, update both `.env` and the GitHub webhook
settings, restart. Tunnel URLs from a demo should not stay registered.

---

## Testing

```bash
make test              # unit suite + coverage gate (offline)
make coverage          # HTML report at htmlcov/index.html
make lint              # ruff
make spec-lint         # validate openapi.yaml against the 3.1 metaschema
```

**Unit tests** (`tests/unit/`) — 190 tests, 98% line coverage, no network.
GitHub is mocked with `respx`.

- Route validation: missing title, empty title, invalid `state`, empty patch,
  `per_page` out of bounds, malformed JSON
- Signature verification: valid, invalid, tampered body, re-serialised body,
  wrong secret, malformed headers, wrong algorithm prefix
- Error mapping: every GitHub status, plus the 403 rate-limit-vs-permission
  fork and `Retry-After` computation
- Pagination: `Link` parsing including malformed and comma-containing URLs,
  rewriting, relation order, `per_page` clamping
- Idempotency: redelivery, composite key, persistence across restart
- Conditional GET: `If-None-Match` replay, 304 handling, cache invalidation
- Cross-cutting: single error envelope, request-id correlation, secrets never
  appearing in responses

**Integration tests** (`tests/integration/`) — run against the real GitHub API
and a running instance:

```bash
make run &                    # in one terminal
make test-integration         # in another
```

Covers create → get, list containment, update title/body, close → reopen,
comment → list comments, pagination headers, ETag cache, and 404/400 paths.
Each test closes the issues it creates; the repo will accumulate closed issues
titled `[itest] ...`, which is expected.

With a tunnel running and the webhook registered:

```bash
make test-webhook             # includes a real end-to-end delivery
```

Integration tests skip automatically unless `RUN_INTEGRATION=1`, so `make test`
stays green offline.

**CI** (`.github/workflows/ci.yml`) — lint, OpenAPI validation, unit tests on
Python 3.11 and 3.12 with an 80% coverage gate, Docker build plus an image
smoke test that fails the build if an unsigned webhook is ever accepted, and a
live integration job (skipped on forks).

### API collection

`postman_collection.json` covers every route, with a pre-request script that
computes the HMAC signature for the webhook call. Import into Postman or
Bruno; set the `baseUrl` and `webhookSecret` collection variables.

---

## Project layout

```
.
├── app/
│   ├── main.py              # app factory, middleware, exception handlers
│   ├── config.py            # env-driven settings
│   ├── models.py            # Pydantic mirrors of the OpenAPI schemas
│   ├── errors.py            # error types + the single response envelope
│   ├── dependencies.py      # DI providers
│   ├── logging_config.py    # JSON logs, request-id correlation
│   ├── github/
│   │   ├── client.py        # the only module that knows about api.github.com
│   │   ├── errors.py        # GitHub status -> AppError
│   │   ├── pagination.py    # Link header parse + rewrite
│   │   └── cache.py         # ETag LRU (conditional GET)
│   ├── routes/
│   │   ├── issues.py        # CRUD + comments
│   │   ├── webhook.py       # signed intake + /events
│   │   └── health.py        # /healthz
│   └── webhooks/
│       ├── verify.py        # HMAC SHA-256, constant-time compare
│       └── store.py         # SQLite, idempotent inserts
├── tests/
│   ├── conftest.py
│   ├── fixtures/            # GitHub payload samples
│   ├── unit/
│   └── integration/
├── openapi.yaml             # the contract
├── DESIGN.md                # design note
├── Dockerfile
├── docker-compose.yaml
├── Makefile
├── postman_collection.json
├── .devcontainer/
├── .github/workflows/ci.yml
└── .env.example
```

Business logic never imports FastAPI: `verify.py`, `pagination.py`,
`store.py`, and `github/errors.py` are all framework-free, which is what makes
them testable in isolation.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Service will not start, `ValidationError` on boot | A required env var is missing. Check `.env` against `.env.example`. |
| `401 unauthorized` on every `/issues` call | `GITHUB_TOKEN` is wrong, expired, or not scoped to this repo. Re-run the verification curl in [Token setup](#token-setup). |
| `403 forbidden` | Token is valid but lacks **Issues: Read and write** on the repository. |
| `429 rate_limited` | GitHub budget exhausted. Wait for `Retry-After`; the ETag cache reduces list-call cost. |
| Webhook returns `401` | `WEBHOOK_SECRET` differs between `.env` and GitHub's webhook settings. They must match byte for byte — check for a trailing newline. |
| Webhook returns `400 unsupported_event` | The webhook subscribes to more than Issues and Issue comments. Narrow the selection. |
| No deliveries appear in `/events` | Tunnel is down or the URL changed. Check **Recent Deliveries** in GitHub's webhook settings for the actual response. |
| Redelivery creates a second `/events` row | Should be impossible. Confirm `X-GitHub-Delivery` is being sent. |
| `/issues` returns pull requests | Should be impossible; they are filtered. File it as a bug. |
| Docker: `permission denied` on the DB | Set `EVENTS_DB_PATH=/data/events.db` and mount a volume at `/data`. |

---

## Attribution

Scaffolded from FastAPI's standard project layout as described in the
[official documentation](https://fastapi.tiangolo.com/tutorial/bigger-applications/).
All business logic, tests, and the OpenAPI specification are original work.

Third-party libraries: FastAPI, Uvicorn, httpx, Pydantic, pytest, respx, Ruff.
