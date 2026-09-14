# Design Note

Four decisions shaped this service: how upstream failures are translated, how
pagination crosses the boundary, how webhook replays are made safe, and what
security trade-offs were accepted. Each is below with the reasoning and the
cost.

---

## 1. Error mapping

A gateway has two kinds of caller mistake and two kinds of upstream problem,
and a client can only respond sensibly if it can tell them apart. The guiding
rule: **a caller error stays 4xx, a GitHub problem becomes 5xx**, and GitHub's
response body is never forwarded verbatim.

| GitHub | This service | Code | Why |
|---|---|---|---|
| 401 | 401 | `unauthorized` | Server credential problem. The message names `GITHUB_TOKEN` because the operator, not the caller, must fix it. |
| 403 + `x-ratelimit-remaining: 0` | **429** | `rate_limited` | Rate limiting, not permission. `Retry-After` populated. |
| 403 + `retry-after` | **429** | `rate_limited` | Secondary rate limit. |
| 403 otherwise | 403 | `forbidden` | Genuine permission failure; message names the missing `Issues: Read and write` grant. |
| 404 | 404 | `not_found` | |
| 422 / 410 | **400** | `invalid_request` | GitHub's 422 means *our caller* sent something invalid. Field errors are flattened into `details`. |
| 429 | 429 | `rate_limited` | |
| 502/503/504 | **503** | `upstream_unavailable` | Transient; `Retry-After: 30`. |
| Other 5xx | **502** | `upstream_error` | We reached GitHub, it misbehaved — a bad gateway, not a bad server. |
| Timeout / connect error | 503 | `upstream_unavailable` | |
| Unhandled exception | 500 | `internal_error` | Reserved strictly for our own bugs. |

**The 403 fork is the interesting one.** GitHub overloads 403 for "you are out
of requests" and "your token cannot do that" — two problems with opposite
remedies. `is_rate_limited()` disambiguates on `x-ratelimit-remaining: 0`, then
a `retry-after` header, then the message text. A client that retries a
permission error loops forever; a client that gives up on a rate limit loses
data. The distinction is worth the branch, and it is directly tested
(`test_403_permission_problem_is_not_a_rate_limit`).

Every response uses one envelope, so a client needs one error branch:

```json
{"error": {"code": "invalid_request", "message": "...", "details": [{"field": "title", "message": "Field required"}]}}
```

FastAPI's native 422 is reshaped into this same 400 envelope by a
`RequestValidationError` handler, so a validation failure and a GitHub
rejection look identical to the caller.

**Trade-off accepted:** collapsing GitHub's 422 to 400 loses the distinction
between "syntactically invalid" and "semantically rejected". The `details`
array preserves what actually matters, and one fewer status code is one fewer
branch for every client.

---

## 2. Pagination

GitHub's cursor lives in an RFC 8288 `Link` header. Two options: forward it
untouched, or rewrite the URLs.

**Rewriting won.** A forwarded header hands the caller
`https://api.github.com/repositories/1/issues?page=2` — a URL they cannot call,
because the whole point of this gateway is that they hold no GitHub token.
Following the link would 401. `rewrite_link_header()` therefore rebuilds each
relation as `{this-service}/issues?state=...&per_page=...&page=2`, carrying the
caller's own filters plus GitHub's page cursor, in `prev, next, first, last`
order.

Also on the list route:

- `per_page` is bounded to 1–100 at the edge, so an out-of-range value is a 400
  from us rather than a confusing rejection from GitHub.
- `X-Total-Count`, `X-Page`, `X-Per-Page` for clients that would rather not
  parse a `Link` header.
- `X-RateLimit-*` forwarded so a caller can pace itself.
- **Pull requests are filtered out.** GitHub's issues endpoint returns PRs
  because they share the issue number space. They are not issues, and a
  consumer counting open issues would silently get the wrong number.

**Trade-off accepted:** the parser is deliberately tolerant — a malformed
segment is skipped, not raised. A broken pagination hint should never fail an
otherwise good response.

---

## 3. Webhook dedupe

GitHub redelivers on any non-2xx or timeout, and the redeliver button exists in
the UI. Duplicate processing must be impossible, not merely unlikely.

**Key: `(X-GitHub-Delivery, action)`, primary key in SQLite, written with
`INSERT OR IGNORE`.** The delivery UUID alone covers GitHub's own retries; the
action is included so a hand-replayed variant stays distinguishable during
debugging. `record()` returns whether the row was new, so follow-on work runs
exactly once while the duplicate still gets a 204 — GitHub's retry logic needs
to settle either way.

Ordering in the handler is load-bearing:

1. Read the **raw** body. Re-serialising parsed JSON changes whitespace and key
   order, so the HMAC would never match. A unit test pins this
   (`test_reserialised_body_rejected`).
2. Verify the signature **before** parsing or inspecting anything. An unsigned
   request with a bad event type returns 401, not 400 — the sender learns
   nothing about what else was wrong.
3. Reject unknown events and actions with 400, so a misconfigured subscription
   is visible rather than silently swallowed.
4. Insert, then return 204.
5. Anything slow runs in a `BackgroundTask`, after the response is written.

**Why persist before acking?** The insert is sub-millisecond, and doing it
first is what makes redelivery safe. Deferring it would leave a window where a
crash loses the delivery while GitHub has already been told it succeeded.

**Trade-off accepted:** SQLite means one writer and no horizontal scale. For a
single-instance gateway that is the right size; the store is behind a small
interface, so Postgres or Redis is a swap, not a rewrite.

---

## 4. Security trade-offs

**Fine-grained PAT over a GitHub App.** An App would give short-lived
installation tokens and cleaner rotation, but costs a registration, a private
key to store, and JWT exchange logic. For a single repository the PAT scoped to
`Issues: Read and write` on exactly one repo is a smaller blast radius than a
classic PAT and far less machinery. *Accepted cost:* manual rotation, and a
leaked token is valid until revoked.

**Constant-time comparison.** `hmac.compare_digest`, never `==`. A byte-by-byte
comparison leaks the expected signature through response timing, one byte at a
time. Cheap to do right, unrecoverable to get wrong.

**Nothing secret is ever logged.** Not the token, not `WEBHOOK_SECRET`, not the
signature header — not even on the rejection path, where the temptation to log
"expected X, got Y" is strongest. Logging a failed signature attempt is fine;
logging the value is a gift to anyone with log access. A test asserts the
signature never appears in an error response.

**Config entirely through environment.** `.env` is gitignored, `.env.example`
is committed with placeholders, and CI uses dummy values because the unit suite
never touches the network.

**GitHub bodies are not passed through.** A 404 body carries a
`documentation_url`; forwarding it leaks upstream structure and confuses
callers who have no GitHub relationship. Tested.

**Health check does not call GitHub.** A liveness probe that depends on a third
party flaps when that third party has a bad minute, and the orchestrator then
restarts a perfectly healthy process.

**Known gaps, deliberately out of scope:** the gateway itself is unauthenticated
(bind it to localhost or put it behind a reverse proxy — the webhook route is
the only one meant to face the internet, and it authenticates by signature);
there is no per-caller rate limiting of our own; the ETag cache is in-process
and unbounded in freshness, though writes invalidate it.

---

## Extra credit: conditional GET

`GET /issues` stores the returned `ETag` keyed by path plus sorted query
parameters, and replays it as `If-None-Match`. GitHub answers 304 **without
charging the rate limit**, and we serve the cached body as a 200 with
`X-Cache: HIT`. Writes clear the cache so a create never leaves a stale list
behind. The cache is a bounded LRU: losing it on restart costs exactly one
extra API call, which is the correct price for an optimisation.
