from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import psycopg
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from verigence_security.auth_store import PostgresAuthStore
from verigence_security.role_templates import PostgresRoleTemplateStore, RoleTemplateService
from verigence_security.settings import normalize_database_url

TENANT_ID = os.environ.get("DUMMY_TENANT_ID", "DummyTenant")
USER_ID = "dummy-pc-user"
USER_EXTERNAL_SUBJECT = "dummy-pc-user-external"
USER_EMAIL = "dummy.pc@verigence.test"
ROLE = "PC"

TEST_CLIENT_ID = os.environ["TEST_CLIENT_ID"]
TEST_CLIENT_SECRET = os.environ["TEST_CLIENT_SECRET"]
TEST_REDIRECT_URI = os.environ["TEST_REDIRECT_URI"]
SECURITY_BASE_URL = os.environ["SECURITY_BASE_URL"].rstrip("/")
AUDIT_BASE_URL = os.environ["AUDIT_BASE_URL"].rstrip("/")
SECURITY_DATABASE_URL = normalize_database_url(os.environ["SECURITY_DATABASE_URL"])
AUDIT_DATABASE_URL = normalize_database_url(os.environ["AUDIT_DATABASE_URL"])
DI_DATABASE_URL = normalize_database_url(os.environ["DI_DATABASE_URL"])

POLL_SECONDS = 5
POLL_TIMEOUT = 210


def _uid(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"verigence:{TENANT_ID}:{name}")


DEALER_ID = _uid("dealer")
OUTLET_ID = _uid("outlet")
CUSTOMER_ID = _uid("customer")
JOURNEY_ID = _uid("journey")
ASSIGNMENT_ID = _uid("pc-assignment")
RETENTION_POLICY_ID = _uid("di-retention")
PROFILE_ID = _uid("booking-form-profile")


BOOKING_FIELDS: tuple[tuple[str, str, bool, str], ...] = (
    ("dealer_name", "STRING", True, "Name of the dealership"),
    ("dealer_branch", "STRING", False, "Branch or location of the dealer"),
    ("booking_reference_number", "IDENTIFIER", True, "Booking reference or order number"),
    ("booking_date", "DATE", True, "Date the booking was made"),
    ("customer_name", "STRING", True, "Full name of the customer"),
    ("customer_phone", "PHONE", True, "Customer contact phone number"),
    ("customer_email", "EMAIL", False, "Customer email address"),
    ("customer_address", "STRING", False, "Customer residential or postal address"),
    ("vehicle_model", "STRING", True, "Vehicle model name"),
    ("vehicle_variant", "STRING", True, "Vehicle variant or trim level"),
    ("vehicle_color", "STRING", True, "Preferred or booked vehicle colour"),
    ("sales_person", "STRING", False, "Name of the sales executive"),
    ("ex_showroom_price", "CURRENCY", False, "Ex-showroom price of the vehicle"),
    ("insurance_amount", "CURRENCY", False, "Insurance component of the total price"),
    ("road_tax_registration", "CURRENCY", False, "Road tax and registration charges"),
    ("accessories_cost", "CURRENCY", False, "Cost of accessories added"),
    ("other_charges", "CURRENCY", False, "Any other charges listed"),
    ("total_price", "CURRENCY", True, "Grand total on-road price"),
    ("booking_amount_paid", "CURRENCY", True, "Amount paid as booking advance"),
    ("balance_amount", "CURRENCY", False, "Remaining balance due at delivery"),
    ("mode_of_payment", "STRING", False, "Payment mode used for the booking amount"),
    ("payment_reference_no", "IDENTIFIER", False, "Cheque, DD, or NEFT/RTGS reference"),
    ("expected_delivery", "STRING", False, "Expected delivery date or timeframe"),
)


def marker(name: str, value: str = "PASS") -> None:
    print(f"{name}={value}", flush=True)


def wait_health(url: str) -> None:
    last = ""
    for _ in range(60):
        try:
            response = httpx.get(url, timeout=5.0)
            last = f"{response.status_code} {response.text[:160]}"
            if response.status_code == 200:
                return
        except Exception as exc:  # pragma: no cover - live diagnostic
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Service did not become healthy: {url}: {last}")


