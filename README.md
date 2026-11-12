# OCR Accent-Aware Archive Search

FastAPI + SQLite FTS5 service for an OCR archive whose searchable accent
policy can be changed online. Page corrections and deletes continue to commit
while a new index is being built; the old index serves searches until the new
generation has caught up and is switched atomically.

## Start with Docker Compose

```bash
docker compose up --build -d
```

Open **<http://localhost:8000/docs>** for the management service and
**<http://localhost:8080/docs>** for the read-only public service. Each
container runs exactly two uvicorn worker processes (via `app.serve`) against
one WAL-mode SQLite file on the named volume `archive-data`.

```bash
docker compose ps
docker compose exec api ps -ef | grep '[u]vicorn'
docker compose exec public ps -ef | grep '[u]vicorn'
```

`api` is the only service that writes the database: page writes, index
rebuilds and redacted publication. `public` shares the same WAL file but its
application exposes only published-body reads and search; it opens
`query_only` connections. SQLite is not shared between separate database
files — both services mount the same named volume.

To make a rebuild visibly long enough to submit edits by hand during it:

```bash
REBUILD_BATCH_DELAY_MS=250 docker compose up --build -d
```

The delay is a demo-only aid and is `0` by default. Reset demo data with:

```bash
docker compose down -v
```

## Public read-only service

The public port is a **separate application** with only three routes:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/public/pages/{page_id}` | Read the published redacted body. |
| `GET` | `/public/search?q=word` | Word-AND search over published redacted bodies. |
| `GET` | `/healthz` | Health. |

There is no original-page endpoint and no title anywhere on this port.
Search is fixed to the `keep` accent rule and never follows the management
service's active policy. Hits, totals and returned bodies come from the
redacted `publications`/`public_fts` projection in one read transaction.
No management route exists in this application.

## Swagger demo

Every step is available with **Try it out** on `/docs`. The OpenAPI description
also contains the full demo narrative and accented request examples.

### 1. Write two accented OCR pages under the initial `keep` policy

```bash
BASE=http://localhost:8000

curl -sS -X PUT "$BASE/api/pages/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Crème brûlée à Paris",
    "body": "Le café est près de la gare; le señor lit un naïve résumé."
  }'

curl -sS -X PUT "$BASE/api/pages/ocr-002" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "A plain cafe",
    "body": "This page uses cafe and naive without accents."
  }'
```

### 2. Search with the initial exact-accent rule

```bash
curl --get "$BASE/api/search" --data-urlencode 'q=café'
# ocr-001 is returned.

curl --get "$BASE/api/search" --data-urlencode 'q=cafe'
# Only ocr-002 matches. The accented café is not folded under keep.
```

Word queries are literal words. Multiple words are ANDed; FTS query operators
from the request are never interpreted.

### 3. Trigger the background switch to `fold`

```bash
curl -sS -X POST "$BASE/api/index/rebuild" \
  -H 'Content-Type: application/json' \
  -d '{"accent_policy": "fold"}'
```

The response is HTTP `202` and contains a `job.job_id`. A duplicate trigger
while the job is active returns the **same** job ID with
`"reused_existing_rebuild": true`.

### 4. Correct and delete during the rebuild

Run these while `GET /api/index/status` reports `building` or `catching_up`:

```bash
# Correction: replaces the page and becomes visible in both the old live index
# and, after catch-up, the new generation.
curl -sS -X PUT "$BASE/api/pages/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Crème brûlée — corrected OCR",
    "body": "Le café fermé est près de la gare; le señor relit le résumé corrigé."
  }'

# Deletion must remain deleted; the old snapshot scan must not revive it.
curl -sS -X DELETE "$BASE/api/pages/ocr-002"
```

These writes commit immediately. The rebuild scan does not hold a lock for its
duration, so editing is not blocked by it.

### 5. Watch the live status and old index

```bash
curl -sS "$BASE/api/index/status"
curl --get "$BASE/api/search" --data-urlencode 'q=cafe'
```

While the job is running, `active_policy` is still `keep`,
`rebuild_in_progress` is `true`, and `current_rebuild_job` shows progress:
`queued → building → catching_up → activating → succeeded`.

### 6. Query after the switch

Once `current_rebuild_job` is `null` and `active_policy` is `fold`:

```bash
curl --get "$BASE/api/search" --data-urlencode 'q=cafe'
# Returns ocr-001 with the corrected body: café matches cafe.

curl --get "$BASE/api/search" --data-urlencode 'q=resume'
# Folded résumé matches.
```

Deleted `ocr-002` remains absent, and returned titles/bodies are the corrected
committed versions from the same SQLite snapshot as the hit.

## Redaction publication demo

The examples below run against the management port and are also available
with **Try it out** on the management `/docs` page. Code points are Unicode
code points (not UTF-16 units or bytes).

### 1. Write a page and read its revision

```bash
BASE=http://localhost:8000

curl -sS -X PUT "$BASE/api/pages/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Crème brûlée à Paris",
    "body": "Le café est près de la gare; le señor lit un naïve résumé."
  }'
