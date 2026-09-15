"""Gmail adapter contract (spec §4.1, §4.2, §12.3).

Normalization, typed errors, capability gating, history pagination with duplicates and out-of-order
records, invalid-cursor detection, and the Message-ID reconciliation query. Every test runs against
FakeGmail; no live provider call happens here.
"""
from __future__ import annotations

import base64

import pytest

from backend.app.adapters import gmail as gmail_mod
from backend.app.adapters.gmail import (FakeGmail, GmailAdapter, build, build_mime, error_kind,
                                        html_to_text, make_message_id, normalize_message, strip_quoted)
from backend.app.core.errors import ProviderError, Unsupported
from backend.app.models.comms import Connection
from backend.tests import fixtures_gmail as fx


def test_normalize_message_parses_headers_bodies_and_attachments():
    norm = normalize_message(fx.SUPPLIER_LIST)
    assert norm["provider_message_id"] == "m-supplier"
    assert norm["thread_id"] == "t-supplier"
    assert norm["from"] == "kenji@exporter.example.jp"
    assert norm["to"] == [fx.BUSINESS]
    assert norm["subject"] == "Shipment manifest and invoices"
    assert "STK-0500" in norm["text"]
    assert norm["headers"]["message-id"] == "<m-supplier@mail.example>"
    assert norm["date"] is not None and norm["date"].tzinfo is not None
    assert [a["filename"] for a in norm["attachments"]] == ["INV-7781.pdf"]
    assert norm["attachments"][0]["attachment_id"]


def test_normalize_message_keeps_only_declared_headers():
    norm = normalize_message(fx.AUTO_REPLY)
    assert norm["headers"]["auto-submitted"] == "auto-replied"
    assert norm["headers"]["precedence"] == "bulk"
    # subject/from/to are promoted to fields, they are not left in the raw header bag
    assert "subject" not in norm["headers"] and "from" not in norm["headers"]


def test_html_only_message_falls_back_to_text_and_quotes_are_stripped():
    raw = fx.raw_message("m-html", "t-html", from_addr="a@b.example", to=fx.BUSINESS, subject="hi",
                         text="", html="<p>Hello <b>there</b></p><br>Line two")
    norm = normalize_message(raw)
    assert "Hello there" in norm["text"] and "<p>" not in norm["text"]
    assert html_to_text("<script>x</script><p>Keep</p>") == "Keep"
    quoted = "My answer is yes.\n\nOn Tue, Dylan wrote:\n> old text\n> more old text"
    assert strip_quoted(quoted) == "My answer is yes."


def test_build_mime_carries_our_message_id_and_threading_headers():
    mid = make_message_id("azkeitrucks.com")
    raw = build_mime(to=["maria@example.com"], subject="Re: hi", body="Hello", from_addr=fx.BUSINESS,
                     message_id=mid, in_reply_to="<prev@mail.example>")
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert f"Message-ID: {mid}" in decoded
    assert "In-Reply-To: <prev@mail.example>" in decoded
    assert "References: <prev@mail.example>" in decoded
    assert mid.startswith("<") and mid.endswith("@azkeitrucks.com>")


async def test_history_pages_and_converges_over_duplicate_and_out_of_order_records():
    fake = FakeGmail(fx.business_fixture())
    seen, token, pages = [], None, 0
    while True:
        page = await fake.history("100", page_token=token)
        pages += 1
        for rec in page["history"]:
            for added in rec.get("messagesAdded") or []:
                seen.append(added["message"]["id"])
        token = page["next_page_token"]
        if not token:
            break
    assert pages > 1, "the fixture must paginate"
    assert len(seen) > len(set(seen)), "the fixture must contain a duplicate history record"
    assert set(seen) == set(fx.business_fixture()["messages"])


async def test_invalid_history_cursor_is_a_typed_schema_changed_error():
    fake = FakeGmail({**fx.business_fixture(), "invalid_history_before": "500"})
    with pytest.raises(ProviderError) as e:
        await fake.history("100")
    assert error_kind(e.value) == "schema_changed"
    assert e.value.detail["code"] == "history_invalid"


async def test_metadata_format_never_returns_a_body():
    fake = FakeGmail(fx.business_fixture())
    meta = await fake.get_message("m-mixed", format="metadata")
    assert meta["from"] == "maria.chen@example.com" and meta["subject"]
    assert meta["text"] == "" and meta["attachments"] == []
    assert fake.fetched_metadata == ["m-mixed"] and fake.fetched_full == []


async def test_unsupported_capabilities_are_reported_not_faked():
    fake = FakeGmail(fx.business_fixture(), capabilities={"read": True, "send": False, "drafts": False, "labels": False})
    with pytest.raises(Unsupported) as e:
        await fake.modify_labels("m-mixed", add=["Label_1"])
    assert e.value.detail["capability"] == "labels"
    with pytest.raises(Unsupported):
        await fake.send(build_mime(to=["x@example.com"], subject="s", body="b"))
    with pytest.raises(Unsupported):
        await fake.create_draft(raw="x")
    with pytest.raises(Unsupported):
        await fake.watch("")


async def test_send_records_our_message_id_and_reconciliation_finds_it_once():
    fake = FakeGmail(fx.business_fixture())
    mid = make_message_id()
    raw = build_mime(to=["maria.chen@example.com"], subject="Re: hi", body="Hello", message_id=mid)
    receipt = await fake.send(raw, thread_id="t-mixed")
    assert receipt["message_id"] and receipt["thread_id"] == "t-mixed"
    found = await fake.find_sent_by_message_id(mid)
    assert found["message_id"] == receipt["message_id"]
    assert await fake.find_sent_by_message_id(make_message_id()) is None
    assert len(fake.sent) == 1


async def test_unknown_result_after_send_still_leaves_the_message_findable():
    fake = FakeGmail(fx.business_fixture())
    fake.send_then_lose_result = True
    mid = make_message_id()
    with pytest.raises(ProviderError) as e:
        await fake.send(build_mime(to=["maria.chen@example.com"], subject="s", body="b", message_id=mid))
    assert error_kind(e.value) == "unknown_result"
    # the provider did accept it: reconciliation must find exactly one copy and never resend
    found = await fake.find_sent_by_message_id(mid)
    assert found is not None and len(fake.sent) == 1


async def test_build_without_a_connection_is_setup_blocked_not_a_fake_success(db):
    gmail_mod.FACTORY = None
    conn = Connection(provider="gmail_business", label="business", status="disconnected", environment="test")
    db.add(conn)
    await db.commit()
    with pytest.raises(Unsupported) as e:
        build(db, conn)
    assert e.value.detail.get("setup_blocked") is True
    conn.status = "disconnected"
    await db.commit()


def test_live_adapter_capability_map_follows_granted_scopes_and_flags():
    conn = Connection(provider="gmail_business", status="connected",
                      granted_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                      capabilities={"send": True, "drafts": True, "labels": True})
    caps = _bind(conn).capabilities()
    assert caps["read"] is True
    # send/compose/modify scopes were never granted, so no flag can switch them on
    assert caps["send"] is False and caps["drafts"] is False and caps["labels"] is False


def _bind(conn):
    a = GmailAdapter.__new__(GmailAdapter)
    a.db = None
    a.conn = conn
    return a
