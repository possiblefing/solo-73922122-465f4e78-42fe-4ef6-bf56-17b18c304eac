"""Read-only public entrypoint.

This application deliberately exposes only two data operations over published
content:

* ``GET /public/pages/{page_id}`` reads one published, already-redacted body;
* ``GET /public/search`` performs a fixed-``keep`` word-AND search over the
  published bodies.

It has no route for the original page (neither body nor title), no title field
in any response, and no access to the internal accent-policy generations.  It
never opens a writable connection.  Run it on a separate port from the
management API; see ``app.serve``.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .db import connect
from .publication import get_public_page, search_public

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(process)d %(message)s",
)
logger = logging.getLogger("archive.public")

PUBLIC_DESCRIPTION = """
Read-only access to **published, redacted OCR bodies**.

This port intentionally exposes only:

* **By-page read** — `GET /public/pages/{page_id}`.
* **Word search** — `GET /public/search?q=word1+word2`.

There is no original-page endpoint, no title is stored or returned, and no
management operation is available here.

### Search rule

Queries are split into literal words. Every word must occur (AND), and the
accent rule is **fixed to `keep`**: `café` and `cafe` are different words.
Changing the internal accent policy or rebuilding an internal index generation
does not change this port's results.

Search and result bodies come from the *redacted* published text only. Text
removed by a redaction span is not searchable. Hits and total counts are read
in the same committed snapshot as the returned bodies.

### Publication lifecycle

A page becomes visible here only after the archive staff publish a redacted
snapshot through the management API. Any later correction or deletion of the
source page immediately withdraws that public snapshot. A restart preserves
both published and withdrawn state.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Deliberately no schema creation or any other write.  The management
    # service owns the database; this process only opens read connections.
    logger.info("public read-only entrypoint ready")
    yield


app = FastAPI(
    title="OCR Archive — Public Read-Only Access",
    version="1.0.0",
    description=PUBLIC_DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "public", "description": "Read published redacted bodies and search them."},
        {"name": "ops", "description": "Health checks."},
    ],
)


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_error(_request: Request, exc: sqlite3.OperationalError):
    logger.error("public read failed: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "The published projection is not ready yet. Start the "
            "management service so it can initialize the archive database.",
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["ops"], summary="Public entrypoint health")
def health() -> dict:
    conn = connect(query_only=True)
    try:
        conn.execute("SELECT 1")
        count = int(
            conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        )
        return {"status": "ok", "read_only": True, "published_pages": count}
    finally:
        conn.close()


@app.get(
    "/public/pages/{page_id}",
    tags=["public"],
    summary="Read one published, redacted page body",
    response_description="The redacted body only; this API never returns a title.",
)
def read_public_page(
    page_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=512,
            description="Page ID whose redacted publication should be read.",
            examples=["ocr-001"],
        ),
    ],
) -> dict:
    page = get_public_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="publication not found")
    return page


@app.get(
    "/public/search",
    tags=["public"],
    summary="Word-AND search over published redacted bodies (fixed keep rule)",
    response_description=(
        "Total and hits are computed from the redacted public index only. "
        "Bodies and revisions are read in the same snapshot as the hits."
    ),
    openapi_extra={
        "parameters": [
            {
                "name": "q",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 2048},
                "examples": {
                    "redacted_word_absent": {
                        "summary": "A redacted word is not searchable",
                        "value": "café",
                    },
                    "remaining_word_present": {
                        "summary": "A word outside every span still matches",
                        "value": "gare",
                    },
                    "and_all_words": {
                        "summary": "All words must occur",
                        "value": "gare señor",
                    },
                    "exact_accent_keep": {
                        "summary": "Accents are exact (fixed keep rule)",
                        "value": "resume",
                    },
                },
            }
        ]
    },
)
def public_search(
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=2048,
            description="One or more literal words. All must occur. The accent "
            "rule is fixed to keep, independent of the management service policy.",
            examples=["gare señor"],
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=100, description="Maximum hits to return.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Number of ordered hits to skip.")] = 0,
) -> dict:
    try:
        return search_public(q, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("public search failed for query %r", q)
        raise