# -> revision 1 (the body is 58 code points)

curl -sS "$BASE/api/pages/ocr-001"
```

### 2. Publish less than 20 redaction spans

```bash
curl -sS -X POST "$BASE/api/publications/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 1,
    "spans": [
      {"start": 3,  "end": 7},
      {"start": 32, "end": 37},
      {"start": 45, "end": 50},
      {"start": 51, "end": 57}
    ]
  }'
# -> "published_body": "Le █ est près de la gare; le █ lit un █ █."
```

Each span is half-open and replaced by exactly one `█`. The original page is
unchanged. Invalid spans (empty, overlapping, negative, beyond the body, or
more than 20) return **422** and publish nothing. The response includes
`already_published` so retries are safe.

### 3. Read and search on the public port

```bash
PUBLIC=http://localhost:8080

curl -sS "$PUBLIC/public/pages/ocr-001"
# -> page_id, body (redacted), revision, published_at. No title.

curl --get "$PUBLIC/public/search" --data-urlencode 'q=gare'
# -> ocr-001

curl --get "$PUBLIC/public/search" --data-urlencode 'q=café'
# -> total 0: the redacted word is not searchable.

curl --get "$PUBLIC/public/search" --data-urlencode 'q=gare+señor'
# -> total 0: all words are required, and señor was redacted.
```

Accent matching on the public port is fixed to `keep` (`café` != `cafe`),
regardless of any rebuild on the management port.

### 4. Correct the page, then re-publish only the new revision

```bash
curl -sS -X PUT "$BASE/api/pages/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{"title": "Correction", "body": "Le secret est ailleurs."}'
# -> revision 2. The public snapshot for revision 1 is withdrawn immediately.

curl -sS -X POST "$BASE/api/publications/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "spans": [{"start": 3, "end": 7}]}'
# -> HTTP 409 with current_revision 2; nothing is published.

curl -sS -X POST "$BASE/api/publications/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "spans": [{"start": 3, "end": 9}]}'
# -> publishes "Le █ est ailleurs."
```

### 5. Withdraw explicitly (corrected-then-removed workflow)

```bash
curl -sS -X DELETE "$BASE/api/publications/ocr-001"
# Idempotent; body {"page_id":"ocr-001","withdrawn":true,...} or ...false.

curl -sS -X DELETE "$BASE/api/publications/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2}'
# Optional guard: 409 if the publication moved on, 200 if it matches.
```

A `DELETE /api/pages/ocr-001` withdraws the publication in the same database
transaction. Re-creating the same page ID with `PUT` does not restore it, and
neither does a restart or an internal rebuild: the withdrawn state is durable.

## Accent examples

| Document word | `keep` query | `fold` query |
|---|---|---|
| café | café | cafe or café |
| señor | señor | senor or señor |
| naïve | naïve | naive or naïve |
| résumé | résumé | resume or résumé |
| Crème brûlée | crème / brûlée | creme / brulee |
| İstanbul | İstanbul | Istanbul |

`keep` normalises composed/decomposed spellings to NFC but never removes a
diacritic. `fold` removes combining marks attached to Latin base letters only;
non-Latin combining marks are preserved.

## API summary

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/api/pages/{page_id}` | Insert/replace an OCR title and body. |
| `DELETE` | `/api/pages/{page_id}` | Idempotent delete. |
| `GET` | `/api/pages/{page_id}` | Read a committed page. |
| `POST` | `/api/publications/{page_id}` | Publish a redacted snapshot (409 on stale revision). |
| `DELETE` | `/api/publications/{page_id}` | Withdraw a publication (idempotent). |
| `GET` | `/api/publications/{page_id}` | Inspect the current published redacted body. |
| `GET` | `/api/search?q=word` | Word search over the active FTS generation. |
| `POST` | `/api/index/rebuild` | Queue/switch accent policy through a background rebuild. |
| `GET` | `/api/index/status` | Active policy/generation, current job, progress, history. |
| `GET` | `/api/index/jobs/{job_id}` | One rebuild job. |
| `GET` | `/healthz` | Health and active policy. |

`POST /api/index/rebuild` also accepts `?accent_policy=fold` for convenience.
If the body/parameter is omitted, the opposite of the active policy is used.
The alias `POST /api/index/switch` is available but omitted from the Swagger
schema.

## Correctness model

* **Durable source of truth** — `pages` stores the latest committed title/body
  and `changes` is an append-only sequence log containing the full content of
  every write/delete.
* **One active generation** — each generation owns a separate FTS5 table.
  `settings.active_generation_id` and `settings.active_policy` are switched in
  the same short transaction that marks the new generation active.
* **Writes during rebuild** — a write transaction updates `pages`, appends to
  `changes`, and updates the currently active FTS table. It never writes to a
  generation under construction. Therefore no scan-length write lock is needed,
  and a failed worker cannot corrupt the serving index.
* **Snapshot baseline** — the rebuild reads its baseline in a pinned WAL read
  transaction. Writers continue to commit in WAL while that snapshot is open.
