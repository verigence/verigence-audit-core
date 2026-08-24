from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
from PIL import Image, ImageDraw, ImageFont

RESULT_PATH = Path("docs/runtime/uc03-field-review-live-e2e.txt")
WEB_BASE = os.environ.get("WEB_BASE", "https://verigence-web-dev.jbrconsulting-it.workers.dev").rstrip("/")
AUDIT_BASE = WEB_BASE + "/audit-core"
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT_SECONDS", "300"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

BOOKING_FIELDS: tuple[tuple[str, str, bool, str], ...] = (
    ("customer_name", "TEXT", True, "Customer full name exactly as printed on the Booking Form."),
    ("customer_phone", "TEXT", True, "Customer mobile or phone number exactly as printed on the Booking Form."),
    ("vehicle_model", "TEXT", True, "Vehicle model exactly as printed on the Booking Form."),
    ("vehicle_variant", "TEXT", True, "Vehicle variant exactly as printed on the Booking Form."),
    ("vehicle_color", "TEXT", True, "Vehicle colour or color exactly as printed on the Booking Form."),
)

RESULTS: list[str] = []
TENANT_ID = ""
SECURITY_BASE = ""
AUDIT_DATABASE_URL = ""
DI_DATABASE_URL = ""
HUMAN_TOKEN = ""
SUPERADMIN_ID = ""
JOURNEY_ID = ""
CUSTOMER_ID = ""
EVIDENCE_ID = ""


def record(key: str, value: Any = "PASS") -> None:
    line = f"{key}={value}"
    RESULTS.append(line)
    print(line, flush=True)


def normalize_db(value: str) -> str:
    result = value.strip()
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if result.startswith(prefix):
            result = "postgresql://" + result[len(prefix) :]
    return result


def public_base(values: dict[str, Any]) -> str:
    domain = str(values.get("RAILWAY_PUBLIC_DOMAIN") or values.get("RAILWAY_STATIC_URL") or "").strip()
    if not domain:
        raise RuntimeError("Railway public domain is missing")
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    return domain.rstrip("/")


def load_vars(path_env: str) -> dict[str, Any]:
    path = os.environ.get(path_env, "")
    if not path:
        raise RuntimeError(f"{path_env} is not configured")
    return json.loads(Path(path).read_text())


def uuid_value(label: str) -> uuid.UUID:
    namespace = uuid.UUID(TENANT_ID)
    return uuid.uuid5(namespace, "uc03-field-review-live:" + label)


