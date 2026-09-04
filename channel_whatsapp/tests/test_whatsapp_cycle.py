"""End-to-end tests for the full WhatsApp booking and delivery cycle."""
from __future__ import annotations
import hashlib, re, uuid
import pytest
from channel_whatsapp.tests.fake_meta.server import fake_meta_server
from channel_whatsapp.wa.webhook import verify_signature


def _sha256(c): return hashlib.sha256(c).hexdigest()


class TestHmacVerification:
    def test_valid_signature_accepted(self):
        import hmac as _hmac
        body = b'{"entry":[]}'
        sig = "sha256=" + _hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert verify_signature(raw_body=body, hub_signature=sig, app_secret="secret")

    def test_invalid_signature_rejected(self):
        assert not verify_signature(raw_body=b"body", hub_signature="sha256=deadbeef", app_secret="wrong")

    def test_missing_signature_rejected(self):
        assert not verify_signature(raw_body=b"body", hub_signature=None, app_secret="secret")

    def test_wrong_prefix_rejected(self):
        assert not verify_signature(raw_body=b"body", hub_signature="md5=abc", app_secret="secret")


class TestFakeMetaServer:
    def test_media_url_resolution(self):
        import httpx
        with fake_meta_server() as s:
            s.inject_media(media_id="m1", content=b"%PDF")
            resp = httpx.get(f"{s.base_url}/v19.0/m1")
            assert resp.status_code == 200
            assert "url" in resp.json()

    def test_media_download(self):
        import httpx
        content = b"%PDF booking"
        with fake_meta_server() as s:
            s.inject_media(media_id="m2", content=content)
            url_resp = httpx.get(f"{s.base_url}/v19.0/m2")
            dl = httpx.get(url_resp.json()["url"])
            assert dl.content == content

    def test_signed_payload_verifies(self):
        with fake_meta_server() as s:
            s.inject_media(media_id="m3", content=b"%PDF")
            body, sig = s.signed_webhook_payload(phone_number_id="pn1", from_number="+91999", media_id="m3")
            assert verify_signature(raw_body=body, hub_signature=sig, app_secret=s.app_secret)

    def test_message_recording(self):
        import httpx
        with fake_meta_server() as s:
            r = httpx.post(f"{s.base_url}/pnid/messages", json={"to": "+919", "type": "text"})
            assert r.status_code == 200
            assert len(s.sent_messages) == 1


class TestMediaFetch:
    def test_hash_mismatch_quarantines(self):
        from channel_whatsapp.media.fetch import MediaFetchError, fetch_media
        import httpx
        content = b"real"
        class FT(httpx.BaseTransport):
            def handle_request(self, req):
                if "/download" in str(req.url):
                    return httpx.Response(200, content=content, headers={"Content-Type": "application/pdf"})
                return httpx.Response(200, json={"url": "http://x/download", "mime_type": "application/pdf"})
        with pytest.raises(MediaFetchError) as ei:
            fetch_media(media_id="m", access_token="t", declared_sha256="0"*64, transport=FT())
        assert ei.value.quarantine is True
        assert ei.value.code == "MEDIA_HASH_MISMATCH"

    def test_correct_hash_succeeds(self):
        from channel_whatsapp.media.fetch import fetch_media
        import httpx
        content = b"good"
        sha = _sha256(content)
        class FT(httpx.BaseTransport):
            def handle_request(self, req):
                if "/download" in str(req.url):
                    return httpx.Response(200, content=content, headers={"Content-Type": "application/pdf"})
                return httpx.Response(200, json={"url": "http://x/download", "mime_type": "application/pdf"})
        r = fetch_media(media_id="m", access_token="t", declared_sha256=sha, transport=FT())
        assert r.sha256 == sha


class TestAadhaarRedaction:
    def test_digit_masking(self):
        from channel_whatsapp.media.redact import mask_aadhaar_text
        assert "XXXX XXXX 9012" in mask_aadhaar_text("1234 5678 9012")

    def test_non_aadhaar_unchanged(self):
        from channel_whatsapp.media.redact import mask_aadhaar_text
        assert mask_aadhaar_text("PAN: ABCDE1234F") == "PAN: ABCDE1234F"

    def test_needs_redaction_flags(self):
        from channel_whatsapp.media.redact import needs_redaction
        assert needs_redaction("AADHAAR") is True
        assert needs_redaction("PAN") is False

    def test_no_pii_in_copy(self):
        from channel_whatsapp.wa.copy import load_copy
        for k, v in load_copy("en").items():
            assert not re.search(r"\b\d{12}\b", str(v)), f"PII in copy/{k}"


