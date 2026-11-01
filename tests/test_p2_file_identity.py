"""Whose each uploaded file is (found live 2026-10-31, ADI-RD-261022-022).

A PC dropped the next customer's PDF ("Tapan kumar patra.pdf") into the
booking the first one created ("Azizur Reheman.pdf"): Tapan's PAN, Aadhaar and
docket superseded Azizur's and his receipt counted as Azizur's payment. The
recompute now decides each file's identity, takes a foreign file's documents
out of the booking (P2_FILE_IDENTITY_ENFORCE) and gives the customer's own
copies back -- by itself, no PC or Team Lead action."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_extracted_field, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_file_identity import (
    DocumentFacts,
    FileFacts,
    classify,
    file_stem,
    files_view,
    names_match,
    normalise_aadhaar,
    same_aadhaar,
    same_pan,
)

T0 = datetime(2026, 10, 22, 11, 47, tzinfo=UTC)


def _file(batch_id: str, name: str, minutes: float, *docs: tuple[str, dict], pages: int = 1,
          busy: bool = False) -> FileFacts:
    return FileFacts(batch_id, name, T0 + timedelta(minutes=minutes), pages, busy, [
        DocumentFacts(f"{batch_id}-{i}", doc_type, {k: (v if isinstance(v, list) and k.endswith("_names") else [v])
                                                     for k, v in fields.items()})
        for i, (doc_type, fields) in enumerate(docs)])


AZIZUR = _file(
    "azizur", "Azizur Reheman.pdf", 0,
    ("pan_card", {"pan_number": "AHOPR2948B", "pan_name": "AZIZUR REHAMAN"}),
    ("aadhaar", {"aadhaar_number": "7760 9413 4320", "aadhaar_name": "AZIZUR REHAMAN"}),
    # someone else's Aadhaar inside the customer's own file: accepted with them
    ("aadhaar", {"aadhaar_number": "8384 9148 1355", "aadhaar_name": "Samim Sahadat"}),
    ("booking_docket", {"customer_name": "AZIZUR REHAMAN"}),
    ("dealer_receipt", {"customer_name": "AZIZUR REHAMAN-AHOPR2948B"}),
    ("dealer_receipt", {"customer_name": "STATE BANK OF INDIA (FINANCER)"}),
    pages=13,
)
TAPAN = _file(
    "tapan", "Tapan kumar patra.pdf", 2,
    ("pan_card", {"pan_number": "AVFPP9596G", "pan_name": "TAPAN KUMAR PATRA"}),
    ("aadhaar", {"aadhaar_number": "5845 3895 7247", "aadhaar_name": "TAPAN KUMAR PATRA"}),
    ("booking_docket", {"customer_name": "TAPAN KUMAR PATRA"}),
    ("payment_receipt", {"customer_name": "TAPAN KUMAR PATRA-AVFPP9596G"}),
    pages=5,
)


# ------------------------------------------------------------ pure rules


@pytest.mark.parametrize(("name", "stem"), [
    ("Jyotsna nayak delivery inscope-26(47092834102359).jpg", "jyotsna nayak"),
    ("Jyotsna nayak delivery inscope-1(47091299956874).jpg", "jyotsna nayak"),
    ("jagannath suppliers (06-10).pdf", "jagannath suppliers"),
    ("Prasana kumar senapati  (1).pdf", "prasana kumar senapati"),
    ("suryakanta parida pan card.jpeg", "suryakanta parida"),
    ("IMG_20261019_120541.jpg", None),
    ("DOC-20261025-WA0015.pdf", None),
    ("WhatsApp Image 2026-10-26 at 12.38.02 PM.jpeg", None),
    ("DocScanner Oct 19, 2026 12-15 PM.pdf", None),
])
def test_files_whose_names_share_a_stem_are_one_unit(name, stem):
    assert file_stem(name) == stem


def test_misread_numbers_and_names_are_one_person():
    assert same_pan("BQPRP2303F", "BQRPR2303F")  # coordinator's example: last five the same
    assert same_pan("GNIPM0756G", "GNPM0756G")  # a letter dropped
    assert not same_pan("AHOPR2948B", "AVFPP9596G")
    assert same_aadhaar(normalise_aadhaar("XXXX XXXX 7247"), normalise_aadhaar("5845 3895 7247"))
    assert same_aadhaar(normalise_aadhaar("5845 3895 7247"), normalise_aadhaar("5845 3896 7247"))
    assert not same_aadhaar(normalise_aadhaar("5845 3895 7247"), normalise_aadhaar("7760 9413 4320"))
    assert names_match("Chittaranjan Mahia", "CHITTA RANJAN MATIA")
    assert names_match("JYOTSNA MAYAK-APMPN2818Q", "Jyotsna Nayak")
    assert not names_match("TAPAN KUMAR PATRA", "TARUN KUMAR SAHOO")
    assert not names_match("MIHIR KUMAR DEHURY", "Mr. Anil Kumar Dehury")


def test_the_second_customers_file_is_foreign_and_the_first_is_the_customers():
    decision = classify([AZIZUR, TAPAN])
    assert decision.customer_name == "AZIZUR REHAMAN"
    assert decision.files["azizur"].status == "BELONGS"
    tapan = decision.files["tapan"]
    assert (tapan.status, tapan.reason_code, tapan.person_name, tapan.masked_pan) == (
        "FOREIGN", "OTHER_CUSTOMER", "TAPAN KUMAR PATRA", "AVFPP****G")
    assert "AZIZUR REHAMAN" in tapan.reason


def test_a_reupload_of_the_same_customer_belongs():
    again = _file("again", "IMG_20261022_120000.jpg", 30,
                  ("pan_card", {"pan_number": "AHOPR2948B", "pan_name": "AZIZUR REHAMAN"}))
    same_name = _file("named", "receipt.pdf", 31, ("dealer_receipt", {"customer_name": "Azizur Rehaman - R270812345"}))
    decision = classify([AZIZUR, again, same_name])
    assert {b: d.status for b, d in decision.files.items()} == {"azizur": "BELONGS", "again": "BELONGS",
                                                                 "named": "BELONGS"}


def test_a_file_with_no_identity_belongs():
    gate_pass = _file("gate", "BISHNU CHARAN GATE PASS.pdf", 5, ("gate_pass", {"vehicle_model": "XUV"}))
    canteen = _file("csd", "csd receipt.pdf", 6,
                    ("dealer_receipt", {"customer_name": "THE AREA MANAGER CANTEEN STORES DEPARTMENT"}))
    decision = classify([AZIZUR, gate_pass, canteen])
    assert decision.files["gate"].status == decision.files["csd"].status == "BELONGS"
    assert decision.files["gate"].reason_code == decision.files["csd"].reason_code == "NO_IDENTITY"


def test_a_co_applicants_aadhaar_inside_the_first_file_is_accepted_later_too():
    """Samim's Aadhaar was in Azizur's own file; a later file with Samim's
    Aadhaar alone is Azizur's booking's."""
    later = _file("samim", "Samim.pdf", 10, ("aadhaar", {"aadhaar_number": "8384 9148 1355",
                                                          "aadhaar_name": "Samim Sahadat"}))
    decision = classify([AZIZUR, later])
    assert (decision.files["samim"].status, decision.files["samim"].reason_code) == ("BELONGS", "SAME_AADHAAR")


def test_a_co_applicant_on_the_bank_approval_letter_is_a_second_party():
    with_letter = _file(
        "azizur", "Azizur Reheman.pdf", 0, *[(d.doc_type, {k: v[0] for k, v in d.fields.items()})
                                               for d in AZIZUR.documents],
        ("bank_approval_letter", {"finance_applicant_name": "Mr. AZIZUR REHAMAN",
                                  "finance_co_applicant_name": "TAPAN KUMAR PATRA"}), pages=14)
    decision = classify([with_letter, TAPAN])
    tapan = decision.files["tapan"]
    assert (tapan.status, tapan.reason_code) == ("BELONGS", "SECOND_PARTY")
    assert "co-applicant" in tapan.reason


def test_a_page_by_page_upload_is_about_the_person_most_pages_are_about():
    """UTK-BI-261020-007: every page its own file, the first page read was
    Madhulina Digal's receipt but the booking is Jyotsna Nayak's."""
    pages = [_file("p26", "Jyotsna nayak delivery inscope-26(1).jpg", 0,
                   ("dealer_receipt", {"customer_name": "MADHULINA DIGAL-FBCPD9965H"}))]
    pages += [_file(f"p{i}", f"Jyotsna nayak delivery inscope-{i}(2).jpg", i,
                    ("dealer_receipt", {"customer_name": "JYOTSNA MAYAK-APMPN2818Q"})) for i in range(1, 6)]
    pages.append(_file("p29", "Jyotsna nayak delivery inscope-29(3).jpg", 7,
                       ("pan_card", {"pan_number": "APMPN2818Q", "pan_name": "JYOTSNA NAYAK"})))
    decision = classify(pages)
    assert decision.customer_name == "JYOTSNA NAYAK"
    assert all(d.status == "BELONGS" for d in decision.files.values())


def test_the_customer_is_whom_most_units_and_pages_are_about():
    """UTK-SA-261020-004: "debabrata dani.pdf" came first, but two files of
    Nihar Ranjan Mohapatra's firm (one unit, more pages) are the booking."""
    debabrata = _file("db", "debabrata dani.pdf", 0, ("pan_card", {"pan_number": "AMAPD1990L",
                                                                    "pan_name": "DEBABRATA DANI"}),
                      ("booking_docket", {"customer_name": "DEBABRATA DANI"}), pages=4)
    firm1 = _file("j1", "jagannath suppliers (06-10).pdf", 1,
                  ("booking_docket", {"customer_name": "JAGANNATH SUPPLIER"}),
                  ("gst_certificate", {"gstin": "21ALUPM2756L1ZM", "organisation_legal_name": "NIHAR RANJAN MOHAPATRA",
                                       "organisation_trade_name": "JAGANNATH SUPPLIERS"}), pages=4)
    firm2 = _file("j2", "jagannath suppliers (04-10).pdf", 2,
                  ("pan_card", {"pan_number": "ALUPM2756L", "pan_name": "NIHAR RANJAN MOHAPATRA"}),
                  ("payment_receipt", {"customer_name": "JAGANNATH SUPPLIERS-R270893389"}), pages=5)
    decision = classify([debabrata, firm1, firm2])
    assert decision.customer_name == "NIHAR RANJAN MOHAPATRA"
    assert decision.files["j1"].status == decision.files["j2"].status == "BELONGS"
    assert decision.files["db"].status == "FOREIGN"


