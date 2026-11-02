"""PAGE_IMAGES (2026-11-02): a stored page returned as a small JPEG on the job
row, read only."""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from audit_core import uc03_page_images as pages
from audit_core.uc03_p2_worker import _one_shot_target


def _pdf(pages_count: int = 2) -> bytes:
    images = [Image.new("RGB", (595, 842), color=(255, 255, 255 - 60 * i)) for i in range(pages_count)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def test_a_request_names_the_tenant_and_up_to_three_pages():
    tenant, wanted = pages.parse_request("t-1:look", ["utk-sa-261027-002:17", " ADI-RD-261102-008:23 "])
    assert tenant == "t-1" and wanted == [("UTK-SA-261027-002", 17), ("ADI-RD-261102-008", 23)]
    for tag, reports in (("no-label", ["A-1:1"]), ("t:x", []), ("t:x", ["A-1:1"] * 4), ("t:x", ["A-1"]),
                         ("t:x", ["A-1:0"]), ("t:x", ["A-1:1; drop"])):
        with pytest.raises(ValueError):
            pages.parse_request(tag, reports)


def test_the_page_comes_back_as_a_jpeg_within_its_share():
    encoded, width, height = pages.render_jpeg(_pdf(), budget_chars=60_000, page_index=1)
    assert len(encoded) <= 60_000
    image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert image.format == "JPEG" and image.mode == "L" and image.size == (width, height)
    assert max(width, height) <= 1600


def test_the_worker_knows_the_job():
    target, args = _one_shot_target("PAGE_IMAGES", None, "t:x", ["A-1:1"])
    assert target is pages.run_page_images and args == (None, "t:x", ["A-1:1"])


def test_pages_are_looked_up_and_rendered_without_writing(monkeypatch):
    seen = []

    class Storage:
        def get_object(self, key):
            seen.append(key)
            return _pdf(3)

    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("set_tenant_context and the lookup are replaced in this test")

    class Begin:
        def __enter__(self):
            return Connection()

        def __exit__(self, *exc):
            return False

    class Engine:
        def begin(self):
            return Begin()

    monkeypatch.setattr("audit_core.uc03_p2_storage.get_p2_document_storage", lambda: Storage())
    monkeypatch.setattr(pages, "set_tenant_context", lambda connection, tenant_id: None)
    found = {("A-1", 2): ("p2-documents/t/x/groups/0001.pdf", 1), ("B-2", 5): None}
    monkeypatch.setattr(pages, "page_object_key",
                        lambda connection, *, tenant_id, reference, page: found[(reference, page)])
    out = pages.run_page_images(Engine(), "t:look", ["A-1:2", "B-2:5"])
    assert seen == ["p2-documents/t/x/groups/0001.pdf"]
    assert [p["journey"] for p in out["pages"]] == ["A-1"] and out["pages"][0]["jpegBase64"]
    assert out["missing"] == [{"journey": "B-2", "page": 5, "why": "no stored page"}]