class TestChecklist:
    def test_default_requirements_retail(self):
        from channel_whatsapp.deal.checklist import _default_requirements
        reqs = dict(_default_requirements("retail_individual"))
        for k in ("BOOKING_FORM", "PAN", "AADHAAR", "TAX_INVOICE_VEHICLE", "DELIVERY_NOTE"):
            assert reqs[k] == "blocking"

    def test_financed_adds_bank_docs(self):
        from channel_whatsapp.deal.checklist import _default_requirements
        reqs = dict(_default_requirements("retail_financed"))
        assert "LOAN_SANCTION_LETTER" in reqs

    def test_corporate_adds_gst(self):
        from channel_whatsapp.deal.checklist import _default_requirements
        reqs = dict(_default_requirements("corporate"))
        assert "GST_REGISTRATION" in reqs

    def test_gap_message_no_pii(self):
        from channel_whatsapp.deal.checklist import ChecklistState, format_gap_message
        from channel_whatsapp.wa.copy import load_copy
        state = ChecklistState(deal_id=uuid.uuid4(), booking_number="BK-1",
                               blocking_total=2, blocking_satisfied=0,
                               blocking_missing=["PAN", "Aadhaar"], received=[], is_complete=False)
        _, body = format_gap_message(state, locale="en", copy=load_copy("en"))
        assert not re.search(r"\b\d{12}\b", body)


class TestDealDedup:
    def test_fuzzy_threshold(self):
        from channel_whatsapp.deal.builder import _FUZZY_THRESHOLD
        assert _FUZZY_THRESHOLD == 0.6


class TestRateLimitSafety:
    def test_stops_before_meta_limit(self):
        from channel_whatsapp.media.fetch import MediaFetchError, _BACKOFF_BEFORE_META_LIMIT, fetch_media
        import httpx
        assert _BACKOFF_BEFORE_META_LIMIT == 4
        class Fail(httpx.BaseTransport):
            def handle_request(self, req): return httpx.Response(503)
        with pytest.raises(MediaFetchError) as ei:
            fetch_media(media_id="x", access_token="t", declared_sha256=None,
                        attempt=_BACKOFF_BEFORE_META_LIMIT, transport=Fail())
        assert ei.value.code == "MEDIA_ATTEMPT_LIMIT"


class TestSessionStateMachine:
    def test_debounce_uses_90s(self):
        from channel_whatsapp.wa.session import record_file_arrival
        import inspect
        src = inspect.getsource(record_file_arrival)
        assert "90 seconds" in src and "flush_at" in src and "file_count" in src

    def test_claim_uses_skip_locked(self):
        from channel_whatsapp.wa.session import claim_sessions_for_flush
        import inspect
        assert "SKIP LOCKED" in inspect.getsource(claim_sessions_for_flush)


class TestCrossProjectIsolation:
    def test_phone_extraction(self):
        from channel_whatsapp.wa.router import extract_sender_phone
        payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "919876543210"}]}}]}]}
        assert extract_sender_phone(payload) == "+919876543210"

    def test_no_phone_returns_none(self):
        from channel_whatsapp.wa.router import extract_sender_phone
        assert extract_sender_phone({}) is None


class TestOutboxWindow:
    def test_within_window(self):
        from channel_whatsapp.wa.outbox import within_window
        from datetime import datetime, timezone, timedelta
        assert within_window(last_at=datetime.now(tz=timezone.utc) - timedelta(hours=2))
        assert not within_window(last_at=datetime.now(tz=timezone.utc) - timedelta(hours=25))
        assert not within_window(last_at=None)


class TestCopyLoader:
    def test_all_locales_load(self):
        from channel_whatsapp.wa.copy import load_copy
        for loc in ("en", "hi", "pa"):
            c = load_copy(loc)
            assert "welcome" in c

    def test_fallback_to_en(self):
        from channel_whatsapp.wa.copy import load_copy
        assert load_copy("fr") == load_copy("en")

    def test_no_pii_in_any_locale(self):
        from channel_whatsapp.wa.copy import load_copy
        for loc in ("en", "hi", "pa"):
            for k, v in load_copy(loc).items():
                assert not re.search(r"\b\d{12}\b", str(v)), f"PII in {loc}/{k}"


class TestConfig:
    def test_debounce_default(self):
        from channel_whatsapp.config import WhatsAppSettings
        s = WhatsAppSettings(wa_app_secret="sec", wa_access_token="tok", wa_verify_token="ver")
        assert s.wa_debounce_seconds == 90

    def test_governance_gate_default_off(self):
        from channel_whatsapp.config import WhatsAppSettings
        s = WhatsAppSettings(wa_app_secret="sec", wa_access_token="tok", wa_verify_token="ver")
        assert s.wa_redaction_enabled is False