def test_the_proprietors_pan_in_the_gstin_belongs():
    """A proprietor's firm file names the firm; their PAN card in a later file
    is the PAN inside the GSTIN on file."""
    firm = _file("firm", "Rida Infrastructure.pdf", 0,
                 ("gst_certificate", {"gstin": "21AHOPR2948B2ZE", "organisation_legal_name": "AZIZUR REHAMAN",
                                      "organisation_trade_name": "RIDA INFRASTRUCTURE"}),
                 ("booking_docket", {"customer_name": "M/S RIDA INFRASTRUCTURE"}), pages=6)
    pan = _file("pan", "IMG_20261022_1.jpg", 5, ("pan_card", {"pan_number": "AHOPR2948B", "pan_name": "MD AZIJUR RAHMAN"}))
    decision = classify([firm, pan])
    # The PAN inside the GSTIN is the customer's own: the file is the customer's.
    assert decision.files["pan"].status == "BELONGS"
    assert decision.files["pan"].reason_code in ("CUSTOMER_FILE", "SAME_PAN")
    assert decision.customer_name == "AZIZUR REHAMAN"


def test_a_companys_director_or_signatory():
    """ADI-RD-261022-021: a company's booking file and a person's file. The
    person the GST certificate names as signatory belongs; one it does not
    name is kept to be checked (a director's KYC or another booking)."""
    company = _file("co", "Jagannath Power And Infra Private Limited Booking .pdf", 0,
                    ("pan_card", {"pan_number": "AAFCJ2048H", "pan_name": "JAGANNATH POWER AND INFRA PRIVATE LIMITED"}),
                    ("gst_certificate", {"gstin": "21AAFCJ2048H1ZY",
                                         "organisation_legal_name": "JAGANNATH POWER AND INFRA PRIVATE LIMITED",
                                         "gst_authorised_signatory_names": ["PRAVAKAR SAHOO", "SATYABAN SAHOO"]}),
                    ("booking_docket", {"customer_name": "M/S Jagannath Power And Infra Private Limited"}), pages=8)
    signatory = _file("sig", "Pravakar Sahoo.pdf", 2, ("pan_card", {"pan_number": "BXYPS1234K",
                                                                       "pan_name": "PRAVAKAR SAHOO"}))
    stranger = _file("ps", "Prasana kumar senapati  (1).pdf", 3,
                     ("pan_card", {"pan_number": "AONPS0726E", "pan_name": "PRASANA KUMAR SENAPATI"}),
                     ("booking_docket", {"customer_name": "Prasana Kumar Senapati"}))
    decision = classify([company, signatory, stranger])
    assert decision.customer_kind == "ORG"
    assert (decision.files["sig"].status, decision.files["sig"].reason_code) == ("BELONGS", "SECOND_PARTY")
    assert "signatory" in decision.files["sig"].reason
    assert (decision.files["ps"].status, decision.files["ps"].reason_code) == ("UNKNOWN", "COMPANY_PERSON_UNPROVEN")