* **Catch-up** — the worker applies every change-log row after the snapshot,
  in sequence, to its private shadow FTS table. It does this in small write
  transactions, with heartbeats.
* **Atomic swap** — the final short `BEGIN IMMEDIATE` transaction applies the
  last residual changes, verifies shadow/page document counts, updates the
  pointer/policy, and marks the job succeeded. Only then does a new request see
  the new generation and policy.
* **Snapshot-consistent reads** — a search begins one SQLite read transaction,
  reads the active pointer/policy, the FTS hits and the canonical bodies in that
  same transaction. Hits and returned text therefore come from one committed
  state, even if a swap commits concurrently.
* **One rebuild across processes** — `rebuild_jobs` has a partial unique index
  on active states. Claiming is done under `BEGIN IMMEDIATE`, so two API
  processes cannot start two jobs; the loser returns the current job.
* **Failure/interruption** — the previous active generation is never changed
  until final commit. A failed job marks only its shadow generation failed. A
  new process marks a dead owner's job interrupted at startup (dead PID or a
  different container host), so the rebuild can be retriggered immediately.
  Canonical pages and the change log survive in SQLite.
* **Fencing** — the final activation re-checks the job's claim token and
  active state under the write lock, so a stale worker that resumes cannot
  switch an obsolete generation after a newer job has been claimed.

FTS rowids are taken from the canonical page rowid and every log entry carries
that rowid, making updates/deletes O(index lookup) rather than rescanning the
FTS table by page ID.

Retired FTS tables are deliberately retained rather than dropped in the swap
transaction. This avoids schema-lock surprises for an already-running query;
generation history is visible in `/api/index/status`.

## Publication correctness model

* **Snapshot, not a view** — a publication stores the exact redacted body and
  its source revision in `publications`; `public_fts` indexes that body. The
  public read path never recomputes from `pages`.
* **No write lock during redaction** — span validation and body reconstruction
  run on a consistent committed copy before any write transaction. The short
  `BEGIN IMMEDIATE` transaction re-reads the source revision and body; if a
  correction committed in the meantime, `expected_revision` fails and the
  request returns **409** without publishing.
* **One commit** — the publication row and its `public_fts` entry are inserted
  or replaced in the same transaction. A failure rolls both back, so no half
  publication is visible.
* **Automatic withdrawal** — `upsert_page` and `delete_page` delete the
  publication and its public FTS row inside their own write transaction. There
  is no window in which a corrected or deleted page has a stale public copy.
* **Late publishes cannot resurrect** — after any source write the revision
  changed, so a publish prepared with the old revision receives 409. A delete
  removes the row entirely; re-creating the same page ID starts a new revision
  and does not restore the old snapshot.
* **Independent of internal policy** — the public FTS table and query use a
  fixed `keep` representation. Publishing and internal generation rebuilds do
  not touch each other, and a `keep`/`fold` switch cannot change public results.
* **Durable** — publish and withdraw are committed SQLite rows. Startup never
  republishes from the source pages, so both published and withdrawn state
  survive a restart.
* **Snapshot-consistent public reads** — one read transaction pins the active
  publication rows and their FTS hits, so `total`, hits and bodies agree even
  while a publish/withdraw commits concurrently.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ARCHIVE_DB_PATH` | `./data/archive.db` locally, `/data/archive.db` in Docker | SQLite path. |
| `REBUILD_LEASE_SECONDS` | `20` | Heartbeat lease before a trigger may reclaim a job. |
| `REBUILD_BATCH_SIZE` | `500` | Scan/catch-up batch size. |
| `REBUILD_BATCH_DELAY_MS` | `0` | Demo-only pacing per batch. |
| `DB_BUSY_TIMEOUT_SECONDS` | `30` | SQLite writer wait. |
| `ARCHIVE_API_PORT` | `8000` | Management uvicorn port (`app.serve management`). |
| `ARCHIVE_PUBLIC_PORT` | `8080` | Read-only public uvicorn port (`app.serve public`). |
| `API_PORT` / `PUBLIC_PORT` | `8000` / `8080` | Compose host port mappings. |

## Local development and tests

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt

# Management service (two uvicorn workers)
ARCHIVE_DB_PATH=/tmp/archive-dev.db ARCHIVE_API_PORT=8000 \
  python3 -m app.serve management

# Read-only public service in another terminal (two uvicorn workers)
ARCHIVE_DB_PATH=/tmp/archive-dev.db ARCHIVE_PUBLIC_PORT=8080 \
  python3 -m app.serve public
```

```bash
pytest -q
```

The tests exercise exact vs folded search, a rebuild that finishes with a
correction and a deletion submitted during the scan, duplicate job idempotency,
and restart/stale-job recovery. Publication tests cover code-point spans,
422 validation, 409 stale revisions, atomic publish/withdraw, fixed-keep public
search, source-write/delete withdrawal, same-ID re-creation, restart durability
and the absence of original-page routes on the public app. They use temporary
SQLite files.
