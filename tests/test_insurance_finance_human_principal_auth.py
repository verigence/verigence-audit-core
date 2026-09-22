from __future__ import annotations

import inspect

from audit_core import insurance_tradein, payments_finance


def test_get_insurance_uses_the_human_principal_not_get_principal() -> None:
    # Regression for a live incident: get_insurance was on Depends(get_principal),
    # which requires a JWT carrying tenant_id/permissions claims directly. The
    # human token format actually issued today deliberately carries neither
    # (permissions are checked live against the Security service instead), so
    # every real PC/TL request to this endpoint was a guaranteed 401 -- not
    # transient, not a caching issue, confirmed live via the browser network
    # tab (consistent 401 on every load, while the Journey Overview call
    # alongside it, already on get_human_principal, succeeded with the same
    # bearer token).
    source = inspect.getsource(insurance_tradein.get_insurance)
    assert "Depends(get_human_principal)" in source
    assert "Depends(get_principal)" not in source
    assert "get_security_authorization_client" in source


def test_get_finance_uses_the_human_principal_not_get_principal() -> None:
    source = inspect.getsource(payments_finance.get_finance)
    assert "Depends(get_human_principal)" in source
    assert "Depends(get_principal)" not in source
    assert "get_security_authorization_client" in source


def test_get_insurance_still_enforces_the_same_permission_and_business_scope() -> None:
    # The fix must not weaken authorization -- same permission key, same
    # business-scope (dealer/outlet) check as before, just adapted to a
    # caller that only carries a subject, not a full Principal.
    source = inspect.getsource(insurance_tradein.get_insurance)
    assert '"audit.journey.read"' in source
    assert "_scope_human(" in source


def test_get_finance_still_enforces_the_same_permission_and_business_scope() -> None:
    source = inspect.getsource(payments_finance.get_finance)
    assert '"audit.payment.read"' in source
    assert "_authorize_scope_human(" in source


def test_other_get_principal_endpoints_in_the_same_files_are_left_untouched() -> None:
    # Deliberately narrow fix: only the two endpoints actually called by the
    # live Journey 360 page (getInsurance/getFinance) were migrated. Every
    # other route in these same files must be untouched, not swept up into
    # a wider migration with its own unverified blast radius.
    insurance_source = inspect.getsource(insurance_tradein)
    finance_source = inspect.getsource(payments_finance)
    assert insurance_source.count("Depends(get_principal)") >= 1
    assert finance_source.count("Depends(get_principal)") >= 1