def test_a_company_file_naming_its_own_car_is_another_purchase():
    """UTK-SU-261019-001: "Progressive Odisha Pvt Ltd.pdf" invoices car
    ...J19579 (it is UTK-SU-261019-002's file too); the booking's own invoice
    is Rabindra Barik's ...J35566."""
    rabindra = _file("rb", "Rabindra Barik.pdf", 0,
                     ("aadhaar", {"aadhaar_number": "8690 1983 8273", "aadhaar_name": "RABINDRA BARIK"}),
                     ("booking_docket", {"customer_name": "RABINDRA BARIK"}),
                     ("customer_invoice_dms", {"buyer_name": "RABINDRA BARIK", "chassis_number": "MA1RA2TTKT6J35566"}),
                     pages=24)
    company = _file("po", "Progressive Odisha Pvt Ltd.pdf", 0,
                    ("pan_card", {"pan_number": "AAPCP8628R", "pan_name": "PROGRESSIVE ODISHA PRIVATE LIMITED"}),
                    ("customer_invoice_dms", {"buyer_name": "PROGRESSIVE ODISHA PRIVATE LIMITED",
                                              "buyer_gstin": "21AAPCP8628R1ZJ", "chassis_number": "MA1RB2TUKT6J19579"}),
                    pages=20)
    decision = classify([rabindra, company])
    assert decision.customer_name == "RABINDRA BARIK"
    assert (decision.files["po"].status, decision.files["po"].reason_code) == ("FOREIGN", "OTHER_VEHICLE")


def test_several_customers_dropped_into_a_companys_booking_are_foreign():
    """ADI-RD-261024-004: a company's booking file beside people's own booking files."""
    company = _file("co", "Mahaveer Construction Booking .pdf", 0,
                    ("pan_card", {"pan_number": "ABVFM1234D", "pan_name": "MAHAVEER CONSTRUCTION"}), pages=6)
    people = [_file(f"p{i}", f"{name} Booking .pdf", i,
                    ("pan_card", {"pan_number": pan, "pan_name": name}), ("booking_docket", {"customer_name": name}))
              for i, (name, pan) in enumerate([("Santosh Kumar Sethi", "QGDPS1234Q"), ("Urmila Swain", "UBPPS1234P")],
                                              start=1)]
    decision = classify([company, *people])
    assert [decision.files[f"p{i}"].status for i in (1, 2)] == ["FOREIGN", "FOREIGN"]


def test_a_receipt_alone_in_another_name_is_kept_to_be_checked():
    """The payer on a receipt is often a relative (product owner 2026-10-28):
    without a PAN, Aadhaar, KYC or docket in another name the file stays."""
    receipt = _file("rc", "WhatsApp Image 2026-10-31 at 11.44.28 AM.jpeg", 9,
                    ("dealer_receipt", {"customer_name": "NIHARIKA JENA-R270812345"}))
    decision = classify([AZIZUR, receipt])
    assert (decision.files["rc"].status, decision.files["rc"].reason_code) == ("UNKNOWN", "NAME_ONLY")


def test_a_file_still_being_read_is_not_taken_out_yet():
    busy = _file("tapan", "Tapan kumar patra.pdf", 2, *[(d.doc_type, {k: v[0] for k, v in d.fields.items()})
                                                        for d in TAPAN.documents], busy=True)
    decision = classify([AZIZUR, busy])
    assert (decision.files["tapan"].status, decision.files["tapan"].reason_code) == ("UNKNOWN", "STILL_PROCESSING")


def test_two_individual_pans_in_one_kept_file_and_two_cars_are_reported_only():
    mixed = _file("mx", "Suprava Maharaj Booking .pdf", 0,
                  ("pan_card", {"pan_number": "AHOPR2948B", "pan_name": "AZIZUR REHAMAN"}),
                  ("pan_card", {"pan_number": "AVFPP9596G", "pan_name": "TAPAN KUMAR PATRA"}),
                  ("customer_invoice_dms", {"buyer_name": "AZIZUR REHAMAN", "chassis_number": "MA1RA2TTKT6J35566"}),
                  ("insurance_cover", {"insured_name": "AZIZUR REHAMAN", "chassis_number": "MA1RB2TUKT6J19579"}),
                  pages=9)
    decision = classify([mixed])
    assert decision.files["mx"].status == "BELONGS"
    assert {c["check"] for c in decision.checks} == {"TWO_PANS_IN_FILE", "MULTIPLE_VEHICLES"}


# --------------------------------------------------- the booking, end to end


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2fid")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _requirements(journey) -> dict[str, UUID]:
    out = {}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for key in ("pan_card", "aadhaar", "booking_docket", "payment_receipt", "bank_approval_letter"):
            out[key] = UUID(str(connection.execute(
                text("INSERT INTO auditcore.journey_document_requirements (tenant_id, journey_id, requirement_key, "
                     "document_type_key, process_area, requirement_level) VALUES (:t, :j, :k, :k, 'BOOKING', "
                     "'REQUIRED') RETURNING journey_document_requirement_id"),
                {"t": journey.tenant_id, "j": journey.journey_id, "k": key},
            ).scalar_one()))
    return out


_SOURCE_TYPE = {"booking_docket": "booking_form", "payment_receipt": "dealer_receipt"}


def _upload(journey, requirements, name: str, minutes: float, docs: list[tuple[str, str, dict]]) -> dict[str, dict]:
    """One uploaded file whose pages became documents: (requirement, evidence
    type, fields). Each single-slot document supersedes the copy then active,
    as uc03_pc_booking_documents links it (decision 2026-10-01)."""
    batch_id = uuid4()
    created = datetime.now(UTC) - timedelta(minutes=60 - minutes)
    out: dict[str, dict] = {}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                 "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                 "created_at_utc) VALUES (:t, :b, :j, :n, 'application/pdf', 100, :p, :k, 'COMPLETED', :a, :c)"),
            {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id, "n": name, "p": len(docs),
             "k": f"p2/{batch_id}", "a": journey.actor_id, "c": created},
        )
        for page, (requirement, doc_type, _fields) in enumerate(docs, start=1):
            document_id = uuid4()
            connection.execute(
                text("INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, "
                     "page_sha256, page_object_key, client_upload_id, di_document_id, queue_status, "
                     "classified_document_type, business_stage) VALUES (:t, :b, :j, :p, :sha, :k, :c, :d, 'READY', "
                     ":dt, 'BOOKING')"),
                {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id, "p": page, "sha": uuid4().hex,
                 "k": f"p2/{batch_id}/{page}", "c": f"p2-{uuid4().hex}", "d": document_id, "dt": doc_type},
            )
            prior = None
            if requirement != "payment_receipt":
                prior = connection.execute(
                    text("UPDATE auditcore.evidence SET association_status='SUPERSEDED', "
                         "void_reason='REPLACED_BY_NEWER_UPLOAD', voided_at_utc=now() "
                         "WHERE tenant_id=:t AND journey_id=:j AND journey_document_requirement_id=:r "
                         "AND association_status='ACTIVE' RETURNING evidence_id"),
                    {"t": journey.tenant_id, "j": journey.journey_id, "r": requirements[requirement]},
                ).scalar_one_or_none()
            evidence_id = connection.execute(
                text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, journey_document_requirement_id, "
                     "di_subject_id, di_document_id, document_type_key, evidence_purpose, process_area, "
                     "association_status, supersedes_evidence_id, linked_at_utc) VALUES (:t, :j, :c, :r, :s, :d, :dt, "
                     "'BOOKING_DOCUMENT', 'BOOKING', 'ACTIVE', :prior, :at) RETURNING evidence_id"),
                {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id,
                 "r": requirements[requirement], "s": uuid4(), "d": document_id, "dt": doc_type, "prior": prior,
                 "at": created + timedelta(seconds=page)},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO auditcore.document_capture_v2_documents (tenant_id, journey_id, stage_code, "
                     "di_document_id, client_upload_id, capture_status, created_by_actor_id) "
                     "VALUES (:t, :j, 'BOOKING', :d, :c, 'CLASSIFIED', :a)"),
                {"t": journey.tenant_id, "j": journey.journey_id, "d": document_id, "c": f"cap-{uuid4().hex}",
                 "a": journey.actor_id},
            )
            out[f"{requirement}:{page}"] = {"document_id": document_id, "evidence_id": UUID(str(evidence_id)),
                                            "batch_id": batch_id}
    for (_requirement, doc_type, fields), document in zip(docs, out.values(), strict=True):
        for key, value in fields.items():
            add_extracted_field(journey, di_document_id=document["document_id"], field_key=key, value=value,
                                document_type=_SOURCE_TYPE.get(doc_type, doc_type))
    return out


