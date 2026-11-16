"""A customer's name as shown is never an id (closing list item 5, 2026-11-16)."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text


@pytest.mark.parametrize(("legal", "entered", "shown"), [
    ("SUSHANTA SAHOO", "7421c0bf-08c1-4679-b655-81bc42a38426", "SUSHANTA SAHOO"),
    (None, "7421c0bf-08c1-4679-b655-81bc42a38426", "Name not read"),
    (None, "7421c0bf08c14679b65581bc42a38426", "Name not read"),
    (None, "1015544", "Name not read"),
    (None, "Bikash Martha", "Bikash Martha"),
    ("  ", None, "Name not read"),
])
def test_shown_name(legal, entered, shown):
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    with engine.connect() as c:
        assert c.execute(text("SELECT auditcore.shown_name(:l, :e)"), {"l": legal, "e": entered}).scalar_one() == shown
    engine.dispose()