def auth_headers(*, idempotency_key: str | None = None, version: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {HUMAN_TOKEN}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def check(response: httpx.Response, expected: set[int], label: str) -> dict[str, Any]:
    if response.status_code not in expected:
        raise RuntimeError(f"{label} failed: HTTP {response.status_code} {response.text[:1200]}")
    try:
        body = response.json()
        return body if isinstance(body, dict) else {"value": body}
    except Exception:
        return {}


def configure_security() -> None:
    global SECURITY_BASE, HUMAN_TOKEN, SUPERADMIN_ID, TENANT_ID
    values = load_vars("SECURITY_VARS_PATH")
    SECURITY_BASE = public_base(values)
    db = normalize_db(str(values.get("DATABASE_URL") or ""))
    if not db:
        raise RuntimeError("Security DATABASE_URL is missing")

    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.user_id::text
            FROM security.user_admin_role_assignments a
            JOIN security.users u ON u.user_id=a.user_id
            WHERE a.role_key='SuperAdmin' AND a.scope_type='PLATFORM'
              AND a.scope_id IS NULL AND a.status='ACTIVE' AND u.status='ACTIVE'
            ORDER BY a.assigned_at_utc LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("No active Security platform SuperAdmin is available for DEV test authorization")
        SUPERADMIN_ID = str(row[0])

    key = str(values.get("SECURITY_PRIVATE_KEY_PEM") or "").replace("\\n", "\n")
    kid = str(values.get("SECURITY_KEY_ID") or "").strip()
    issuer = str(values.get("SECURITY_TOKEN_ISSUER") or "verigence-security")
    audience = str(values.get("SECURITY_TOKEN_AUDIENCE") or "verigence-platform")
    if not key or not kid:
        raise RuntimeError("Security signing key configuration is unavailable to the DEV test runner")
    now = datetime.now(UTC)
    HUMAN_TOKEN = jwt.encode(
        {
            "iss": issuer,
            "sub": SUPERADMIN_ID,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "jti": str(uuid.uuid4()),
            "actor_type": "USER",
        },
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )

    tenant_name = "UC03 Field Review E2E " + now.strftime("%Y%m%d-%H%M%S")
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.post(
            SECURITY_BASE + "/security/v1/platform/tenants",
            headers={
                "Authorization": f"Bearer {HUMAN_TOKEN}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json={"tenantName": tenant_name},
        )
    body = check(response, {200, 201}, "Security test tenant create")
    TENANT_ID = str(body.get("tenantId") or "")
    uuid.UUID(TENANT_ID)
    record("UC03_LIVE_SECURITY_TENANT")
    record("UC03_LIVE_TEST_TENANT", TENANT_ID)


def seed_audit_fixture() -> None:
    global AUDIT_DATABASE_URL, JOURNEY_ID, CUSTOMER_ID
    values = load_vars("AUDIT_VARS_PATH")
    AUDIT_DATABASE_URL = normalize_db(str(values.get("DATABASE_URL") or ""))
    if not AUDIT_DATABASE_URL:
        raise RuntimeError("Audit Core DATABASE_URL is missing")

    suffix = TENANT_ID.replace("-", "")[:12]
    category_id = uuid_value("category")
    oem_id = uuid_value("oem")
    dealer_id = uuid_value("dealer")
    outlet_id = uuid_value("outlet")
    customer_id = uuid_value("customer")
    profile_id = uuid_value("doc-profile")
    profile_version_id = uuid_value("doc-profile-v1")
    journey_id = uuid_value("journey")
    assignment_id = uuid_value("business-assignment")

    with psycopg.connect(AUDIT_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
        cur.execute(
            "INSERT INTO auditcore.product_categories (product_category_id, category_code, category_name) VALUES (%s,%s,%s)",
            (category_id, f"E2E-CAT-{suffix}", "UC03 E2E Passenger Vehicle"),
        )
        cur.execute(
            "INSERT INTO auditcore.oems (oem_id, oem_code, oem_name) VALUES (%s,%s,%s)",
            (oem_id, f"E2E-OEM-{suffix}", "UC03 E2E Motors"),
        )
        cur.execute(
            """
            INSERT INTO auditcore.projects (
                tenant_id, project_code, project_name, oem_id, product_category_id,
                effective_start_date, timezone_name, project_status
            ) VALUES (%s,%s,%s,%s,%s,CURRENT_DATE-1,'Asia/Kolkata','ACTIVE')
            """,
            (TENANT_ID, f"UC03-E2E-{suffix}", "UC03 Field Review Live E2E", oem_id, category_id),
        )
        cur.execute(
            "INSERT INTO auditcore.dealers (tenant_id,dealer_id,dealer_code,dealer_name) VALUES (%s,%s,%s,%s)",
            (TENANT_ID, dealer_id, f"E2E-D-{suffix}", "UC03 E2E Dealer"),
        )
        cur.execute(
            """
            INSERT INTO auditcore.dealer_outlets (
                tenant_id,outlet_id,dealer_id,outlet_code,outlet_name
            ) VALUES (%s,%s,%s,%s,%s)
            """,
            (TENANT_ID, outlet_id, dealer_id, f"E2E-O-{suffix}", "UC03 E2E Outlet"),
        )
        cur.execute(
            """
            INSERT INTO auditcore.business_assignments (
                tenant_id,business_assignment_id,security_actor_id,business_role_code,dealer_id,outlet_id
            ) VALUES (%s,%s,%s,'PC',%s,%s)
            """,
            (TENANT_ID, assignment_id, SUPERADMIN_ID, dealer_id, outlet_id),
        )
        cur.execute(
            """
            INSERT INTO auditcore.customers (
                tenant_id,customer_id,dealer_id,outlet_id,customer_type_code,display_name
            ) VALUES (%s,%s,%s,%s,'INDIVIDUAL','Rajesh Kumar')
            """,
            (TENANT_ID, customer_id, dealer_id, outlet_id),
        )
        cur.execute(
            """
            INSERT INTO auditcore.document_requirement_profiles (
                tenant_id,document_requirement_profile_id,profile_code,profile_name
            ) VALUES (%s,%s,%s,'UC03 E2E Booking Documents')
            """,
            (TENANT_ID, profile_id, f"UC03-E2E-PROFILE-{suffix}"),
        )
        cur.execute(
            """
            INSERT INTO auditcore.document_requirement_profile_versions (
                tenant_id,document_requirement_profile_version_id,
                document_requirement_profile_id,version_no,lifecycle_status,effective_from
            ) VALUES (%s,%s,%s,1,'DRAFT',CURRENT_DATE-1)
            """,
            (TENANT_ID, profile_version_id, profile_id),
        )
        cur.execute(
            """
            INSERT INTO auditcore.document_requirement_items (
                tenant_id,document_requirement_profile_version_id,
                requirement_key,document_type_key,process_area,
                requirement_level,condition_config,sort_order
            ) VALUES (%s,%s,'BOOKING_FORM','booking_form','BOOKING','REQUIRED','{}'::jsonb,10)
            """,
            (TENANT_ID, profile_version_id),
        )
        cur.execute(
            """
            UPDATE auditcore.document_requirement_profile_versions
            SET lifecycle_status='PUBLISHED'
            WHERE tenant_id=%s AND document_requirement_profile_version_id=%s
            """,
            (TENANT_ID, profile_version_id),
        )
        cur.execute(
            """
            INSERT INTO auditcore.journeys (
                tenant_id,journey_id,dealer_id,outlet_id,customer_id,
                journey_reference,document_requirement_profile_version_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                TENANT_ID,
                journey_id,
                dealer_id,
                outlet_id,
                customer_id,
                f"UC03-E2E-J-{suffix}",
                profile_version_id,
            ),
        )
        conn.commit()

    JOURNEY_ID = str(journey_id)
    CUSTOMER_ID = str(customer_id)
    record("UC03_LIVE_AUDIT_FIXTURE")
    record("UC03_LIVE_JOURNEY", JOURNEY_ID)


def seed_di_profile() -> None:
    global DI_DATABASE_URL
    values = load_vars("DI_VARS_PATH")
    DI_DATABASE_URL = normalize_db(str(values.get("DI_DATABASE_URL") or values.get("DATABASE_URL") or ""))
    if not DI_DATABASE_URL:
        raise RuntimeError("DI database URL is missing")
    mock_value = str(values.get("DI_DOCAI_MOCK") or "").strip().lower()
    record("UC03_LIVE_DI_DOCAI_MOCK", mock_value or "unset")
    record("UC03_LIVE_DI_GEMINI_KEY_PRESENT", str(bool(str(values.get("DI_DOCAI_GEMINI_API_KEY") or "").strip())).lower())

    retention_id = uuid_value("retention")
    profile_id = uuid_value("di-profile")
    with psycopg.connect(DI_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
        cur.execute(
            """
            INSERT INTO docintel.tenant_settings (
                tenant_id,tenant_storage_key,timezone_name,eod_retry_local_time,
                eod_retry_enabled,classification_acceptance_score,
                subject_matching_min_confidence,upload_timeout_minutes,max_upload_bytes,
                allowed_mime_types,quality_policy,whatsapp_subject_reference_prefix,
                status,created_at_utc,updated_at_utc
            ) VALUES (%s,%s,'Asia/Kolkata',TIME '23:00:00',true,70.00,80.00,
                      30,31457280,'["application/pdf","image/jpeg","image/png","image/tiff"]'::jsonb,
                      '[]'::jsonb,'','ACTIVE',now(),now())
            """,
            (TENANT_ID, uuid_value("di-storage")),
        )
        cur.execute(
            """
            INSERT INTO docintel.retention_policies (
                tenant_id,retention_policy_id,policy_key,display_name,
                retention_days,disposition,status,created_at_utc,updated_at_utc
            ) VALUES (%s,%s,'uc03-e2e','UC03 Field Review E2E',30,'PURGE_CONTENT','ACTIVE',now(),now())
            """,
            (TENANT_ID, retention_id),
        )
        cur.execute(
            "UPDATE docintel.tenant_settings SET active_retention_policy_id=%s WHERE tenant_id=%s",
            (retention_id, TENANT_ID),
        )
        cur.execute(
            """
            SELECT document_type_id, COALESCE(category,'HANDWRITTEN')
            FROM docintel.document_types
            WHERE owner_tenant_id IS NULL AND document_type_key='booking_form' AND status='ACTIVE'
            """
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("DI global booking_form document type is missing")
        document_type_id, category = row
        cur.execute(
            """
            INSERT INTO docintel.tenant_document_types (
                tenant_id,document_type_id,physical_form_type,requires_processing,
                is_active,display_order,created_at_utc,updated_at_utc
            ) VALUES (%s,%s,%s,true,true,10,now(),now())
            """,
            (TENANT_ID, document_type_id, category),
        )
        cur.execute(
            """
            INSERT INTO docintel.extraction_profiles (
                profile_id,document_type_id,scope_tenant_id,version_no,profile_name,status,
                classification_hint,created_by_actor_id,published_by_actor_id,
                created_at_utc,published_at_utc,updated_at_utc
            ) VALUES (%s,%s,%s,1,'UC03 Field Review Live E2E','DRAFT','Booking Form',
                      'UC03_E2E',NULL,now(),NULL,now())
            """,
            (profile_id, document_type_id, TENANT_ID),
        )
        for sequence, (field_key, data_type, required, description) in enumerate(BOOKING_FIELDS, 1):
            field_id = uuid_value("field:" + field_key)
            cur.execute(
                """
                INSERT INTO docintel.canonical_fields (
                    canonical_field_id,owner_tenant_id,field_key,display_name,
                    data_type,description,status,created_at_utc,updated_at_utc
                ) VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',now(),now())
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
                    profile_field_id,profile_id,canonical_field_id,enabled,expected,
                    extraction_instruction,aliases,score_included,score_weight,
                    manual_correction_allowed,display_sequence,created_at_utc,updated_at_utc
                ) VALUES (%s,%s,%s,true,%s,%s,'[]'::jsonb,true,1.0,true,%s,now(),now())
                """,
                (uuid_value("profile-field:" + field_key), profile_id, field_id, required, description, sequence),
            )
        cur.execute(
            """
            UPDATE docintel.extraction_profiles
            SET status='PUBLISHED',published_by_actor_id='UC03_E2E',published_at_utc=now(),updated_at_utc=now()
            WHERE profile_id=%s
            """,
            (profile_id,),
        )
        conn.commit()
    record("UC03_LIVE_DI_PROFILE")


def booking_image() -> bytes:
    image = Image.new("RGB", (1400, 1000), "white")
    draw = ImageDraw.Draw(image)
    try:
        title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 54)
        body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 46)
        label = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
    except OSError:
        title = body = label = ImageFont.load_default()
    draw.text((420, 55), "BOOKING FORM", fill="black", font=title)
    rows = [
        ("Customer Name", "RAJESH KUMAR"),
        ("Customer Phone", "9876543210"),
        ("Vehicle Model", "XUV700"),
        ("Vehicle Variant", "AX7 L DIESEL AT"),
        ("Vehicle Color", "WHITE"),
    ]
    y = 190
    for left, right in rows:
        draw.text((100, y), left + ":", fill="black", font=label)
        draw.text((570, y), right, fill="black", font=body)
        draw.line((90, y + 66, 1310, y + 66), fill="black", width=2)
        y += 135
    draw.rectangle((65, 35, 1335, 930), outline="black", width=4)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def di_diagnostic() -> str:
    if not EVIDENCE_ID:
        return "no evidence"
    try:
        with psycopg.connect(AUDIT_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
            cur.execute(
                "SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=%s AND evidence_id=%s",
                (TENANT_ID, uuid.UUID(EVIDENCE_ID)),
            )
            row = cur.fetchone()
            doc_id = row[0] if row else None
        if doc_id is None:
            return "audit evidence has no DI document"
        with psycopg.connect(DI_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_ID,))
            cur.execute(
                """
                SELECT processing_status, confirmation_status, processing_failure_code
                FROM docintel.documents WHERE tenant_id=%s AND document_id=%s
                """,
                (TENANT_ID, doc_id),
            )
            row = cur.fetchone()
            if row is None:
                return "DI document row missing"
            cur.execute(
                "SELECT count(*) FROM docintel.extracted_facts WHERE tenant_id=%s AND document_id=%s",
                (TENANT_ID, doc_id),
            )
            fact_count = cur.fetchone()[0]
            return f"processing={row[0]} confirmation={row[1]} failure={row[2]} facts={fact_count}"
    except Exception as exc:
        return f"diagnostic-error={type(exc).__name__}:{exc}"


def live_flow() -> None:
    global EVIDENCE_ID
    di_values = load_vars("DI_VARS_PATH")
    di_base = public_base(di_values)
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        audit_health = client.get(AUDIT_BASE + "/health")
        check(audit_health, {200}, "Audit Core through Web proxy health")
        di_health = client.get(di_base + "/health/ready")
        check(di_health, {200}, "DI ready")
        record("UC03_LIVE_HEALTH")

        start = client.post(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/booking/start",
            headers=auth_headers(idempotency_key="uc03-e2e-start-" + uuid.uuid4().hex, version=0),
        )
        start_body = check(start, {200}, "Booking start")
        if int(start_body.get("aggregateVersion", -1)) != 1:
            raise RuntimeError(f"Booking start returned unexpected version: {start_body}")
        record("UC03_LIVE_AUTH")
        record("UC03_LIVE_BOOKING_START")

        docs = client.get(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/stages/BOOKING/documents",
            headers=auth_headers(),
        )
        if docs.status_code != 200:
            raise RuntimeError(f"Booking documents list failed: {docs.status_code} {docs.text[:1000]}")
        docs_body = docs.json()
        if not any(item.get("requirementKey") == "BOOKING_FORM" for item in docs_body):
            raise RuntimeError(f"BOOKING_FORM requirement missing after Booking start: {docs_body}")
        record("UC03_LIVE_REQUIREMENT_SNAPSHOT")

        image_bytes = booking_image()
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        upload = client.post(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/stages/BOOKING/documents/BOOKING_FORM/evidence",
            headers=auth_headers(idempotency_key="uc03-e2e-upload-" + uuid.uuid4().hex),
            files={"file": ("uc03-booking-form.png", image_bytes, "image/png")},
        )
        upload_body = check(upload, {201}, "Booking stage document upload")
        EVIDENCE_ID = str(upload_body.get("evidenceId") or "")
        uuid.UUID(EVIDENCE_ID)
        record("UC03_LIVE_STAGE_UPLOAD")
        record("UC03_LIVE_EVIDENCE", EVIDENCE_ID)

        deadline = time.time() + POLL_TIMEOUT
        workspace: dict[str, Any] = {}
        last_refresh = ""
        while time.time() < deadline:
            refresh = client.post(
                f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/booking/extraction/refresh",
                headers=auth_headers(),
            )
            last_refresh = f"HTTP {refresh.status_code} {refresh.text[:600]}"
            if refresh.status_code not in {200, 409, 422, 503}:
                raise RuntimeError("Extraction refresh unexpected response: " + last_refresh)
            get_ws = client.get(
                f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/uc03-workspace",
                headers=auth_headers(),
            )
            if get_ws.status_code == 200:
                workspace = get_ws.json()
                pending = [p for p in workspace.get("proposals", []) if p.get("status") == "PENDING"]
                if pending:
                    break
            time.sleep(POLL_SECONDS)
        else:
            raise RuntimeError(
                "Booking extraction produced no pending proposals before timeout; "
                + last_refresh
                + "; "
                + di_diagnostic()
            )

        proposals = [p for p in workspace.get("proposals", []) if p.get("status") == "PENDING"]
        if not proposals:
            raise RuntimeError("Workspace contains no pending extraction proposals")
        record("UC03_LIVE_EXTRACTION")
        record("UC03_LIVE_PROPOSAL_COUNT", len(proposals))

        by_field = {str(p.get("fieldKey")): p for p in proposals}
        if "customer_name" not in by_field or "customer_phone" not in by_field:
            raise RuntimeError(
                "Live extraction did not provide both customer_name and customer_phone required to test Accept + Change: "
                + json.dumps(sorted(by_field))
            )
        record("UC03_LIVE_CORE_FIELDS")

        localized = []
        for proposal in proposals:
            region = proposal.get("evidenceRegion")
            if not isinstance(region, dict):
                continue
            box = region.get("box")
            if not isinstance(box, list) or len(box) != 4:
                raise RuntimeError(f"Invalid evidenceRegion box: {region}")
            values = [float(v) for v in box]
            if any(v < 0 or v > 1000 for v in values) or values[2] <= values[0] or values[3] <= values[1]:
                raise RuntimeError(f"Out-of-range or inverted evidenceRegion box: {region}")
            localized.append(proposal)
        if localized:
            record("UC03_LIVE_LOCALIZATION", f"PASS:{len(localized)}")
        else:
            record("UC03_LIVE_LOCALIZATION", "FALLBACK_ONLY:NO_RELIABLE_BOX_RETURNED")

        content = client.get(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/evidence/{EVIDENCE_ID}/review-content",
            headers=auth_headers(),
        )
        if content.status_code != 200:
            raise RuntimeError(f"Review content failed: {content.status_code} {content.text[:600]}")
        content_type = content.headers.get("content-type", "")
        if "image/png" not in content_type.lower():
            raise RuntimeError(f"Review content type is not image/png: {content_type}")
        if hashlib.sha256(content.content).hexdigest() != image_hash:
            raise RuntimeError("Review content bytes do not match the uploaded source image")
        cache_control = content.headers.get("cache-control", "").lower()
        if "private" not in cache_control or "no-store" not in cache_control:
            raise RuntimeError(f"Review content cache policy is unsafe: {cache_control}")
        record("UC03_LIVE_REVIEW_CONTENT")

        current_version = int(workspace.get("aggregateVersion", -1))
        accept_proposal = by_field["customer_name"]
        accept = client.post(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/extraction-proposals/{accept_proposal['proposalId']}/accept",
            headers=auth_headers(idempotency_key="uc03-e2e-accept-" + uuid.uuid4().hex, version=current_version),
            json={"acceptedValue": None},
        )
        accept_body = check(accept, {200}, "Accept extraction proposal")
        current_version = int(accept_body.get("aggregateVersion", -1))
        record("UC03_LIVE_ACCEPT")

        correct_proposal = by_field["customer_phone"]
        corrected_value = "9876543211"
        correct = client.post(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/extraction-proposals/{correct_proposal['proposalId']}/correct",
            headers=auth_headers(idempotency_key="uc03-e2e-correct-" + uuid.uuid4().hex, version=current_version),
            json={"acceptedValue": corrected_value},
        )
        correct_body = check(correct, {200}, "Correct extraction proposal")
        current_version = int(correct_body.get("aggregateVersion", -1))
        record("UC03_LIVE_CORRECT")

        verify_ws_response = client.get(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/uc03-workspace",
            headers=auth_headers(),
        )
        verify_ws = check(verify_ws_response, {200}, "Reload Booking workspace")
        final_by_id = {p.get("proposalId"): p for p in verify_ws.get("proposals", [])}
        accepted = final_by_id.get(accept_proposal["proposalId"], {})
        corrected = final_by_id.get(correct_proposal["proposalId"], {})
        if accepted.get("status") != "ACCEPTED":
            raise RuntimeError(f"Accepted proposal did not persist ACCEPTED state: {accepted}")
        if corrected.get("status") != "CORRECTED" or str(corrected.get("acceptedValue")) != corrected_value:
            raise RuntimeError(f"Corrected proposal did not persist correction: {corrected}")
        if corrected.get("proposedValue") in (None, ""):
            raise RuntimeError("Corrected proposal lost the original machine proposed value")
        record("UC03_LIVE_DECISION_PERSISTENCE")

        current_version = int(verify_ws.get("aggregateVersion", current_version))
        assess = client.put(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/stages/BOOKING/documents/BOOKING_FORM",
            headers=auth_headers(idempotency_key="uc03-e2e-assess-" + uuid.uuid4().hex, version=current_version),
            json={"answer": "YES", "evidenceId": EVIDENCE_ID, "remarks": "Live UC03 field-review E2E"},
        )
        assess_body = check(assess, {200}, "Booking document assessment")
        if assess_body.get("requirementStatus") != "SATISFIED":
            raise RuntimeError(f"Booking document was not satisfied after linking evidence: {assess_body}")
        record("UC03_LIVE_DOCUMENT_SATISFIED")

        direct_workspace = client.get(
            f"{AUDIT_BASE}/v1/tenants/{TENANT_ID}/journeys/{JOURNEY_ID}/uc03-workspace",
            headers=auth_headers(),
        )
        check(direct_workspace, {200}, "Final workspace through deployed Web proxy")
        record("UC03_LIVE_WEB_PROXY")

    record("UC03_FIELD_REVIEW_LIVE_E2E")


def cleanup_security_tenant() -> None:
    if not TENANT_ID or not HUMAN_TOKEN or not SECURITY_BASE:
        return
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.delete(
                SECURITY_BASE + "/security/v1/platform/tenants/" + TENANT_ID,
                headers={"Authorization": f"Bearer {HUMAN_TOKEN}"},
            )
        record("UC03_LIVE_SECURITY_CLEANUP_HTTP", response.status_code)
    except Exception as exc:
        record("UC03_LIVE_SECURITY_CLEANUP", "WARN:" + type(exc).__name__)


def write_result(outcome: str, error: str | None = None) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# UC03 Field Review Live DEV E2E",
        "",
        f"- executed_at_utc: {datetime.now(UTC).isoformat()}",
        f"- audit_dev_source_sha: {os.environ.get('AUDIT_DEV_SOURCE_SHA', '')}",
        f"- web_base: {WEB_BASE}",
        f"- outcome: {outcome}",
    ]
    if error:
        safe = error.replace("\n", " ")[:2000]
        header.append(f"- error: {safe}")
    header += ["", "## Markers", ""] + [f"- {line}" for line in RESULTS]
    RESULT_PATH.write_text("\n".join(header) + "\n")


def main() -> None:
    outcome = "FAIL"
    try:
        configure_security()
        seed_audit_fixture()
        seed_di_profile()
        live_flow()
        outcome = "PASS"
        write_result(outcome)
    except Exception as exc:
        record("UC03_FIELD_REVIEW_LIVE_E2E", "FAIL")
        record("UC03_LIVE_DIAGNOSTIC", di_diagnostic())
        write_result(outcome, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        cleanup_security_tenant()
        if RESULT_PATH.exists():
            # Re-write once so cleanup status is also captured.
            current = RESULT_PATH.read_text().split("## Markers", 1)[0].rstrip()
            RESULT_PATH.write_text(current + "\n\n## Markers\n\n" + "\n".join(f"- {line}" for line in RESULTS) + "\n")


if __name__ == "__main__":
    main()