def seed_security() -> str:
    role_service = RoleTemplateService(PostgresRoleTemplateStore(SECURITY_DATABASE_URL))
    role_service.seed_platform_defaults()
    rows = role_service.seed_tenant(
        tenant_id=TENANT_ID,
        actor_sub="DUMMY_E2E",
        correlation_id="dummytenant-booking-e2e",
        replace=False,
    )
    role_keys = {row.role_key for row in rows}
    required_roles = {"PC", "TL", "PM"}
    if not required_roles.issubset(role_keys):
        raise RuntimeError(f"DummyTenant missing operational role templates: {required_roles-role_keys}")
    marker("DUMMYTENANT_ROLE_TEMPLATES")

    auth_store = PostgresAuthStore(SECURITY_DATABASE_URL)
    auth_store.ensure_tenant(TENANT_ID)
    auth_store.upsert_user(
        user_id=USER_ID,
        external_subject=USER_EXTERNAL_SUBJECT,
        email=USER_EMAIL,
        active=True,
    )
    auth_store.upsert_membership(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        roles=(ROLE,),
        direct_permissions=frozenset(),
        active=True,
    )

    session_token = secrets.token_urlsafe(48)
    auth_store.create_session(
        session_token,
        user_id=USER_ID,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )

    wait_health(SECURITY_BASE_URL + "/health")
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        authorize = client.get(
            SECURITY_BASE_URL + "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": TEST_CLIENT_ID,
                "redirect_uri": TEST_REDIRECT_URI,
                "state": "dummytenant-booking-e2e",
                "tenant_id": TENANT_ID,
            },
            cookies={"verigence_session": session_token},
        )
        if authorize.status_code != 302:
            raise RuntimeError(f"Security authorize failed: {authorize.status_code} {authorize.text[:300]}")
        query = parse_qs(urlparse(authorize.headers["location"]).query)
        code = query.get("code", [None])[0]
        if not code:
            raise RuntimeError(f"Security authorize returned no code: {authorize.headers['location']}")

        token_response = client.post(
            SECURITY_BASE_URL + "/oauth/token",
            auth=(TEST_CLIENT_ID, TEST_CLIENT_SECRET),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": TEST_REDIRECT_URI,
            },
        )
        if token_response.status_code != 200:
            raise RuntimeError(
                f"Security token exchange failed: {token_response.status_code} {token_response.text[:300]}"
            )
        user_token = token_response.json()["access_token"]
        jwks = client.get(SECURITY_BASE_URL + "/.well-known/jwks.json").json()

    header = jwt.get_unverified_header(user_token)
    jwk = next(item for item in jwks["keys"] if item["kid"] == header["kid"])
    claims = jwt.decode(
        user_token,
        jwt.PyJWK.from_dict(jwk).key,
        algorithms=["RS256"],
        audience="verigence-platform",
        issuer="verigence-security",
    )
    if claims.get("sub") != USER_ID or claims.get("tenant_id") != TENANT_ID:
        raise RuntimeError("Security USER token has unexpected subject or tenant")
    if claims.get("actor_type") != "USER" or ROLE not in claims.get("roles", []):
        raise RuntimeError("Security USER token is not a PC USER token")
    permissions = set(claims.get("permissions", []))
    required = {
        "audit.evidence.upload",
        "audit.evidence.refresh",
        "di.subject.create",
        "di.document.upload",
        "di.document.read",
        "di.document.fields.read",
    }
    missing = required - permissions
    if missing:
        raise RuntimeError(f"PC role is missing E2E permissions: {sorted(missing)}")
    marker("DUMMYTENANT_PC_USER")
    return user_token


