"""Redacted publication, withdrawal and the read-only public entrypoint."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import configure_database

BODY = "Le café est près de la gare; le señor lit un naïve résumé."
SPANS = [(3, 7), (32, 37), (45, 50), (51, 57)]
REDACTED = "Le █ est près de la gare; le █ lit un █ █."


def _put_page(client: TestClient, page_id: str, body: str, title: str = "") -> dict:
    response = client.put(f"/api/pages/{page_id}", json={"title": title, "body": body})
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client: TestClient, page_id: str, revision: int, spans=SPANS) -> dict:
    response = client.post(
        f"/api/publications/{page_id}",
        json={
            "expected_revision": revision,
            "spans": [{"start": start, "end": end} for start, end in spans],
        },
    )
    return {"status": response.status_code, "json": response.json()}


def _public_search(public_host: TestClient, query: str) -> dict:
    response = public_host.get("/public/search", params={"q": query})
    assert response.status_code == 200, response.text
    return response.json()


def test_publish_replaces_each_span_with_one_block_and_leaves_source_unchanged(
    client: TestClient,
):
    page = _put_page(client, "ocr-001", BODY)
    assert page["revision"] == 1

    result = _publish(client, "ocr-001", page["revision"])
    assert result["status"] == 200, result
    assert result["json"]["published_body"] == REDACTED
    assert result["json"]["redaction_count"] == 4
    assert "█" in result["json"]["published_body"]
    assert "café" not in result["json"]["published_body"]

    # The original archive page is untouched.
    stored = client.get("/api/pages/ocr-001").json()
    assert stored["body"] == BODY
    assert stored["revision"] == page["revision"]
    # Management search still sees unredacted words.
    assert client.get("/api/search", params={"q": "café"}).json()["total"] == 1


def test_spans_are_code_points_not_utf16_units(client: TestClient):
    # A non-BMP character before the span must not shift a code-point offset.
    body = "𝄞 then secret"
    page = _put_page(client, "astral", body)
    start = body.index("secret")
    result = _publish(client, "astral", page["revision"], [(start, start + 6)])
    assert result["status"] == 200, result
    assert result["json"]["published_body"] == "𝄞 then █"


@pytest.mark.parametrize(
    "spans,expected",
    [
        ([(3, 6), (6, 9)], "abc██j"),  # adjacent, ascending
        ([(6, 9), (3, 6)], "abc██j"),  # same spans in reverse request order
        ([(0, 1), (1, 2), (2, 3)], "███defghij"),  # each yields its own block
    ],
)
def test_adjacent_spans_are_allowed_in_any_order(client: TestClient, spans, expected):
    page = _put_page(client, "adjacent", "abcdefghij")
    result = _publish(client, "adjacent", page["revision"], spans)
    assert result["status"] == 200, result
    assert result["json"]["published_body"] == expected


@pytest.mark.parametrize(
    "spans",
    [
        [(3, 3)],  # empty
        [(0, 4), (3, 8)],  # overlap
        [(3, 7), (5, 9)],  # overlap in request order
        [(2, 4), (-1, 1)],  # negative
        [(3, 99)],  # out of bounds
    ],
)
def test_invalid_spans_return_422_and_publish_nothing(client: TestClient, spans):
    page = _put_page(client, "bad-span", BODY)
    result = _publish(client, "bad-span", page["revision"], spans)
    assert result["status"] == 422, result
    assert client.get("/api/publications/bad-span").status_code == 404


def test_publish_unknown_page_returns_404(client: TestClient):
    result = _publish(client, "missing", 1)
    assert result["status"] == 404, result
    assert client.get("/api/publications/missing").status_code == 404


def test_replacing_a_publication_updates_body_and_index_together(client: TestClient):
    from app.public import app as public_app

    page = _put_page(client, "ocr-001", "alpha beta gamma")
    assert _publish(client, "ocr-001", page["revision"], [(0, 5)])["status"] == 200

    # Same revision, different spans: the publication is replaced in one
    # transaction, so the old redacted text stops matching immediately.
    replacement = _publish(client, "ocr-001", page["revision"], [(6, 10)])
    assert replacement["status"] == 200, replacement
    assert replacement["json"]["published_body"] == "alpha █ gamma"

    with TestClient(public_app) as public_client:
        assert _public_search(public_client, "alpha")["total"] == 1  # no longer redacted
        assert _public_search(public_client, "beta")["total"] == 0  # now redacted
        assert _public_search(public_client, "gamma")["total"] == 1
        assert _public_search(public_client, "gamma")["hits"][0]["body"] == "alpha █ gamma"


def test_more_than_twenty_spans_is_rejected(client: TestClient):
    body = "a" * 40
    page = _put_page(client, "many", body)
    spans = [(index * 2, index * 2 + 1) for index in range(21)]
    result = _publish(client, "many", page["revision"], spans)
    assert result["status"] == 422, result

    twenty = spans[:20]
    result = _publish(client, "many", page["revision"], twenty)
    assert result["status"] == 200, result


def test_stale_revision_returns_409_and_keeps_the_previous_publication(
    client: TestClient,
):
    assert _put_page(client, "ocr-001", BODY)["revision"] == 1
    assert _publish(client, "ocr-001", 1)["status"] == 200

    corrected = client.put(
        "/api/pages/ocr-001",
        json={"title": "corrected", "body": "Le café est près de la gare."},
    ).json()
    assert corrected["revision"] != 1

    stale = _publish(client, "ocr-001", 1, [(3, 7)])
    assert stale["status"] == 409, stale
    assert stale["json"]["detail"]["current_revision"] == corrected["revision"]

    # The source correction already withdrew the old public snapshot.
    assert client.get("/api/publications/ocr-001").status_code == 404

    fresh = _publish(client, "ocr-001", corrected["revision"], [(3, 7)])
    assert fresh["status"] == 200, fresh
    assert fresh["json"]["published_body"] == "Le █ est près de la gare."


def test_publish_rechecks_revision_after_computing_redaction(
    client: TestClient, monkeypatch
):
    """A write that commits between preparation and the write lock must lose."""
    from app import publication

    revision = _put_page(client, "race", "original secret")["revision"]

    real_apply = publication.apply_redactions

    def slow_apply(body, spans):
        result = real_apply(body, spans)
        # Simulate a concurrent correction right before the write transaction.
        from app.archive import upsert_page

        upsert_page("race", "", "corrected body")
        return result

    monkeypatch.setattr(publication, "apply_redactions", slow_apply)
    result = _publish(client, "race", revision, [(9, 15)])
    assert result["status"] == 409, result
    assert client.get("/api/publications/race").status_code == 404


def test_public_port_is_read_only_and_never_returns_a_title(client: TestClient):
    from app.public import app as public_app

    page = _put_page(client, "ocr-001", BODY, title="Crème brûlée à Paris")
    assert _publish(client, "ocr-001", page["revision"])["status"] == 200

    with TestClient(public_app) as public_client:
        openapi_paths = set(public_client.get("/openapi.json").json()["paths"])
        assert openapi_paths == {"/healthz", "/public/pages/{page_id}", "/public/search"}

        read = public_client.get("/public/pages/ocr-001")
        assert read.status_code == 200
        assert set(read.json()) == {"page_id", "body", "revision", "published_at"}
        assert read.json()["body"] == REDACTED
        assert "title" not in read.json()

        # The original page endpoint does not exist on this port.
        assert public_client.get("/api/pages/ocr-001").status_code == 404
        assert public_client.get("/api/search", params={"q": "café"}).status_code == 404


def test_public_search_uses_redacted_text_only_and_fixed_keep_rule(
    client: TestClient,
):
    from app.public import app as public_app

    first = _put_page(client, "ocr-001", BODY)
    second = _put_page(client, "ocr-002", "This page uses cafe and naive without accents.")

    publish_first = _publish(client, "ocr-001", first["revision"])
    publish_second = _publish(client, "ocr-002", second["revision"], [])
    assert publish_first["status"] == publish_second["status"] == 200

    with TestClient(public_app) as public_client:
        redacted_query = _public_search(public_client, "señor")
        assert redacted_query["total"] == 0
        assert redacted_query["hits"] == []

        # The unredacted occurrence in ocr-002 is still indexed.
        assert _public_search(public_client, "cafe")["hits"][0]["page_id"] == "ocr-002"

        for hit in _public_search(public_client, "gare")["hits"]:
            assert set(hit) == {"page_id", "body", "revision", "published_at", "score"}
            assert "█" in hit["body"]

        # Fixed keep: accented café is redacted in ocr-001, but the plain word
        # in ocr-002 must not fold-match an accented query.
        assert _public_search(public_client, "café")["total"] == 0

        ANDed = _public_search(public_client, "gare señor")
        assert ANDed["total"] == 0  # señor was redacted from ocr-001

        assert _public_search(public_client, "page uses")["total"] == 1


def test_internal_policy_switch_does_not_change_public_search(client: TestClient):
    from app.public import app as public_app

    first = _put_page(client, "ocr-001", BODY)
    assert _publish(client, "ocr-001", first["revision"])["status"] == 200
    with TestClient(public_app) as public_client:
        assert _public_search(public_client, "resume")["total"] == 0

    trigger = client.post("/api/index/rebuild", json={"accent_policy": "fold"})
    assert trigger.status_code == 202
    job_id = trigger.json()["job"]["job_id"]

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = client.get("/api/index/status").json()
        if status["active_policy"] == "fold":
            break
        time.sleep(0.03)
    else:
        raise AssertionError(f"rebuild {job_id} did not finish")

    # Internal search now folds; the public projection does not.
    internal = client.get("/api/search", params={"q": "resume"}).json()
    assert internal["total"] == 1
    assert internal["accent_policy"] == "fold"

    with TestClient(public_app) as public_client:
        assert _public_search(public_client, "resume")["total"] == 0
        assert _public_search(public_client, "résumé")["total"] == 0  # redacted
        assert _public_search(public_client, "gare")["accent_policy"] == "keep"


def test_write_and_delete_withdraw_public_version_and_same_id_cannot_revive_it(
    client: TestClient,
):
    from app.public import app as public_app

    first = _put_page(client, "ocr-001", BODY)
    assert _publish(client, "ocr-001", first["revision"])["status"] == 200

    corrected = _put_page(client, "ocr-001", "Le secret est ailleurs.")
    with TestClient(public_app) as public_client:
        assert public_client.get("/public/pages/ocr-001").status_code == 404
        assert _public_search(public_client, "gare")["total"] == 0

    # A stale "late publish" request cannot bring the old snapshot back.
    late = _publish(client, "ocr-001", first["revision"])
    assert late["status"] == 409, late

    # Re-publishing with the current revision is possible, but only the new
    # redacted content becomes public.
    republished = _publish(client, "ocr-001", corrected["revision"], [(3, 9)])
    assert republished["status"] == 200, republished
    assert republished["json"]["published_body"] == "Le \u2588 est ailleurs."

    delete = client.delete("/api/pages/ocr-001")
    assert delete.status_code == 200
    with TestClient(public_app) as public_client:
        assert public_client.get("/public/pages/ocr-001").status_code == 404
        assert _public_search(public_client, "ailleurs")["total"] == 0

    # Recreating the same page ID and rebuilding does not restore the old
    # publication; a new publish requires the new revision.
    recreated = _put_page(client, "ocr-001", BODY)
    with TestClient(public_app) as public_client:
        assert public_client.get("/public/pages/ocr-001").status_code == 404
    assert _publish(client, "ocr-001", first["revision"])["status"] == 409
    assert _publish(client, "ocr-001", recreated["revision"])["status"] == 200


def test_withdraw_is_idempotent_and_can_be_revision_guarded(client: TestClient):
    _put_page(client, "ocr-001", BODY)
    assert _publish(client, "ocr-001", 1)["status"] == 200

    guarded = client.request(
        "DELETE",
        "/api/publications/ocr-001",
        json={"expected_revision": 99},
    )
    assert guarded.status_code == 409, guarded
    assert client.get("/api/publications/ocr-001").status_code == 200

    withdrawn = client.delete("/api/publications/ocr-001")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["withdrawn"] is True
    again = client.delete("/api/publications/ocr-001")
    assert again.status_code == 200
    assert again.json()["withdrawn"] is False
    assert client.get("/api/publications/ocr-001").status_code == 404


def test_publication_survives_restart_and_withdrawal_stays_withdrawn(tmp_path):
    from app import archive, publication
    from app.db import init_db
    from app.public import app as public_app

    configure_database(tmp_path / "durable.db")
    init_db()
    page = archive.upsert_page("kept", "", BODY)
    publication.publish("kept", page["revision"], SPANS)

    gone = archive.upsert_page("gone", "", BODY)
    publication.publish("gone", gone["revision"], SPANS)
    archive.delete_page("gone")

    # Re-run startup schema/boostrap exactly as a restarted process would.
    init_db()
    configure_database(tmp_path / "durable.db")

    with TestClient(public_app) as public_client:
        assert public_client.get("/public/pages/kept").json()["body"] == REDACTED
        assert public_client.get("/public/pages/gone").status_code == 404
        assert _public_search(public_client, "gare")["total"] == 1
        assert _public_search(public_client, "gare")["hits"][0]["page_id"] == "kept"


def test_failed_publish_leaves_no_partial_publication(client: TestClient, monkeypatch):
    from app import publication

    page = _put_page(client, "ocr-001", BODY)

    def broken_insert(conn, *args, **kwargs):
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(publication, "_insert_public_row", broken_insert, raising=False)
    try:
        result = _publish(client, "ocr-001", page["revision"])
    except RuntimeError:
        # TestClient re-raises server exceptions when raise_server_exceptions
        # is left at its default.  The endpoint itself returned 500.
        result = {"status": 500}
    assert result["status"] == 500
    assert client.get("/api/publications/ocr-001").status_code == 404