def _receipt_payment(journey, document_id: UUID, amount: str, number: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        booking_id = connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id) VALUES (:t, :j) ON CONFLICT (tenant_id, "
                 "journey_id) DO UPDATE SET updated_at_utc=now() RETURNING booking_id"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.payments (tenant_id, journey_id, amount, booking_id, payment_stage, "
                 "status_source, source_di_document_id, receipt_number, receipt_date) "
                 "VALUES (:t, :j, :a, :b, 'BOOKING', 'EVIDENCE', :d, :n, '2026-10-22')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "a": amount, "b": booking_id, "d": document_id,
             "n": number},
        )


def _azizur_then_tapan(journey):
    requirements = _requirements(journey)
    azizur = _upload(journey, requirements, "Azizur Reheman.pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "AHOPR2948B", "pan_name": "AZIZUR REHAMAN"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "7760 9413 4320", "aadhaar_name": "AZIZUR REHAMAN"}),
        ("booking_docket", "booking_docket", {"customer_name": "AZIZUR REHAMAN", "dealer_name": "Aditya Motors"}),
        ("payment_receipt", "dealer_receipt", {"customer_name": "AZIZUR REHAMAN-AHOPR2948B", "amount_paid": "21000",
                                               "receipt_number": "AMC-B/30001", "receipt_date": "2026-10-22"}),
    ])
    tapan = _upload(journey, requirements, "Tapan kumar patra.pdf", 2, [
        ("pan_card", "pan_card", {"pan_number": "AVFPP9596G", "pan_name": "TAPAN KUMAR PATRA"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "5845 3895 7247", "aadhaar_name": "TAPAN KUMAR PATRA"}),
        ("booking_docket", "booking_docket", {"customer_name": "TAPAN KUMAR PATRA", "dealer_name": "Aditya Motors"}),
        ("payment_receipt", "payment_receipt", {"customer_name": "TAPAN KUMAR PATRA-AVFPP9596G", "amount_paid": "51000",
                                                "receipt_number": "AMC-B/30580", "receipt_date": "2026-10-22"}),
    ])
    _receipt_payment(journey, azizur["payment_receipt:4"]["document_id"], "21000", "AMC-B/30001")
    _receipt_payment(journey, tapan["payment_receipt:4"]["document_id"], "51000", "AMC-B/30580")
    return requirements, azizur, tapan


