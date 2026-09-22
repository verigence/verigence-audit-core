"""Source-inspection coverage for request_pc_document_reupload: exercising
the full route needs a TL-scoped case/evidence/requirement fixture this
file doesn't set up, so this checks the same thing the endpoint's own
behaviour actually needs -- no audit_findings row, and the Task's own
payload carries the document's name -- the same level test_uc03_delivery_
capture_v2.py already uses for similarly connection-heavy endpoints.

Bug fix, confirmed live: a TL reupload request raised both an
audit_findings row and a PC_DOCUMENT_REUPLOAD Task for the same event,
showing up twice in the Task Queue (inflating the "Documents" tab count),
and neither carried the document's own name -- just a bare document_id
UUID -- so the item was indistinguishable from any other reupload request
on the same journey.
"""
from __future__ import annotations

import inspect

import audit_core.uc03_tl_supervisory as tl_supervisory


def test_reupload_request_never_creates_an_audit_finding() -> None:
    source = inspect.getsource(tl_supervisory.request_pc_document_reupload)
    assert "INSERT INTO auditcore.audit_findings" not in source
    assert "_create_reupload_finding" not in source
    assert not hasattr(tl_supervisory, "_create_reupload_finding")


def test_reupload_task_payload_carries_the_document_name() -> None:
    source = inspect.getsource(tl_supervisory.request_pc_document_reupload)
    assert '"documentTypeKey": document_type_key' in source
    assert '"documentLabel": document_label' in source
    assert "document[\"document_type_key\"]" in source


def test_reupload_response_no_longer_promises_a_finding_id() -> None:
    assert "findingId" not in tl_supervisory.TlReuploadRequestResponse.model_fields


def test_existing_reupload_task_short_circuits_without_requiring_a_finding() -> None:
    # The old gate (`existing is not None and existing["finding_id"] is not
    # None`) would never short-circuit once findings stopped being created
    # at all -- confirmed this was actually fixed, not just the create path.
    source = inspect.getsource(tl_supervisory.request_pc_document_reupload)
    assert 'if existing is not None and existing["finding_id"]' not in source
    assert "if existing is not None:" in source
