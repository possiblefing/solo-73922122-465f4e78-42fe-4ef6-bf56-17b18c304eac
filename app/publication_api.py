"""Management endpoints for redacted publication.

Kept in a router module so the management app and the read-only public app stay
clearly separated.  None of these routes exist on the public port.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .publication import (
    MAX_REDACTION_SPANS,
    RevisionConflict,
    SpanError,
    get_public_page,
    publish,
    withdraw,
)

logger = logging.getLogger("archive.publication.api")

router = APIRouter()


class SpanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(
        ...,
        ge=0,
        description="Inclusive start offset in Unicode code points.",
    )
    end: int = Field(
        ...,
        description="Exclusive end offset in Unicode code points; must be greater than start.",
    )

    @model_validator(mode="after")
    def _non_empty(self) -> "SpanModel":
        if self.end <= self.start:
            raise ValueError("end must be greater than start (empty span)")
        return self


class RedactionPublish(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Publish with four redacted words",
                    "value": {
                        "expected_revision": 1,
                        "spans": [
                            {"start": 3, "end": 7},
                            {"start": 32, "end": 37},
                            {"start": 45, "end": 50},
                            {"start": 51, "end": 57},
                        ],
                    },
                },
                {
                    "summary": "Invalid: the span overlaps the previous one",
                    "value": {
                        "expected_revision": 1,
                        "spans": [{"start": 3, "end": 10}, {"start": 8, "end": 12}],
                    },
                },
                {
                    "summary": "Invalid: a span is empty",
                    "value": {"expected_revision": 1, "spans": [{"start": 4, "end": 4}]},
                },
            ]
        },
    )

    expected_revision: int = Field(
        ...,
        ge=1,
        description=(
            "The page revision the spans were computed against. It must equal "
            "the currently committed revision; otherwise HTTP 409 is returned "
            "and nothing is published."
        ),
        examples=[1],
    )
    spans: list[SpanModel] = Field(
        ...,
        max_length=MAX_REDACTION_SPANS,
        description=(
            "At most 20 half-open Unicode code-point spans. Each non-empty span "
            "is replaced by exactly one █ in the published body. Spans must not "
            "overlap and must not exceed the body. They may be listed in any "
            "order. Offsets count Unicode code points, not UTF-16 units or bytes."
        ),
    )


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Withdraw after correcting a published page",
                    "value": {"expected_revision": 3},
                },
                {"summary": "Unconditional idempotent withdrawal", "value": {}},
            ]
        },
    )

    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional guard: the publication's source revision must equal this "
            "value, else HTTP 409. Omit for an unconditional idempotent withdrawal."
        ),
    )


def _conflict_detail(exc: RevisionConflict) -> dict:
    return {
        "message": str(exc),
        "page_id": exc.page_id,
        "expected_revision": exc.expected_revision,
        "current_revision": exc.current_revision,
    }


@router.post(
    "/api/publications/{page_id}",
    tags=["publication"],
    summary="Publish a redacted snapshot for the read-only public port",
    response_description=(
        "The redacted body that the public port will serve. The original "
        "archive page is unchanged. A stale expected_revision returns HTTP 409."
    ),
    responses={
        200: {"description": "Redacted snapshot published (or already current)."},
        404: {"description": "The source page does not exist."},
        409: {
            "description": (
                "The expected revision is no longer current. Nothing was "
                "published; publish again with the corrected revision."
            )
        },
        422: {"description": "Empty, overlapping, out-of-bounds, or too many spans."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "examples": {
                        "publish_four_words": {
                            "summary": "Redact café, señor, naïve and résumé",
                            "description": (
                                "Offsets are Unicode code points in the body "
                                "`Le café est près de la gare; le señor lit un "
                                "naïve résumé.` (58 code points): café 3:7, "
                                "señor 32:37, naïve 45:50, résumé 51:57."
                            ),
                            "value": {
                                "expected_revision": 1,
                                "spans": [
                                    {"start": 3, "end": 7},
                                    {"start": 32, "end": 37},
                                    {"start": 45, "end": 50},
                                    {"start": 51, "end": 57},
                                ],
                            },
                        },
                        "reject_overlap": {
                            "summary": "422 because 8:12 overlaps 3:10",
                            "value": {
                                "expected_revision": 1,
                                "spans": [
                                    {"start": 3, "end": 10},
                                    {"start": 8, "end": 12},
                                ],
                            },
                        },
                        "reject_out_of_bounds": {
                            "summary": "422 because 57:70 exceeds the 58-code-point body",
                            "value": {
                                "expected_revision": 1,
                                "spans": [{"start": 57, "end": 70}],
                            },
                        },
                        "stale_revision": {
                            "summary": "409 after a correction changed the revision",
                            "description": (
                                "Correct the page first, then resend this "
                                "request with expected_revision 1."
                            ),
                            "value": {
                                "expected_revision": 1,
                                "spans": [{"start": 3, "end": 7}],
                            },
                        },
                    }
                }
            },
        }
    },
)
def publish_redacted_page(
    page_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=512,
            description="Source page ID.",
            examples=["ocr-001"],
        ),
    ],
    payload: Annotated[
        RedactionPublish,
        Body(description="Expected source revision and the code-point spans to redact."),
    ],
) -> dict:
    try:
        return publish(
            page_id,
            payload.expected_revision,
            [(span.start, span.end) for span in payload.spans],
        )
    except SpanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=_conflict_detail(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="page not found") from exc
    except Exception:
        logger.exception("publication failed for %s", page_id)
        raise


@router.delete(
    "/api/publications/{page_id}",
    tags=["publication"],
    summary="Withdraw a published snapshot (idempotent)",
    response_description=(
        "The public body and its search entry are removed together. Correcting "
        "the source page and withdrawing leaves no stale public version."
    ),
    responses={
        200: {"description": "Withdrawn, or already absent."},
        409: {"description": "expected_revision does not match the publication."},
    },
)
def withdraw_redacted_page(
    page_id: Annotated[
        str,
        Path(min_length=1, max_length=512, examples=["ocr-001"]),
    ],
    payload: Annotated[
        WithdrawRequest | None,
        Body(description="Optional expected_revision guard. The body may be omitted."),
    ] = None,
) -> dict:
    try:
        return withdraw(page_id, payload.expected_revision if payload else None)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=_conflict_detail(exc)) from exc
    except Exception:
        logger.exception("publication withdrawal failed for %s", page_id)
        raise


@router.get(
    "/api/publications/{page_id}",
    tags=["publication"],
    summary="Inspect the currently published redacted body (management view)",
    responses={404: {"description": "No current publication for this page."}},
)
def read_publication(
    page_id: Annotated[str, Path(min_length=1, max_length=512, examples=["ocr-001"])],
) -> dict:
    page = get_public_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="publication not found")
    return page
