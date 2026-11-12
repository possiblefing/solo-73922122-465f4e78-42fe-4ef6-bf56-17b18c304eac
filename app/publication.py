"""Redacted public projections: immutable snapshots and their public index.

A publication is a *snapshot* of one committed page revision with every
requested code-point span replaced by a single ``█``.  The redacted body is
stored durably in ``publications`` and indexed in the standalone ``public_fts``
table.  It is never recomputed from ``pages``.

That separation is intentional:

* the internal accent-policy generations and rebuilds cannot change it;
* every write or delete of the source page withdraws it, in the same write
  transaction, so a public copy can never outlive its source revision;
* because a withdrawal is a durable delete, restarting the service (or
  re-creating a page with the same ID) cannot resurrect the old publication;
* publishing re-checks the source revision under the write lock, so a request
  prepared against an older revision is rejected instead of overwriting a
  corrected publication.

Only ``publications`` and ``public_fts`` are read by the public endpoints.  No
title is ever stored or returned there.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .accents import build_match_expression, normalize_for_policy
from .db import begin_immediate, connect, utc_now

MAX_REDACTION_SPANS = 20
REDACTION_BLOCK = "█"


def _insert_public_row(
    conn: sqlite3.Connection, page_ref: int, page_id: str, redacted: str
) -> None:
    # The durable snapshot keeps the exact redacted code points; the index
    # uses the NFC ``keep`` representation so composed/decomposed spellings
    # match exactly as they do in the internal keep generation.
    conn.execute(
        "INSERT INTO public_fts(rowid, page_id, body) VALUES (?, ?, ?)",
        (page_ref, page_id, normalize_for_policy(redacted, "keep")),
    )


class SpanError(ValueError):
    """Invalid redaction spans; mapped to HTTP 422 by the API layer."""


class RevisionConflict(Exception):
    """The expected source revision is no longer current; mapped to HTTP 409."""

    def __init__(self, page_id: str, expected_revision: int, current_revision: int):
        self.page_id = page_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"page {page_id!r} revision is {current_revision}, "
            f"not the expected {expected_revision}"
        )


def _validated_spans(
    body: str | None, spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Sort and validate spans. ``body=None`` checks structure only."""

    if len(spans) > MAX_REDACTION_SPANS:
        raise SpanError(f"at most {MAX_REDACTION_SPANS} redaction spans are allowed")

    ordered = sorted((int(start), int(end)) for start, end in spans)
    previous_end = -1
    for start, end in ordered:
        if start < 0 or end < 0:
            raise SpanError("span offsets must be non-negative")
        if start >= end:
            raise SpanError(f"span {start}:{end} is empty; start must be less than end")
        if start < previous_end:
            raise SpanError(f"span {start}:{end} overlaps another span")
        previous_end = end
    if body is not None:
        for start, end in ordered:
            if end > len(body):
                raise SpanError(
                    f"span {start}:{end} exceeds the body length of "
                    f"{len(body)} code points"
                )
    return ordered


def apply_redactions(body: str, spans: list[tuple[int, int]]) -> str:
    """Replace each validated half-open code-point span with one block.

    Spans are Unicode code-point offsets into ``body``.  They must be
    non-empty, inside the body, and pairwise non-overlapping.  Order in the
    request does not matter.  Adjacent spans are allowed: ``[0, 1]`` and
    ``[1, 2]`` may both be redacted and each yields its own block.
    """

    ordered = _validated_spans(body, spans)
    pieces: list[str] = []
    cursor = 0
    for start, end in ordered:
        pieces.append(body[cursor:start])
        pieces.append(REDACTION_BLOCK)
        cursor = end
    pieces.append(body[cursor:])
    return "".join(pieces)


def withdraw_publication(conn: sqlite3.Connection, page_id: str) -> bool:
    """Remove the public projection inside the caller's write transaction."""

    row = conn.execute(
        "SELECT page_ref FROM publications WHERE page_id = ?", (page_id,)
    ).fetchone()
    if row is not None and row["page_ref"] is not None:
        conn.execute(
            "DELETE FROM public_fts WHERE rowid = ?", (int(row["page_ref"]),)
        )
    # Also by page_id: this repairs any legacy row that predates page_ref and
    # covers the idempotent-delete-of-an-absent-page case.
    conn.execute("DELETE FROM public_fts WHERE page_id = ?", (page_id,))
    cur = conn.execute("DELETE FROM publications WHERE page_id = ?", (page_id,))
    return cur.rowcount > 0