def _settle(journey):
    from audit_core import uc03_p2_worker as worker

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return worker.settle_journey(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _state(journey, *documents: dict) -> list[str]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [connection.execute(text("SELECT association_status FROM auditcore.evidence WHERE tenant_id=:t "
                                        "AND evidence_id=:e"), {"t": journey.tenant_id, "e": d["evidence_id"]}
                                   ).scalar_one() for d in documents]


def _legal_name(journey) -> str | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT c.legal_name FROM auditcore.journeys j JOIN auditcore.customers c ON c.tenant_id=j.tenant_id "
                 "AND c.customer_id=j.customer_id WHERE j.tenant_id=:t AND j.journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()


def _payments(journey) -> dict[str, bool]:
    from audit_core.uc03_p2_journey360 import payments

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return {i["receiptNumber"]: i["counted"] for i in payments(connection, tenant_id=journey.tenant_id,
                                                                    journey_id=journey.journey_id)["items"]}


def test_another_customers_file_leaves_the_booking_and_the_customers_own_documents_come_back(journey, monkeypatch):
    """ADI-RD-261022-022 end to end: Tapan's PAN, Aadhaar and docket superseded
    Azizur's and his receipt counted. After the recompute (enforcement on)
    Tapan's documents are FOREIGN, Azizur's are active again, Tapan's receipt
    no longer counts and the customer is Azizur."""
    _, azizur, tapan = _azizur_then_tapan(journey)
    single = [azizur["pan_card:1"], azizur["aadhaar:2"], azizur["booking_docket:3"]]
    assert _state(journey, *single) == ["SUPERSEDED"] * 3
    # As it stood live: the customer named from the newest PAN (Tapan's), his receipt counted.
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _settle(journey)
    assert _legal_name(journey) == "TAPAN KUMAR PATRA"
    assert _payments(journey) == {"AMC-B/30001": True, "AMC-B/30580": True}
    # Dry run: decided and recorded, nothing changed.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = files_view(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    # Not taken out yet: the web is not told it was (it says "taken out" for FOREIGN).
    assert {f["fileName"]: f["identity"]["status"] for f in view["files"]} == {
        "Azizur Reheman.pdf": "BELONGS", "Tapan kumar patra.pdf": "UNKNOWN"}
    pending = next(f for f in view["files"] if f["fileName"] == "Tapan kumar patra.pdf")
    assert pending["identity"]["reason"] == "Looks like TAPAN KUMAR PATRA's file; not yet taken out"
    assert _state(journey, *single) == ["SUPERSEDED"] * 3

    # A task raised from Tapan's PAN only.
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        task_id, _ = raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"test-document:{journey.journey_id}:{tapan['pan_card:1']['document_id']}", task_type="DOCUMENT_MISSING", source_type="DOCUMENT",
            source_code="TEST", title="Check the PAN", description="x",
            reference={"documentId": str(tapan["pan_card:1"]["document_id"])}, severity="HIGH",
            registry=get_registry())

    monkeypatch.setenv("P2_FILE_IDENTITY_ENFORCE", "1")
    _settle(journey)
    assert _state(journey, *tapan.values()) == ["FOREIGN"] * 4
    assert _state(journey, *single) == ["ACTIVE"] * 3
    assert _state(journey, azizur["payment_receipt:4"]) == ["ACTIVE"]
    assert _legal_name(journey) == "AZIZUR REHAMAN"
    assert _payments(journey) == {"AMC-B/30001": True, "AMC-B/30580": False}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        task = connection.execute(text("SELECT task_status, completion_result FROM auditcore.p2_tasks "
                                       "WHERE tenant_id=:t AND task_id=:id"),
                                  {"t": journey.tenant_id, "id": task_id}).mappings().one()
        from audit_core.uc03_p2_stage import receipt_split

        split = receipt_split(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        view = files_view(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        enforced = connection.execute(text("SELECT identity_enforced_at_utc IS NOT NULL FROM auditcore.p2_upload_batches "
                                           "WHERE tenant_id=:t AND batch_id=:b"),
                                      {"t": journey.tenant_id, "b": tapan["pan_card:1"]["batch_id"]}).scalar_one()
    assert task["task_status"] == "VERIFIED_COMPLETE" and task["completion_result"]["outcome"] == "FOREIGN_FILE"
    assert [str(p["amount"]) for stage in ("BOOKING", "DELIVERY") for p in split[stage]] == ["21000.00"]
    assert view["customerName"] == "AZIZUR REHAMAN" and enforced is True
    tapan_file = next(f for f in view["files"] if f["fileName"] == "Tapan kumar patra.pdf")
    assert tapan_file["identity"] == {"status": "FOREIGN", "personName": "TAPAN KUMAR PATRA",
                                      "maskedPan": "AVFPP****G",
                                      "reason": "Names TAPAN KUMAR PATRA (PAN AVFPP****G), not the customer AZIZUR "
                                                "REHAMAN."}
    assert tapan_file["movedTo"] is None and set(tapan_file) == {"batchId", "fileName", "uploadedAtUtc",
                                                                 "identity", "movedTo"}
    # A repeated recompute changes nothing more.
    _settle(journey)
    assert _state(journey, *tapan.values()) == ["FOREIGN"] * 4 and _state(journey, *single) == ["ACTIVE"] * 3


_WRITES = {
    "evidence": "SELECT evidence_id::text, xmin::text, association_status FROM auditcore.evidence WHERE tenant_id=:t",
    "tasks": "SELECT task_id::text, xmin::text, task_status FROM auditcore.p2_tasks WHERE tenant_id=:t",
    "assessments": "SELECT journey_document_assessment_id::text, xmin::text, '' FROM "
                   "auditcore.journey_document_assessments WHERE tenant_id=:t",
    "activity": "SELECT event_id::text, xmin::text, event_type FROM auditcore.p2_activity_events "
                "WHERE tenant_id=:t",
    "captures": "SELECT di_document_id::text, xmin::text, capture_status FROM "
                "auditcore.document_capture_v2_documents WHERE tenant_id=:t",
    "payments": "SELECT payment_id::text, xmin::text, '' FROM auditcore.payments WHERE tenant_id=:t",
    "batches": "SELECT batch_id::text, xmin::text, coalesce(identity_status, '') FROM auditcore.p2_upload_batches "
               "WHERE tenant_id=:t",
    "decision": "SELECT journey_id::text, xmin::text, coalesce(customer_name, '') FROM "
                "auditcore.p2_journey_file_identity WHERE tenant_id=:t",
}


def _snapshot(journey) -> dict[str, set]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return {name: {tuple(r) for r in connection.execute(text(sql), {"t": journey.tenant_id}).all()}
                for name, sql in _WRITES.items()}


def _apply(journey) -> dict:
    from audit_core.uc03_p2_file_identity import apply_file_identity

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return apply_file_identity(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_with_enforcement_off_only_the_decision_is_written_and_only_when_it_changes(journey, monkeypatch):
    """The dry run (P2_FILE_IDENTITY_ENFORCE off) writes the decision columns and
    the booking's decision row -- nothing else: no evidence, task, assessment,
    capture, payment or activity change. A recompute deciding the same again
    writes nothing at all."""
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _azizur_then_tapan(journey)
    before = _snapshot(journey)
    summary = _apply(journey)
    assert (summary["foreign"], summary["changed"], summary["enforce"]) == (1, 2, False)
    first = _snapshot(journey)
    for name in ("evidence", "tasks", "assessments", "activity", "captures", "payments"):
        assert first[name] == before[name], name
    assert first["batches"] != before["batches"] and len(first["decision"]) == 1
    assert _apply(journey)["changed"] == 0
    assert _snapshot(journey) == first  # not one row rewritten (xmin unchanged)


def _switch(journey, enabled: bool, *, whole_project: bool = False) -> None:
    """The enforcement switch as a SuperAdmin request sets it (p2_file_identity_enforcement),
    as the runtime role: row security and the switch's recompute trigger run for real."""
    from audit_core.db import set_platform_super_admin_context
    from audit_core.dependencies import get_engine

    with get_engine().begin() as connection:
        set_platform_super_admin_context(connection)
        connection.execute(
            text("INSERT INTO auditcore.p2_file_identity_enforcement (tenant_id, journey_id, enabled, set_by_actor_id, "
                 "note) VALUES (:t, :j, :e, 'test-superadmin', 'pilot') "
                 "ON CONFLICT (tenant_id, (COALESCE(journey_id::text, '*'))) DO UPDATE SET enabled=EXCLUDED.enabled, "
                 "set_by_actor_id=EXCLUDED.set_by_actor_id, set_at_utc=now(), note=EXCLUDED.note"),
            {"t": journey.tenant_id, "j": None if whole_project else journey.journey_id, "e": enabled})


_STATE = {
    "evidence": "SELECT evidence_id::text, association_status, void_reason, voided_by_actor_id, voided_at_utc, "
                "pre_foreign_status, foreign_restore_state::text, supersedes_evidence_id::text "
                "FROM auditcore.evidence WHERE tenant_id=:t ORDER BY 1",
    "assessments": "SELECT journey_document_assessment_id::text, evidence_id::text, version_no, updated_at_utc "
                   "FROM auditcore.journey_document_assessments WHERE tenant_id=:t ORDER BY 1",
    "captures": "SELECT di_document_id::text, capture_status FROM auditcore.document_capture_v2_documents "
                "WHERE tenant_id=:t ORDER BY 1",
    "tasks": "SELECT task_id::text, dedupe_key, task_status, verified_at_utc, completion_result::text "
             "FROM auditcore.p2_tasks WHERE tenant_id=:t ORDER BY 1",
    "batches": "SELECT batch_id::text, identity_enforced_at_utc FROM auditcore.p2_upload_batches "
               "WHERE tenant_id=:t ORDER BY 1",
}


def _booking_state(journey) -> dict[str, list]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return {name: [tuple(r) for r in connection.execute(text(sql), {"t": journey.tenant_id}).all()]
                for name, sql in _STATE.items()}


def test_enforcement_is_switched_per_journey_or_project_from_the_database(journey, monkeypatch):
    from audit_core.uc03_p2_file_identity import enforce_enabled

    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    other = uuid4()

    def on(journey_id) -> bool:
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return enforce_enabled(connection, tenant_id=journey.tenant_id, journey_id=journey_id)

    assert on(journey.journey_id) is False
    _switch(journey, True)
    assert (on(journey.journey_id), on(other)) == (True, False)
    _switch(journey, False)
    assert on(journey.journey_id) is False
    _switch(journey, True, whole_project=True)
    assert (on(journey.journey_id), on(other)) == (True, True)
    _switch(journey, False, whole_project=True)
    assert on(journey.journey_id) is False
    monkeypatch.setenv("P2_FILE_IDENTITY_ENFORCE", "1")  # the global override
    assert on(other) is True
    # The runtime cannot write the switch: SuperAdmin requests only (row security).
    from audit_core.dependencies import get_engine

    with get_engine().connect() as connection, pytest.raises(Exception, match="row-level security"):
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("INSERT INTO auditcore.p2_file_identity_enforcement (tenant_id, journey_id, enabled) "
                                "VALUES (:t, NULL, true)"), {"t": journey.tenant_id})


def test_switching_enforcement_off_for_a_journey_puts_everything_back_exactly(journey, monkeypatch):
    """A pilot on one booking: switched on, Tapan's file leaves the booking,
    Azizur's copies come back and the task from Tapan's PAN closes; switched
    off, the next recompute puts the evidence, assessments, capture rows and
    tasks back exactly as they were before."""
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements, azizur, tapan = _azizur_then_tapan(journey)
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_document_assessments (tenant_id, journey_id, stage_code, "
                 "journey_document_requirement_id, requirement_key, evidence_id) VALUES (:t, :j, 'BOOKING', :r, "
                 "'pan_card', :e)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "r": requirements["pan_card"],
             "e": tapan["pan_card:1"]["evidence_id"]})
        task_id, _ = raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"test-document:{journey.journey_id}:{tapan['pan_card:1']['document_id']}", task_type="DOCUMENT_MISSING", source_type="DOCUMENT",
            source_code="TEST", title="Check the PAN", description="x",
            reference={"documentId": str(tapan["pan_card:1"]["document_id"])}, severity="HIGH",
            registry=get_registry())
    _settle(journey)  # decided, not enforced
    before = _booking_state(journey)
    _settle(journey)
    assert _booking_state(journey) == before

    _switch(journey, True)
    # The switch asks for the booking's recompute itself (the worker runs it).
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(text("SELECT work_status FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                                       "AND journey_id=:j AND work_type='STAGE_RECOMPUTE'"),
                                  {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one() == "PENDING"
    _settle(journey)
    assert _state(journey, *tapan.values()) == ["FOREIGN"] * 4
    assert _state(journey, azizur["pan_card:1"], azizur["aadhaar:2"], azizur["booking_docket:3"]) == ["ACTIVE"] * 3
    enforced = _booking_state(journey)
    [task] = [t for t in enforced["tasks"] if t[0] == str(task_id)]
    assert task[2] == "VERIFIED_COMPLETE" and json.loads(task[4])["outcome"] == "FOREIGN_FILE"

    _switch(journey, False)
    _settle(journey)
    after = _booking_state(journey)
    for name in ("evidence", "assessments", "captures", "batches"):
        assert after[name] == before[name], name
    assert after["tasks"] == before["tasks"]
    assert _legal_name(journey) == "TAPAN KUMAR PATRA"  # the customer record follows, as it stood
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = files_view(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert view["enforced"] is False
    assert next(f for f in view["files"] if f["fileName"] == "Tapan kumar patra.pdf")["identity"]["status"] == "UNKNOWN"


def test_a_file_found_to_belong_after_all_comes_back_as_it_was(journey, monkeypatch):
    """A bank approval letter naming Tapan as co-applicant arrives in Azizur's
    own file later: Tapan's file belongs, its documents come back and the
    newest copy is current again, as the upload rule had it."""
    monkeypatch.setenv("P2_FILE_IDENTITY_ENFORCE", "1")
    requirements, azizur, tapan = _azizur_then_tapan(journey)
    _settle(journey)
    assert _state(journey, tapan["pan_card:1"], azizur["pan_card:1"]) == ["FOREIGN", "ACTIVE"]
    _upload(journey, requirements, "Azizur Reheman loan.pdf", 30, [
        ("bank_approval_letter", "bank_approval_letter",
         {"finance_applicant_name": "Mr. AZIZUR REHAMAN", "finance_co_applicant_name": "TAPAN KUMAR PATRA"}),
    ])
    _settle(journey)
    assert _state(journey, *tapan.values()) == ["ACTIVE"] * 4
    assert _state(journey, azizur["pan_card:1"], azizur["aadhaar:2"], azizur["booking_docket:3"]) == ["SUPERSEDED"] * 3
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = files_view(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    tapan_file = next(f for f in view["files"] if f["fileName"] == "Tapan kumar patra.pdf")
    assert tapan_file["identity"]["status"] == "BELONGS" and "co-applicant" in tapan_file["identity"]["reason"]


def _merge_into_document(journey, requirements, page: dict, *, requirement: str, read: bool,
                         fields: dict | None = None) -> dict:
    """The page is grouped into a document of its own file, as the worker does
    it: its unit MERGED into a GROUP unit and its evidence voided as merged
    (uc03_p2_worker._retire_fragments). ``read``: the document was read and
    linked (ACTIVE -- the page had already superseded the copy then active, so
    there is nothing left to supersede); else never read (CANCELLED, "Not read
    within 30 minutes")."""
    group_id, document_id = uuid4(), uuid4()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_document_queue (tenant_id, queue_id, batch_id, journey_id, page_number, "
                 "page_numbers, page_sha256, page_object_key, client_upload_id, di_document_id, queue_status, "
                 "status_reason, classified_document_type, business_stage, unit_kind) "
                 "SELECT tenant_id, :g, batch_id, journey_id, page_number, ARRAY[page_number], :sha, page_object_key, "
                 ":c, :d, CAST(:s AS varchar), CAST(:why AS text), classified_document_type, business_stage, 'GROUP' "
                 "FROM auditcore.p2_document_queue WHERE tenant_id=:t AND di_document_id=:page"),
            {"t": journey.tenant_id, "g": group_id, "sha": uuid4().hex, "c": f"group-{uuid4().hex}",
             "d": document_id, "s": "READY" if read else "CANCELLED",
             "why": None if read else "Not read within 30 minutes. Please upload this page again.",
             "page": page["document_id"]})
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET queue_status='MERGED', merged_into_queue_id=:g "
                 "WHERE tenant_id=:t AND di_document_id=:page"),
            {"t": journey.tenant_id, "g": group_id, "page": page["document_id"]})
        connection.execute(
            text("UPDATE auditcore.evidence SET association_status='VOIDED', void_reason='P2_MERGED_INTO_DOCUMENT', "
                 "voided_at_utc=now() WHERE tenant_id=:t AND evidence_id=:e AND association_status='ACTIVE'"),
            {"t": journey.tenant_id, "e": page["evidence_id"]})
        evidence_id = None
        if read:
            evidence_id = connection.execute(
                text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, "
                     "journey_document_requirement_id, di_subject_id, di_document_id, document_type_key, "
                     "evidence_purpose, process_area, association_status) VALUES (:t, :j, :c, :r, :s, :d, :dt, "
                     "'BOOKING_DOCUMENT', 'BOOKING', 'ACTIVE') RETURNING evidence_id"),
                {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id,
                 "r": requirements[requirement], "s": uuid4(), "d": document_id, "dt": requirement},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO auditcore.document_capture_v2_documents (tenant_id, journey_id, stage_code, "
                     "di_document_id, client_upload_id, capture_status, created_by_actor_id) "
                     "VALUES (:t, :j, 'BOOKING', :d, :c, 'CLASSIFIED', :a)"),
                {"t": journey.tenant_id, "j": journey.journey_id, "d": document_id, "c": f"cap-{uuid4().hex}",
                 "a": journey.actor_id})
    for key, value in (fields or {}).items() if read else ():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type=_SOURCE_TYPE.get(requirement, requirement))
    return {"document_id": document_id, "evidence_id": UUID(str(evidence_id)) if evidence_id else None,
            "batch_id": page["batch_id"], "group_id": group_id}


