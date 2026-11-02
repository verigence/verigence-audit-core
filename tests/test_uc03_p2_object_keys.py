"""Where a PC's upload is stored (product owner 2026-11-02): per outlet, per IST
day, the file's name with the customer's name (and the PC's) appended."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from audit_core.uc03_p2_object_keys import (
    batch_root,
    day_folder,
    journey_folder_of,
    original_key,
    root_of,
    safe_filename,
    safe_part,
)

T, J, B = "tenant-1", uuid4(), uuid4()


def test_the_day_is_the_ist_day_not_the_utc_day() -> None:
    # 19:00 UTC on 25 Oct is 00:30 IST on 26 Oct
    assert day_folder(datetime(2026, 10, 25, 19, 0, tzinfo=UTC)) == "2026-10-26"
    assert day_folder(datetime(2026, 10, 25, 18, 29, tzinfo=UTC)) == "2026-10-25"


def test_original_key_is_per_outlet_then_day_and_names_the_customer() -> None:
    now = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    key = original_key(T, J, B, filename="Booking Form (1).pdf", outlet_name="Utkal Mahindra - KEONJHAR",
                       customer_name="Malaya Kumar Mahanta", pc_name="Rashmirekha M.", now=now)
    assert key == (f"p2-documents/{T}/Utkal-Mahindra-KEONJHAR/2026-11-02/{J}/{B}/original/"
                   "Booking_Form_1___Malaya-Kumar-Mahanta__Rashmirekha-M.pdf")
    assert key.startswith(f"{batch_root(T, J, B, outlet_name='Utkal Mahindra - KEONJHAR', now=now)}/original/")


def test_a_photo_keeps_its_extension_and_a_name_without_one_gets_none() -> None:
    jpeg = original_key(T, J, B, filename="WhatsApp Image 2026-10-30 at 3.53.12 PM.jpeg",
                        outlet_name="o", customer_name="Rupa Barjah", pc_name="p")
    assert jpeg.endswith("/original/WhatsApp_Image_2026-10-30_at_3.53.12_PM__Rupa-Barjah__p.jpeg")
    bare = original_key(T, J, B, filename="scan", outlet_name="o", customer_name="c", pc_name="p")
    assert bare.endswith("/original/scan__c__p")


def test_the_long_outlet_name_is_kept_whole_as_its_folder() -> None:
    key = original_key(T, J, B, filename="f.pdf",
                       outlet_name="Utkal Mahindra - MANCHESWAR FULLRANGE SR NPAHAL WS 2ND FL",
                       customer_name="c", pc_name="p")
    assert f"/{T}/Utkal-Mahindra-MANCHESWAR-FULLRANGE-SR-NPAHAL-WS-2ND-FL/" in key


def test_a_missing_name_gets_a_placeholder_and_unsafe_characters_are_removed() -> None:
    key = original_key(T, J, B, filename="a/b\\..\\c.pdf", outlet_name=None, customer_name="  ../ ",
                       pc_name=None)
    assert f"/{T}/outlet/" in key
    name = key.rsplit("/", 1)[1]
    assert name.endswith("__customer__pc.pdf") and "/" not in name and ".." not in name
    assert safe_part("", fallback="x") == "x" and safe_filename("   ") == "document"


def test_a_long_name_is_cut_not_failed() -> None:
    key = original_key(T, J, B, filename="x" * 500 + ".pdf", outlet_name="o" * 300, customer_name="c" * 300,
                       pc_name="p" * 300)
    assert len(key.rsplit("/", 1)[1]) <= 180 + 2 * 42 + 6
    assert key.endswith(".pdf")


def test_pages_and_groups_follow_the_original_in_every_layout() -> None:
    new = original_key(T, J, B, filename="f.pdf", outlet_name="o", customer_name="c", pc_name="p")
    assert root_of(new) == batch_root(T, J, B, outlet_name="o") and root_of(new).endswith(f"/{J}/{B}")
    dated = f"p2-documents/{T}/2026-W44/2026-10-26/{J}/{B}/original/o__p__f.pdf"
    assert root_of(dated) == f"p2-documents/{T}/2026-W44/2026-10-26/{J}/{B}"
    old = f"p2-documents/{T}/{J}/{B}/original/f.pdf"
    assert root_of(old) == f"p2-documents/{T}/{J}/{B}"
    assert root_of("p2/some-key-with-no-original-folder") is None


def test_journey_folder_is_found_in_every_layout_and_never_another_journeys() -> None:
    new = original_key(T, J, B, filename="f.pdf", outlet_name="o")
    assert journey_folder_of(new, J) == f"{batch_root(T, J, B, outlet_name='o').rsplit('/', 1)[0]}/"
    assert journey_folder_of(f"p2-documents/{T}/{J}/{B}/original/f.pdf", J) == f"p2-documents/{T}/{J}/"
    assert journey_folder_of(new, uuid4()) is None