def publish(
    page_id: str, expected_revision: int, spans: list[tuple[int, int]]
) -> dict[str, Any]:
    """Publish a redacted snapshot, or report a stale revision conflict.

    Redaction runs on the committed source body *outside* any database write
    lock.  The short write transaction then re-reads the source revision and
    body.  If another writer committed in between, the revision check fails
    and nothing is published.
    """

    if len(spans) > MAX_REDACTION_SPANS:
        raise SpanError(f"at most {MAX_REDACTION_SPANS} redaction spans are allowed")

    # Structure can be checked immediately, before reading the database.  The
    # authoritative bounds check happens against the re-read body inside the
    # write transaction, because a correction may change the body length.
    _validated_spans(None, spans)

    source_conn = connect(query_only=True)
    source_body: str | None
    try:
        row = source_conn.execute(
            "SELECT body, rev FROM pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        if row is None:
            raise LookupError(page_id)
        current_revision = int(row["rev"])
        if current_revision != expected_revision:
            raise RevisionConflict(page_id, expected_revision, current_revision)
        source_body = str(row["body"])
    finally:
        source_conn.close()

    # Pure CPU work while no write lock is held.
    redacted = apply_redactions(source_body, spans)

    conn = connect()
    try:
        begin_immediate(conn)
        row = conn.execute(
            "SELECT rowid AS page_ref, body, rev FROM pages WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if row is None:
            raise LookupError(page_id)
        current_revision = int(row["rev"])
        if current_revision != expected_revision:
            raise RevisionConflict(page_id, expected_revision, current_revision)
        if str(row["body"]) != source_body:
            raise RevisionConflict(page_id, expected_revision, current_revision)
        page_ref = int(row["page_ref"])

        existing = conn.execute(
            "SELECT source_rev, body FROM publications WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if (
            existing is not None
            and int(existing["source_rev"]) == current_revision
            and str(existing["body"]) == redacted
        ):
            conn.commit()
            return {
                "page_id": page_id,
                "revision": current_revision,
                "published": True,
                "already_published": True,
                "published_body": redacted,
                "redaction_count": len(spans),
                "published_at": conn.execute(
                    "SELECT published_at FROM publications WHERE page_id = ?", (page_id,)
                ).fetchone()["published_at"],
            }

        now = utc_now()
        # The public FTS rowid is the canonical page rowid.  Deleting by
        # page_id first also repairs a legacy public row from before a
        # replacement, so at most one live public row can exist per page.
        conn.execute("DELETE FROM public_fts WHERE page_id = ?", (page_id,))
        conn.execute(
            """
            INSERT OR REPLACE INTO publications(page_id, page_ref, source_rev, body, published_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (page_id, page_ref, current_revision, redacted, now),
        )
        _insert_public_row(conn, page_ref, page_id, redacted)
        conn.commit()
        return {
            "page_id": page_id,
            "revision": current_revision,
            "published": True,
            "already_published": False,
            "published_body": redacted,
            "redaction_count": len(spans),
            "published_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def withdraw(page_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    """Withdraw a publication (idempotent) in one short transaction."""

    conn = connect()
    try:
        begin_immediate(conn)
        row = conn.execute(
            "SELECT source_rev FROM publications WHERE page_id = ?", (page_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return {"page_id": page_id, "withdrawn": False, "existed": False}
        if expected_revision is not None and int(row["source_rev"]) != expected_revision:
            raise RevisionConflict(page_id, expected_revision, int(row["source_rev"]))
        withdraw_publication(conn, page_id)
        conn.commit()
        return {
            "page_id": page_id,
            "withdrawn": True,
            "existed": True,
            "revision": int(row["source_rev"]),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_public_page(page_id: str) -> dict[str, Any] | None:
    conn = connect(query_only=True)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT page_id, body, source_rev, published_at "
            "FROM publications WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        conn.execute("COMMIT")
        if row is None:
            return None
        return {
            "page_id": row["page_id"],
            "body": row["body"],
            "revision": int(row["source_rev"]),
            "published_at": row["published_at"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search_public(raw_query: str, *, limit: int, offset: int) -> dict[str, Any]:
    """Word-AND search over redacted public bodies only.

    The rule is fixed to ``keep``.  It does not follow the internal
    ``active_policy`` and therefore never changes when an internal index
    generation is rebuilt.
    """

    expression, terms = build_match_expression(raw_query, "keep")
    conn = connect(query_only=True)
    try:
        # One read transaction pins the hit set and the returned redacted
        # bodies to the same committed publish/withdraw state.
        conn.execute("BEGIN")
        total = int(
            conn.execute(
                """
                SELECT COUNT(*)
                  FROM public_fts AS f
                  JOIN publications AS p ON p.page_id = f.page_id
                 WHERE public_fts MATCH ?
                """,
                (expression,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT p.page_id, p.body, p.source_rev, p.published_at,
                   bm25(public_fts) AS score
              FROM public_fts AS f
              JOIN publications AS p ON p.page_id = f.page_id
             WHERE public_fts MATCH ?
             ORDER BY score ASC, p.page_id ASC
             LIMIT ? OFFSET ?
            """,
            (expression, limit, offset),
        ).fetchall()
        conn.execute("COMMIT")
        return {
            "query": raw_query,
            "query_terms": terms,
            "accent_policy": "keep",
            "total": total,
            "limit": limit,
            "offset": offset,
            "hits": [
                {
                    "page_id": row["page_id"],
                    "body": row["body"],
                    "revision": int(row["source_rev"]),
                    "published_at": row["published_at"],
                    "score": float(row["score"]),
                }
                for row in rows
            ],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