def test_a_copy_replaced_by_a_page_of_the_foreign_file_comes_back_too(journey, monkeypatch):
    """Live 2026-11-01 (ADI-RD-261022-018): a page of the other customer's file
    superseded the customer's Aadhaar, then was merged into that file's
    grouped Aadhaar and voided as a page. Only the grouped document went
    FOREIGN, so the customer's own Aadhaar was never given back -- and with a
    masked PAN the booking kept the masked name. Now a copy replaced by any
    page of the file taken out comes back, and is superseded again exactly as
    it was when the file stops being taken out."""
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    requirements = _requirements(journey)
    bikash = _upload(journey, requirements, "Bikash Kumar Martha Booking .pdf", 0, [
        ("pan_card", "pan_card", {"pan_number": "FJBPM5078P", "pan_name": "BXXXXH KXXXR MXXXXA"}),
        ("aadhaar", "aadhaar", {"aadhaar_number": "4410 2290 8812", "aadhaar_name": "Bikash Kumar Martha"}),
        ("booking_docket", "booking_docket", {"customer_name": "Bikash Kumar Martha", "dealer_name": "Aditya Motors"}),
    ])
    sudhanshu = _upload(journey, requirements, "Sudhanshu Sekhar Nayak.pdf", 4, [
        ("aadhaar", "aadhaar", {"aadhaar_number": "9921 5530 1874", "aadhaar_name": "Aswini Kumar Nayak"}),
        ("booking_docket", "booking_docket", {"customer_name": "Sudhansu Sekhar Nayak", "dealer_name": "Aditya Motors"}),
        ("pan_card", "pan_card", {"pan_number": "ARSPN4417H", "pan_name": "SUDHANSU SEKHAR NAYAK"}),
    ])
    # Sudhanshu's Aadhaar page went into his file's grouped Aadhaar (read).
    grouped = _merge_into_document(journey, requirements, sudhanshu["aadhaar:1"], requirement="aadhaar", read=True,
                                   fields={"aadhaar_number": "9921 5530 1874", "aadhaar_name": "Aswini Kumar Nayak"})
    with journey.engine.begin() as connection:  # Bikash's file stays the larger one
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET page_count=12 WHERE tenant_id=:t AND batch_id=:b"),
                           {"t": journey.tenant_id, "b": bikash["pan_card:1"]["batch_id"]})
    _settle(journey)  # decided, not taken out
    before = _booking_state(journey)
    assert _state(journey, bikash["aadhaar:2"]) == ["SUPERSEDED"]

    _switch(journey, True)
    _settle(journey)
    assert _state(journey, grouped, sudhanshu["booking_docket:2"], sudhanshu["pan_card:3"]) == ["FOREIGN"] * 3
    assert _state(journey, sudhanshu["aadhaar:1"]) == ["VOIDED"]  # the page, as it was
    assert _state(journey, *bikash.values()) == ["ACTIVE"] * 3
    # Masked PAN, no GSTIN: the Aadhaar names him (product owner 2026-10-28).
    assert _legal_name(journey) == "Bikash Kumar Martha"
    _settle(journey)  # a repeated recompute changes nothing more
    assert _state(journey, *bikash.values()) == ["ACTIVE"] * 3

    _switch(journey, False)
    _settle(journey)
    after = _booking_state(journey)
    for name in ("evidence", "captures", "batches"):
        assert after[name] == before[name], name