def seed_audit_core() -> None:
    with psycopg.connect(AUDIT_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))

        cur.execute(
            """
            INSERT INTO auditcore.product_categories (category_code, category_name)
            VALUES ('DUMMY_CAR', 'Dummy Four Wheeler')
            ON CONFLICT (category_code) DO UPDATE SET category_name=EXCLUDED.category_name
            RETURNING product_category_id
            """
        )
        category_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO auditcore.oems (oem_code, oem_name)
            VALUES ('DUMMY_OEM', 'Dummy Motors')
            ON CONFLICT (oem_code) DO UPDATE SET oem_name=EXCLUDED.oem_name
            RETURNING oem_id
            """
        )
        oem_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO auditcore.projects (
                tenant_id, project_code, project_name, oem_id, product_category_id,
                effective_start_date, project_status, created_by_actor_id
            ) VALUES (%s, 'DUMMY_PROJECT', 'DummyTenant Audit Project', %s, %s,
                      DATE '2026-01-01', 'ACTIVE', %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                project_name=EXCLUDED.project_name,
                oem_id=EXCLUDED.oem_id,
                product_category_id=EXCLUDED.product_category_id,
                project_status='ACTIVE'
            """,
            (TENANT_ID, oem_id, category_id, USER_ID),
        )
        cur.execute(
            """
            INSERT INTO auditcore.dealers (
                tenant_id, dealer_id, dealer_code, dealer_name, legal_name, status, created_by_actor_id
            ) VALUES (%s, %s, 'DUMMY_DEALER', 'Dummy Motors Mohali', 'Dummy Motors Pvt Ltd', 'ACTIVE', %s)
            ON CONFLICT (tenant_id, dealer_id) DO UPDATE SET dealer_name=EXCLUDED.dealer_name, status='ACTIVE'
            """,
            (TENANT_ID, DEALER_ID, USER_ID),
        )
        cur.execute(
            """
            INSERT INTO auditcore.dealer_outlets (
                tenant_id, dealer_id, outlet_id, outlet_code, outlet_name,
                outlet_classification, city, state_region, postal_code, status, created_by_actor_id
            ) VALUES (%s, %s, %s, 'DUMMY_MOHALI', 'Dummy Motors Mohali',
                      'ONSITE', 'Mohali', 'Punjab', '160062', 'ACTIVE', %s)
            ON CONFLICT (tenant_id, outlet_id) DO UPDATE SET outlet_name=EXCLUDED.outlet_name, status='ACTIVE'
            """,
            (TENANT_ID, DEALER_ID, OUTLET_ID, USER_ID),
        )
        cur.execute(
            """
            INSERT INTO auditcore.customers (
                tenant_id, dealer_id, outlet_id, customer_id, customer_type_code,
                display_name, mobile_last4, email_reference, external_customer_ref,
                status, created_by_actor_id
            ) VALUES (%s, %s, %s, %s, 'INDIVIDUAL', 'Dummy Customer', '3210',
                      'dummy.customer@example.com', 'DUMMY-CUSTOMER-001', 'ACTIVE', %s)
            ON CONFLICT (tenant_id, customer_id) DO UPDATE SET
                display_name=EXCLUDED.display_name, status='ACTIVE'
            """,
            (TENANT_ID, DEALER_ID, OUTLET_ID, CUSTOMER_ID, USER_ID),
        )
        cur.execute(
            """
            INSERT INTO auditcore.journeys (
                tenant_id, dealer_id, outlet_id, customer_id, journey_id,
                journey_reference, observed_status_code, observed_status_source,
                created_by_actor_id
            ) VALUES (%s, %s, %s, %s, %s, 'DUMMY-JOURNEY-001', NULL, NULL, %s)
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                journey_reference=EXCLUDED.journey_reference,
                observed_status_code=NULL,
                observed_status_source=NULL
            """,
            (TENANT_ID, DEALER_ID, OUTLET_ID, CUSTOMER_ID, JOURNEY_ID, USER_ID),
        )
        cur.execute(
            """
            INSERT INTO auditcore.business_assignments (
                tenant_id, assignment_id, security_actor_id, business_role_code,
                dealer_id, outlet_id, assignment_status, created_by_actor_id
            ) VALUES (%s, %s, %s, 'PC', %s, %s, 'ACTIVE', 'DUMMY_E2E')
            ON CONFLICT (tenant_id, assignment_id) DO UPDATE SET
                security_actor_id=EXCLUDED.security_actor_id,
                business_role_code='PC', dealer_id=EXCLUDED.dealer_id,
                outlet_id=EXCLUDED.outlet_id, assignment_status='ACTIVE', effective_to=NULL
            """,
            (TENANT_ID, ASSIGNMENT_ID, USER_ID, DEALER_ID, OUTLET_ID),
        )
        conn.commit()
    marker("DUMMYTENANT_AUDIT_FIXTURE")


