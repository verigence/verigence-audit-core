"""Where did a PC's Drive files go? Read only (product owner 2026-11-24: Aditya, Cuttack, put 15
files in Drive on 23 Nov and the results show 13, with nothing in failures).

For every PC whose name matches, and the outlets they are mapped to:
  1. What Google Drive itself holds in those outlets' folders (every customer folder, every
     sub-folder), for files created in the window -- the truth of what was uploaded.
  2. What Verigence recorded for each of those files (p2_drive_files) and what became of it
     (bulk file outcome, journey, upload batch, pages).
  3. Any Drive file Verigence never recorded, and any recorded file whose journey shows nothing.

Nothing is written: the database session is READ ONLY and only Drive reads are made.

    python tools/drive_files_diagnose.py --name aditya --since 2026-11-22 --until 2026-11-25
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import create_engine, text

from audit_core import google_drive
from audit_core.db import set_tenant_context
from audit_core.google_drive import FOLDER_TYPE, DriveClient
from audit_core.uc03_p2_object_keys import IST


def _ro(engine, tenant_id: str | None = None):
    connection = engine.connect()
    connection.execute(text("SET TRANSACTION READ ONLY"))
    if tenant_id:
        set_tenant_context(connection, tenant_id)
    return connection


def _print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n--- {title} ({len(rows)})")
    for r in rows:
        print("   ", {k: (str(v) if v is not None else None) for k, v in r.items()})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--since", required=True, type=date.fromisoformat)
    p.add_argument("--until", required=True, type=date.fromisoformat)
    a = p.parse_args()
    start = datetime.combine(a.since, time(0), IST).astimezone(UTC)
    end = datetime.combine(a.until, time(0), IST).astimezone(UTC)
    engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})

    from audit_core.db import set_platform_super_admin_context

    with _ro(engine) as c:
        set_platform_super_admin_context(c)
        projects = [dict(r) for r in c.execute(text(
            "SELECT tenant_id, project_name FROM auditcore.projects WHERE project_status='ACTIVE'")).mappings()]
    tenants = []
    for proj in projects:
        with _ro(engine, proj["tenant_id"]) as c:
            row = c.execute(text(
                "SELECT connection_status, root_folder_id, last_synced_at_utc, last_error "
                "FROM auditcore.p2_drive_connections WHERE tenant_id=:t"), {"t": proj["tenant_id"]}).mappings().one_or_none()
        if row:
            tenants.append({**proj, **dict(row)})
    _print_rows("Drive-connected Projects", tenants)
    settings = google_drive.drive_settings()
    print("drive settings present:", settings is not None)

    for t in tenants:
        tid = t["tenant_id"]
        with _ro(engine, tid) as c:
            people = [dict(r) for r in c.execute(text(
                "SELECT DISTINCT pn.actor_id, pn.display_name FROM auditcore.people_names pn "
                "JOIN auditcore.business_assignments ba ON ba.security_actor_id=pn.actor_id AND ba.tenant_id=:t "
                "WHERE pn.display_name ILIKE :n"), {"t": tid, "n": f"%{a.name}%"}).mappings()]
            if not people:
                continue
            print(f"\n================ Project {t['project_name']} ({tid})")
            _print_rows("PCs matching", people)
            actors = [p["actor_id"] for p in people]
            _print_rows("Their assignments", [dict(r) for r in c.execute(text(
                "SELECT ba.security_actor_id, ba.business_role_code, ba.dealer_id, ba.outlet_id, o.outlet_name, "
                "ba.assignment_status, ba.effective_from, ba.effective_to FROM auditcore.business_assignments ba "
                "LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id=ba.tenant_id AND o.outlet_id=ba.outlet_id "
                "WHERE ba.tenant_id=:t AND ba.security_actor_id = ANY(:a)"), {"t": tid, "a": actors}).mappings()])
            _print_rows("Their Google accounts (p2_drive_people)", [dict(r) for r in c.execute(text(
                "SELECT * FROM auditcore.p2_drive_people WHERE tenant_id=:t AND actor_id = ANY(:a)"),
                {"t": tid, "a": actors}).mappings()])
            # Files recorded for the PC, or in the outlets they are mapped to, in the window.
            files = [dict(r) for r in c.execute(text(
                """
                SELECT f.file_row_id, f.drive_file_id, f.content_md5, f.file_name, f.mime_type, f.size_bytes,
                       f.lane, f.file_status, f.detail, f.upload_date, f.drive_created_at_utc, f.seen_at_utc,
                       f.taken_at_utc, f.pc_actor_id, f.outlet_id, o.outlet_name, f.parent_folder_id,
                       f.customer_folder_id, cf.folder_name AS customer_folder, cf.booking_number, f.run_id,
                       f.bulk_id, f.bulk_file_id, f.journey_id AS drive_journey,
                       b.outcome, b.detail AS bulk_detail, b.journey_id AS bulk_journey, b.placed_journey_id,
                       b.placed_reference, b.group_key, b.batch_id, b.sha256 AS bulk_sha,
                       ub.batch_status, ub.page_count, ub.failure_reason,
                       u.status AS bulk_status, u.submitted_at_utc
                FROM auditcore.p2_drive_files f
                LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id=f.tenant_id AND o.outlet_id=f.outlet_id
                LEFT JOIN auditcore.p2_drive_folders cf ON cf.tenant_id=f.tenant_id AND cf.drive_folder_id=f.customer_folder_id
                LEFT JOIN auditcore.p2_bulk_files b ON b.tenant_id=f.tenant_id AND b.file_id=f.bulk_file_id
                LEFT JOIN auditcore.p2_bulk_uploads u ON u.tenant_id=b.tenant_id AND u.bulk_id=b.bulk_id
                LEFT JOIN auditcore.p2_upload_batches ub ON ub.tenant_id=b.tenant_id AND ub.batch_id=b.batch_id
                WHERE f.tenant_id=:t
                  AND (f.pc_actor_id = ANY(:a)
                       OR f.outlet_id IN (SELECT outlet_id FROM auditcore.business_assignments
                                          WHERE tenant_id=:t AND security_actor_id = ANY(:a) AND outlet_id IS NOT NULL))
                  AND (f.seen_at_utc >= :s AND f.seen_at_utc < :e
                       OR f.drive_created_at_utc >= :s AND f.drive_created_at_utc < :e
                       OR f.upload_date BETWEEN :ds AND :de)
                ORDER BY f.outlet_id, cf.folder_name, f.drive_created_at_utc
                """), {"t": tid, "a": actors, "s": start, "e": end, "ds": a.since, "de": a.until}).mappings()]
            print(f"\n--- Recorded Drive files in window ({len(files)})")
            for f in files:
                print(f"  [{f['outlet_name']}] {f['customer_folder']!r} / {f['file_name']!r} lane={f['lane']} "
                      f"status={f['file_status']} outcome={f['outcome']} day={f['upload_date']} "
                      f"created={f['drive_created_at_utc']} seen={f['seen_at_utc']} taken={f['taken_at_utc']} "
                      f"pc={str(f['pc_actor_id'])[:8]} md5={f['content_md5'][:8]} "
                      f"journey={f['placed_journey_id'] or f['bulk_journey'] or f['drive_journey']} "
                      f"ref={f['placed_reference']} batch={f['batch_status']}/{f['page_count']} "
                      f"bulk={f['bulk_status']} detail={f['detail']!r} bulk_detail={f['bulk_detail']!r} "
                      f"fail={f['failure_reason']!r}")
            outlets = sorted({str(f["outlet_id"]) for f in files if f["outlet_id"]} | {
                str(r[0]) for r in c.execute(text(
                    "SELECT outlet_id FROM auditcore.business_assignments WHERE tenant_id=:t "
                    "AND security_actor_id = ANY(:a) AND outlet_id IS NOT NULL"), {"t": tid, "a": actors})})
            folders = [dict(r) for r in c.execute(text(
                "SELECT drive_folder_id, folder_kind, folder_name, outlet_id, gone_at_utc, archived_at_utc "
                "FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND folder_kind IN ('OUTLET','PC') "
                "AND outlet_id = ANY(CAST(:o AS uuid[]))"), {"t": tid, "o": outlets}).mappings()]
            _print_rows("Outlet / PC folders", folders)
            recorded = {}
            for r in c.execute(text("SELECT drive_file_id, file_status, file_name, content_md5 FROM "
                                    "auditcore.p2_drive_files WHERE tenant_id=:t"), {"t": tid}).mappings():
                recorded.setdefault(r["drive_file_id"], []).append(dict(r))
            _print_rows("Drive runs in window", [dict(r) for r in c.execute(text(
                "SELECT run_id, run_trigger, run_status, outlet_id, dealer_id, upload_date, requested_at_utc, "
                "started_at_utc, finished_at_utc, summary FROM auditcore.p2_drive_runs WHERE tenant_id=:t "
                "AND requested_at_utc >= :s AND requested_at_utc < :e ORDER BY requested_at_utc"),
                {"t": tid, "s": start, "e": end}).mappings()])
            row = c.execute(text("SELECT * FROM auditcore.p2_drive_connections WHERE tenant_id=:t"),
                            {"t": tid}).mappings().one_or_none()

        if settings is None or row is None or row["connection_status"] != "CONNECTED":
            print("Drive not readable here; skipping the Drive side.")
            continue
        client = DriveClient(settings, google_drive.unseal(settings, row["refresh_token_sealed"]))
        print("\n--- What Google Drive holds (files created in window), per outlet folder")
        for folder in folders:
            if folder["gone_at_utc"]:
                continue
            pending = [(folder["drive_folder_id"], folder["folder_name"])]
            while pending:
                fid, path = pending.pop()
                for meta in client.children(fid):
                    name = f"{path}/{meta.get('name')}"
                    if meta.get("mimeType") == FOLDER_TYPE:
                        pending.append((meta["id"], name))
                        continue
                    created = google_drive_time(meta.get("createdTime"))
                    modified = google_drive_time(meta.get("modifiedTime"))
                    if not ((created and start <= created < end) or (modified and start <= modified < end)):
                        continue
                    rec = recorded.get(meta["id"])
                    owner = ((meta.get("owners") or [{}])[0] or {}).get("emailAddress")
                    mark = "RECORDED" if rec else "*** NOT RECORDED ***"
                    print(f"  {mark} {name!r} mime={meta.get('mimeType')} size={meta.get('size')} "
                          f"created={created} modified={modified} owner={owner} md5={str(meta.get('md5Checksum'))[:8]} "
                          f"id={meta['id']} rec={[(x['file_status'], x['content_md5'][:8]) for x in rec or []]}")
        client.close()


def google_drive_time(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


if __name__ == "__main__":
    main()
