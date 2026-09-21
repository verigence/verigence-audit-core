"""A slow/unreachable DI must fail through DiCaptureV2Error, not a raw
httpx exception -- every caller (uc03_document_capture_v2.py,
uc03_delivery_capture_v2.py, uc03_unified_document_capture.py) only catches
DiCaptureV2Error to turn a DI failure into the (generic but present)
DependencyUnavailableError message a PC actually sees. A batch upload-intents
call that runs long enough to hit the client's own timeout used to raise
httpx.TimeoutException straight past every one of those handlers, surfacing
as a bare unhandled 500 with no detail at all -- the live symptom this
regression test locks in.
"""
from __future__ import annotations

import httpx
import pytest

from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error


def _timeout_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    return httpx.MockTransport(handler)


def _connect_error_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def test_a_di_timeout_raises_di_capture_v2_error_not_a_raw_httpx_exception() -> None:
    client = DiCaptureV2Client(base_url="http://di.test", transport=_timeout_transport())
    with pytest.raises(DiCaptureV2Error) as excinfo:
        client.create_upload_intents(
            token="t",
            tenant_id="tenant-1",
            external_context_ref="ctx-1",
            phase="BOOKING",
            candidate_document_type_keys=["booking_form"],
            files=[{"clientUploadId": "c1", "filename": "a.pdf", "contentType": "application/pdf"}],
        )
    assert excinfo.value.status_code == 504
    assert excinfo.value.detail


def test_a_di_connection_failure_also_raises_di_capture_v2_error() -> None:
    client = DiCaptureV2Client(base_url="http://di.test", transport=_connect_error_transport())
    with pytest.raises(DiCaptureV2Error):
        client.finalize_document(
            token="t", tenant_id="tenant-1", external_context_ref="ctx-1", document_id="doc-1",
        )


def test_a_normal_di_http_error_response_is_still_wrapped_as_before() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(422, text="Requirement-ref mapping contains a non-candidate document type.")

    client = DiCaptureV2Client(base_url="http://di.test", transport=httpx.MockTransport(handler))
    with pytest.raises(DiCaptureV2Error) as excinfo:
        client.finalize_document(
            token="t", tenant_id="tenant-1", external_context_ref="ctx-1", document_id="doc-1",
        )
    assert excinfo.value.status_code == 422
    assert "non-candidate document type" in excinfo.value.detail