def seed_di_profile() -> None:
    with psycopg.connect(DI_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
        cur.execute(
            """
            INSERT INTO docintel.tenant_settings (
                tenant_id, tenant_storage_key, timezone_name, eod_retry_local_time,
                eod_retry_enabled, classification_acceptance_score,
                subject_matching_min_confidence, upload_timeout_minutes,
                max_upload_bytes, allowed_mime_types, quality_policy,
                whatsapp_subject_reference_prefix, status, created_at_utc, updated_at_utc
            ) VALUES (%s, %s, 'Asia/Kolkata', TIME '23:00:00', true, 70.00, 80.00,
                      30, 31457280, '["application/pdf","image/jpeg","image/png","image/tiff"]'::jsonb,
                      '[]'::jsonb, '', 'ACTIVE', now(), now())
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            (TENANT_ID, _uid("di-storage")),
        )
        cur.execute(
            """
            INSERT INTO docintel.retention_policies (
                tenant_id, retention_policy_id, policy_key, display_name,
                retention_days, disposition, status, created_at_utc, updated_at_utc
            ) VALUES (%s, %s, 'dummy-default', 'DummyTenant DEV Retention',
                      365, 'PURGE_CONTENT', 'ACTIVE', now(), now())
            ON CONFLICT (tenant_id, policy_key) DO NOTHING
            """,
            (TENANT_ID, RETENTION_POLICY_ID),
        )
        cur.execute(
            """
            SELECT retention_policy_id FROM docintel.retention_policies
            WHERE tenant_id=%s AND policy_key='dummy-default'
            """,
            (TENANT_ID,),
        )
        retention_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE docintel.tenant_settings SET active_retention_policy_id=%s WHERE tenant_id=%s",
            (retention_id, TENANT_ID),
        )
        cur.execute(
            """
            SELECT document_type_id, COALESCE(category, 'HANDWRITTEN')
            FROM docintel.document_types
            WHERE owner_tenant_id IS NULL AND document_type_key='booking_form' AND status='ACTIVE'
            """
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("DI booking_form global document type is missing after migrations")
        document_type_id, category = row
        cur.execute(
            """
            INSERT INTO docintel.tenant_document_types (
                tenant_id, document_type_id, physical_form_type, requires_processing,
                is_active, display_order, created_at_utc, updated_at_utc
            ) VALUES (%s, %s, %s, true, true, 10, now(), now())
            ON CONFLICT (tenant_id, document_type_id) DO UPDATE SET
                requires_processing=true, is_active=true, physical_form_type=EXCLUDED.physical_form_type
            """,
            (TENANT_ID, document_type_id, category),
        )

        cur.execute(
            """
            SELECT profile_id, status FROM docintel.extraction_profiles
            WHERE document_type_id=%s AND scope_tenant_id=%s
            ORDER BY version_no DESC
            LIMIT 1
            """,
            (document_type_id, TENANT_ID),
        )
        profile_row = cur.fetchone()
        if profile_row is not None and profile_row[1] == "PUBLISHED":
            cur.execute(
                """
                SELECT count(*) FROM docintel.extraction_profile_fields
                WHERE profile_id=%s AND enabled=true
                """,
                (profile_row[0],),
            )
            enabled_count = cur.fetchone()[0]
            if enabled_count < len(BOOKING_FIELDS):
                raise RuntimeError(
                    "DummyTenant has an incomplete PUBLISHED Booking Form profile; "
                    "refusing to mutate published profile children"
                )
            conn.commit()
            marker("DUMMYTENANT_BOOKING_PROFILE")
            return

        profile_id = profile_row[0] if profile_row is not None else PROFILE_ID
        if profile_row is None:
            cur.execute(
                """
                INSERT INTO docintel.extraction_profiles (
                    profile_id, document_type_id, scope_tenant_id, version_no,
                    profile_name, status, classification_hint,
                    created_by_actor_id, published_by_actor_id,
                    created_at_utc, published_at_utc, updated_at_utc
                ) VALUES (%s, %s, %s, 1, 'DummyTenant Booking Form v1', 'DRAFT',
                          'Booking Form', 'DUMMY_E2E', NULL, now(), NULL, now())
                """,
                (profile_id, document_type_id, TENANT_ID),
            )
        elif profile_row[1] != "DRAFT":
            raise RuntimeError(f"Unexpected DummyTenant Booking Form profile state: {profile_row[1]}")

        for sequence, (field_key, data_type, required, description) in enumerate(BOOKING_FIELDS, 1):
            cur.execute(
                """
                SELECT canonical_field_id FROM docintel.canonical_fields
                WHERE owner_tenant_id=%s AND field_key=%s
                """,
                (TENANT_ID, field_key),
            )
            existing = cur.fetchone()
            field_id = existing[0] if existing else _uid(f"field:{field_key}")
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO docintel.canonical_fields (
                        canonical_field_id, owner_tenant_id, field_key, display_name,
                        data_type, description, status, created_at_utc, updated_at_utc
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE', now(), now())
                    """,
                    (
                        field_id,
                        TENANT_ID,
                        field_key,
                        field_key.replace("_", " ").title(),
                        data_type,
                        description,
                    ),
                )
            cur.execute(
                """
                INSERT INTO docintel.extraction_profile_fields (
                    profile_field_id, profile_id, canonical_field_id, enabled, expected,
                    extraction_instruction, aliases, score_included, score_weight,
                    manual_correction_allowed, display_sequence, created_at_utc, updated_at_utc
                ) VALUES (%s, %s, %s, true, %s, %s, '[]'::jsonb, true, 1.0, true, %s, now(), now())
                ON CONFLICT (profile_id, canonical_field_id) DO NOTHING
                """,
                (_uid(f"profile-field:{field_key}"), profile_id, field_id, required, description, sequence),
            )

        cur.execute(
            """
            UPDATE docintel.extraction_profiles
            SET status='PUBLISHED', published_by_actor_id='DUMMY_E2E',
                published_at_utc=now(), updated_at_utc=now()
            WHERE profile_id=%s AND status='DRAFT'
            """,
            (profile_id,),
        )
        conn.commit()
    marker("DUMMYTENANT_BOOKING_PROFILE")


def build_booking_form_pdf() -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    pdf.setTitle("DummyTenant Booking Form")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawCentredString(width / 2, height - 45, "ORDER BOOKING FORM")
    pdf.setFont("Helvetica", 10)
    y = height - 80
    rows = [
        ("Dealer Name", "Dummy Motors Pvt Ltd"),
        ("Dealer Branch", "Mohali"),
        ("Booking Reference Number", "DBF-2026-0001"),
        ("Booking Date", "16/08/2026"),
        ("Customer Name", "Dummy Customer"),
        ("Customer Phone", "9876543210"),
        ("Customer Email", "dummy.customer@example.com"),
        ("Customer Address", "Sector 67, Mohali, Punjab"),
        ("Vehicle Model", "Creta"),
        ("Vehicle Variant", "SX(O) 1.5 Petrol"),
        ("Vehicle Colour", "Black"),
        ("Sales Person", "Test Sales Executive"),
        ("Ex-Showroom Price", "Rs. 14,20,700"),
        ("Insurance Amount", "Rs. 54,500"),
        ("Road Tax / Registration", "Rs. 1,44,310"),
        ("Accessories Cost", "Rs. 20,000"),
        ("Other Charges", "Rs. 5,000"),
        ("Total Price", "Rs. 16,44,510"),
        ("Booking Amount Paid", "Rs. 5,001"),
        ("Balance Amount", "Rs. 16,39,509"),
        ("Mode of Payment", "NEFT/RTGS"),
        ("Payment Reference No", "TESTNEFT123"),
        ("Expected Delivery", "30/09/2026"),
    ]
    for label, value in rows:
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(45, y, f"{label}:")
        pdf.setFont("Helvetica", 9)
        pdf.drawString(190, y, value)
        pdf.line(185, y - 2, width - 45, y - 2)
        y -= 28
        if y < 70:
            pdf.showPage()
            y = height - 55
    pdf.save()
    return buffer.getvalue()


def _get_di_document_id(evidence_id: str) -> str | None:
    with psycopg.connect(AUDIT_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
        cur.execute(
            "SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=%s AND evidence_id=%s",
            (TENANT_ID, uuid.UUID(evidence_id)),
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None


def _di_diagnostic(document_id: str | None) -> str:
    if not document_id:
        return "DI document ID is unavailable"
    with psycopg.connect(DI_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
        cur.execute(
            """
            SELECT processing_status, confirmation_status, processing_failure_code
            FROM docintel.documents WHERE tenant_id=%s AND document_id=%s
            """,
            (TENANT_ID, uuid.UUID(document_id)),
        )
        row = cur.fetchone()
        if row is None:
            return "DI document row is missing"
        return f"processing={row[0]} confirmation={row[1]} failure={row[2]}"


def run_booking_upload(user_token: str) -> list[dict]:
    wait_health(AUDIT_BASE_URL + "/health")
    booking_path = Path(os.environ["BOOKING_FORM_PATH"])
    booking_pdf = booking_path.read_bytes()
    actual_hash = hashlib.sha256(booking_pdf).hexdigest()
    expected_hash = os.environ["BOOKING_FORM_SHA256"].strip().lower()
    if actual_hash != expected_hash:
        raise RuntimeError(f"Booking Form SHA-256 mismatch: {actual_hash} != {expected_hash}")
    print(f"DUMMYTENANT_BOOKING_FORM_SHA256={actual_hash}", flush=True)
    marker("DUMMYTENANT_EXACT_BOOKING_FORM")

    idempotency_key = "dummy-booking-" + secrets.token_hex(8)
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Idempotency-Key": idempotency_key,
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{AUDIT_BASE_URL}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/evidence",
            headers=headers,
            files={"file": (booking_path.name, booking_pdf, "application/pdf")},
            data={
                "evidencePurpose": "BOOKING_FORM",
                "documentTypeKey": "booking_form",
            },
        )
    if response.status_code != 201:
        raise RuntimeError(f"Audit Core Booking Form upload failed: {response.status_code} {response.text[:500]}")
    evidence = response.json()
    evidence_id = evidence["evidenceId"]
    marker("DUMMYTENANT_BOOKING_UPLOAD")
    marker("DUMMYTENANT_AUDIT_SECURITY_DI_PATH")

    deadline = time.time() + POLL_TIMEOUT
    last = ""
    with httpx.Client(timeout=30.0) as client:
        while time.time() < deadline:
            refresh = client.post(
                f"{AUDIT_BASE_URL}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/evidence/{evidence_id}/refresh",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            last = f"{refresh.status_code} {refresh.text[:300]}"
            if refresh.status_code == 200:
                detail = refresh.json()
                facts = detail.get("facts", [])
                if detail.get("processingStatus") == "PROCESSED" and facts:
                    marker("DUMMYTENANT_BOOKING_EXTRACTION")
                    return facts
            elif refresh.status_code not in {409, 422, 503}:
                raise RuntimeError(f"Audit Core refresh failed: {last}")
            time.sleep(POLL_SECONDS)

    di_document_id = _get_di_document_id(evidence_id)
    diagnostic = _di_diagnostic(di_document_id)
    raise RuntimeError(f"Booking extraction timed out; last refresh={last}; {diagnostic}")


def validate_and_print(fact_rows: list[dict]) -> None:
    if not fact_rows:
        raise RuntimeError("DI returned no Booking Form fields")
    print("DUMMYTENANT_BOOKING_FIELDS=" + json.dumps(fact_rows, default=str, sort_keys=True), flush=True)
    non_empty = [row for row in fact_rows if row.get("value") not in (None, "", [])]
    print(f"DUMMYTENANT_BOOKING_NON_EMPTY_FIELDS={len(non_empty)}", flush=True)
    marker("DUMMYTENANT_BOOKING_CORE_FIELDS")


def main() -> None:
    print(f"DummyTenant Booking Form E2E tenant={TENANT_ID} user={USER_ID} role={ROLE}", flush=True)
    user_token = seed_security()
    seed_audit_core()
    seed_di_profile()
    facts = run_booking_upload(user_token)
    validate_and_print(facts)
    marker("DUMMYTENANT_BOOKING_E2E")


if __name__ == "__main__":
    main()