def _page_tasks(journey, batch_id: UUID) -> dict[str, tuple]:
    """The file and page tasks of one uploaded file: kind -> (status, outcome)."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        rows = connection.execute(
            text("SELECT split_part(dedupe_key, ':', 1), task_status, completion_result->>'outcome' "
                 "FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND reference->>'batchId' = :b "
                 "AND split_part(dedupe_key, ':', 1) IN ('unclassified', 'upload-quality', 'large-upload')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "b": str(batch_id)}).all()
    return {kind: (status, outcome) for kind, status, outcome in rows}


def test_a_taken_out_files_page_tasks_stay_closed_and_come_back_with_the_file(journey, monkeypatch):
    """Pilot 2026-10-31 (UTK-SU-261019-001, UTK-RM-261019-002): the task to
    set the type of an unread page, and the image-quality task, of another
    customer's file were closed when the file was taken out -- and reopened
    by their own producers in the same recompute. While the file is out they
    stay closed; switched off, they come back as they were."""
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    monkeypatch.setattr("audit_core.uc03_p2_task_producer.LARGE_UPLOAD_PAGES", 5)
    _, azizur, tapan = _azizur_then_tapan(journey)
    batch = tapan["pan_card:1"]["batch_id"]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for page, status, error, template in ((5, "SUPPORTING", None, "supporting_document"),
                                              (6, "FAILED", "DI_QUALITY_IMAGE_BLUR_SCORE", None)):
            connection.execute(
                text("INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, "
                     "page_sha256, page_object_key, client_upload_id, queue_status, last_error, template_key, "
                     "business_stage) VALUES (:t, :b, :j, :p, :sha, :k, :c, :s, :e, :tk, 'BOOKING')"),
                {"t": journey.tenant_id, "b": batch, "j": journey.journey_id, "p": page, "sha": uuid4().hex,
                 "k": f"p2/{batch}/{page}", "c": f"p2-{uuid4().hex}", "s": status, "e": error, "tk": template})
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET batch_status='PARTIAL_FAILURE', page_count=6 "
                                "WHERE tenant_id=:t AND batch_id=:b"), {"t": journey.tenant_id, "b": batch})
        # Azizur's file stays the larger one, so he stays the customer.
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET page_count=12 WHERE tenant_id=:t AND batch_id=:b"),
                           {"t": journey.tenant_id, "b": azizur["pan_card:1"]["batch_id"]})
    _settle(journey)  # decided, not taken out: the file's tasks are raised
    assert _page_tasks(journey, batch) == {"unclassified": ("READY", None), "upload-quality": ("READY", None),
                                    "large-upload": ("READY", None)}
    before = _booking_state(journey)

    _switch(journey, True)
    for _ in range(2):  # and a later recompute does not raise them again
        _settle(journey)
        assert _page_tasks(journey, batch) == {kind: ("VERIFIED_COMPLETE", "FOREIGN_FILE")
                                        for kind in ("unclassified", "upload-quality", "large-upload")}
    _switch(journey, False)
    _settle(journey)
    assert _page_tasks(journey, batch) == {"unclassified": ("READY", None), "upload-quality": ("READY", None),
                                           "large-upload": ("READY", None)}
    assert _state(journey, *tapan.values()) == ["ACTIVE"] * 4  # back as the upload rule had it
    assert _booking_state(journey)["tasks"] == before["tasks"]


def test_a_late_link_callback_does_not_bring_a_foreign_document_back():
    """A delayed duplicate DI callback re-activated any non-active repeatable
    document (a receipt): a FOREIGN one is acknowledged and stays out."""
    import inspect

    from audit_core import uc03_pc_booking_documents

    source = inspect.getsource(uc03_pc_booking_documents.acknowledge_booking_document_link)
    guard = source.index('existing_for_document["association_status"] == "FOREIGN"')
    reactivate = source.index("SET association_status='ACTIVE', void_reason=NULL,")
    assert guard < reactivate
    assert source[guard:reactivate].count("return BookingDocumentLinkResponse(") == 1


def test_the_scan_job_reports_and_with_commit_records_and_asks_for_a_recompute(journey, monkeypatch, capsys):
    from audit_core import uc03_file_identity_scan as scan

    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    _azizur_then_tapan(journey)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(scan.logger, "info", lambda event, **kw: events.append((event, kw)))
    totals = scan.run_file_identity_scan(journey.engine, f"{journey.tenant_id}:scan-2026-10-31")
    assert (totals["journeys"], totals["foreign_files"], totals["foreign_journeys"]) == (1, 1, 1)
    items = [kw for event, kw in events if event == "file_identity_scan_item"]
    assert {(i["file_name"], i["status"]) for i in items} == {("Azizur Reheman.pdf", "BELONGS")}
    # For review: one line per FOREIGN / UNKNOWN file with what enforcement would do.
    [review] = [kw for event, kw in events if event == "file_identity_scan_review"]
    assert {k: review[k] for k in ("journey_reference", "pc", "file_name", "status", "person", "masked_pan",
                                   "would_take_out_documents", "of_which_active", "would_stop_receipts",
                                   "receipts_amount", "would_restore_copies")} == {
        "journey_reference": review["journey_reference"], "pc": None, "file_name": "Tapan kumar patra.pdf",
        "status": "FOREIGN", "person": "TAPAN KUMAR PATRA", "masked_pan": "AVFPP****G",
        "would_take_out_documents": 4, "of_which_active": 4, "would_stop_receipts": 1, "receipts_amount": 51000.0,
        "would_restore_copies": 3}
    assert "AZIZUR REHAMAN" in review["reason"]
    [restores] = [kw for event, kw in events if event == "file_identity_scan_restores"]
    assert sorted(restores["restored"]) == ["Aadhaar: AZIZUR REHAMAN", "Booking Docket: AZIZUR REHAMAN",
                                            "PAN Card: AZIZUR REHAMAN"]
    finished = next(kw for event, kw in events if event == "file_identity_scan_finished")
    assert (finished["documents_out"], finished["receipts_out"], finished["copies_restored"]) == (4, 1, 3)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(text("SELECT count(*) FROM auditcore.p2_upload_batches WHERE tenant_id=:t "
                                       "AND identity_status IS NOT NULL"), {"t": journey.tenant_id}).scalar_one() == 0
    totals = scan.run_file_identity_scan(journey.engine, f"{journey.tenant_id}:scan-2026-10-31-commit")
    assert totals["recomputes_requested"] == 1
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        statuses = dict(connection.execute(text("SELECT original_filename, identity_status FROM auditcore.p2_upload_batches "
                                                "WHERE tenant_id=:t"), {"t": journey.tenant_id}).all())
        queued = connection.execute(text("SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                                         "AND work_type='STAGE_RECOMPUTE'"), {"t": journey.tenant_id}).scalar_one()
        evidence = connection.execute(text("SELECT count(*) FROM auditcore.evidence WHERE tenant_id=:t "
                                           "AND association_status='FOREIGN'"), {"t": journey.tenant_id}).scalar_one()
    assert statuses == {"Azizur Reheman.pdf": "BELONGS", "Tapan kumar patra.pdf": "FOREIGN"}
    assert queued == 1 and evidence == 0  # the recompute, not the job, acts on it
    # "-commit" never turns enforcement on: the recompute it asked for decides only.
    _settle(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert connection.execute(text("SELECT count(*) FROM auditcore.p2_file_identity_enforcement "
                                       "WHERE tenant_id=:t"), {"t": journey.tenant_id}).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM auditcore.evidence WHERE tenant_id=:t "
                                       "AND association_status='FOREIGN'"), {"t": journey.tenant_id}).scalar_one() == 0
    with pytest.raises(ValueError):
        scan.run_file_identity_scan(journey.engine, "no-tenant")


def test_system_health_counts_bookings_holding_foreign_files(journey):
    from audit_core.system_health_api import build_alerts, pipeline_health

    _azizur_then_tapan(journey)
    _settle(journey)  # decided (dry run by default)
    with journey.engine.begin() as connection:
        pipeline = pipeline_health(connection, datetime.now(UTC) - timedelta(hours=24))
    [mine] = [p for p in pipeline["projects"] if p["tenantId"] == journey.tenant_id]
    assert mine["fileIdentity"] == {"foreignJourneys": 1, "foreignFiles": 1, "enforcedFiles": 0,
                                    "unknownJourneys": 0, "unknownFiles": 0}
    alerts = build_alerts([], {"webhookConfigured": True, "failedOrCrashed": [], "monitorAlerts": []},
                          {"projects": [mine]}, {})
    assert [a["code"] for a in alerts] == ["FOREIGN_FILES"]
    assert json.dumps(mine).count(str(journey.journey_id)) == 0
