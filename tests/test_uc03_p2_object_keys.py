"""Where a PC's upload is stored: dated folders (IST), outlet and PC in the name."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from audit_core.uc03_p2_object_keys import (
    batch_root,
    date_folders,
    journey_folder_of,
    original_key,
    root_of,
    safe_filename,
    safe_part,
)

T, J, B = "tenant-1", uuid4(), uuid4()


def test_the_day_is_the_ist_day_not_the_utc_day() -> None:
    # 19:00 UTC on 25 Oct is 00:30 IST on 26 Oct (a Monday, ISO week 44)
    assert date_folders(datetime(2026, 10, 25, 19, 0, tzinfo=UTC)) == "2026-W44/2026-10-26"
    assert date_folders(datetime(2026, 10, 25, 18, 29, tzinfo=UTC)) == "2026-W43/2026-10-25"


def test_week_folder_uses_the_iso_week_across_the_new_year() -> None:
    assert date_folders(datetime(2026, 12, 30, 6, 0, tzinfo=UTC)) == "2026-W53/2026-12-30"
    assert date_folders(datetime(2027, 1, 1, 6, 0, tzinfo=UTC)) == "2026-W53/2027-01-01"


def test_original_key_is_dated_and_carries_outlet_and_pc() -> None:
    now = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
    key = original_key(T, J, B, filename="Booking Form (1).pdf", outlet_name="Aditya Motors, Bhubaneswar",
                       pc_name="Ravi K.", now=now)
    assert key == (f"p2-documents/{T}/2026-W44/2026-10-26/{J}/{B}/original/"
                   "Aditya-Motors-Bhubaneswar__Ravi-K__Booking_Form_1_.pdf")
    assert key.startswith(f"{batch_root(T, J, B, now=now)}/original/")


def test_a_missing_name_gets_a_placeholder_and_unsafe_characters_are_removed() -> None:
    key = original_key(T, J, B, filename="a/b\\..\\c.pdf", outlet_name=None, pc_name="  ../  ")
    name = key.rsplit("/", 1)[1]
    assert name.startswith("outlet__pc__") and "/" not in name and ".." not in name
    assert safe_part("", fallback="x") == "x" and safe_filename("   ") == "document"


def test_a_long_name_is_cut_not_failed() -> None:
    key = original_key(T, J, B, filename="x" * 500 + ".pdf", outlet_name="o" * 300, pc_name="p" * 300)
    assert len(key.rsplit("/", 1)[1]) <= 180 + 2 * 42


def test_pages_and_groups_follow_the_original_in_either_layout() -> None:
    new = original_key(T, J, B, filename="f.pdf", outlet_name="o", pc_name="p")
    assert root_of(new) == batch_root(T, J, B) and root_of(new).endswith(f"/{J}/{B}")
    old = f"p2-documents/{T}/{J}/{B}/original/f.pdf"
    assert root_of(old) == f"p2-documents/{T}/{J}/{B}"
    assert root_of("p2/some-key-with-no-original-folder") is None


def test_journey_folder_is_found_in_both_layouts_and_never_another_journeys() -> None:
    new = original_key(T, J, B, filename="f.pdf")
    assert journey_folder_of(new, J) == f"{batch_root(T, J, B).rsplit('/', 1)[0]}/"
    assert journey_folder_of(f"p2-documents/{T}/{J}/{B}/original/f.pdf", J) == f"p2-documents/{T}/{J}/"
    assert journey_folder_of(new, uuid4()) is None
