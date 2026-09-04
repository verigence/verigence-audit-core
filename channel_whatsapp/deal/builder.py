from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from uuid import UUID
import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)
_FUZZY_THRESHOLD = 0.6
_DATE_WINDOW_DAYS = 3


@dataclass(frozen=True)
class DealMatch:
    deal_id: UUID
    booking_number: str
    model: str | None
    state: str
    is_exact: bool
    similarity_score: float | None


@dataclass(frozen=True)
class DealCreated:
    deal_id: UUID
    booking_number: str
    model: str | None
    customer_name: str
    is_provisional: bool


def find_existing_deal(connection, *, tenant_id, booking_number, customer_name, model, booking_date):
    """Exact match first, then trigram fuzzy match. Returns DealMatch or None."""
    row = connection.execute(
        text("SELECT id, booking_number, model, state FROM doc.deal"
             " WHERE tenant_id=:tid AND booking_number=:bn"),
        {"tid": tenant_id, "bn": booking_number}
    ).mappings().one_or_none()
    if row is not None:
        logger.info("deal_dedup_exact_hit", deal_id=str(row["id"]), booking_number=booking_number)
        return DealMatch(deal_id=UUID(str(row["id"])), booking_number=row["booking_number"],
                         model=row.get("model"), state=row["state"], is_exact=True, similarity_score=None)
    if not customer_name or model is None:
        return None
    params = {"tid": tenant_id, "model": model, "cust": customer_name, "threshold": _FUZZY_THRESHOLD}
    date_clause = ""
    if booking_date is not None:
        date_clause = "AND booking_date BETWEEN :bd - :window AND :bd + :window"
        params["bd"] = booking_date
        params["window"] = _DATE_WINDOW_DAYS
    row = connection.execute(
        text(f"SELECT id, booking_number, model, state, similarity(customer_name, :cust) AS sim"
             f" FROM doc.deal WHERE tenant_id=:tid AND model=:model AND customer_name % :cust"
             f" {date_clause} ORDER BY sim DESC LIMIT 1"),
        params
    ).mappings().one_or_none()
    if row is None or row["sim"] < _FUZZY_THRESHOLD:
        return None
    logger.info("deal_dedup_fuzzy_hit", deal_id=str(row["id"]), similarity=round(float(row["sim"]), 3))
    return DealMatch(deal_id=UUID(str(row["id"])), booking_number=row["booking_number"],
                     model=row.get("model"), state=row["state"], is_exact=False,
                     similarity_score=float(row["sim"]))


def create_provisional_deal(connection, *, tenant_id, org_unit_id, booking_number,
                             customer_name, customer_mobile, model, variant, booking_date,
                             deal_type, is_financed, has_exchange, is_corporate, created_by):
    row = connection.execute(
        text("""INSERT INTO doc.deal (tenant_id, org_unit_id, booking_number, customer_name,
                    customer_mobile, model, variant, booking_date, deal_type, is_financed,
                    has_exchange, is_corporate, state, created_from, created_by)
               VALUES (:tenant_id, :org_unit_id, :booking_number, :customer_name,
                       :customer_mobile, :model, :variant, :booking_date, :deal_type,
                       :is_financed, :has_exchange, :is_corporate,
                       'provisional', 'whatsapp', :created_by)
               ON CONFLICT (tenant_id, booking_number) DO NOTHING
               RETURNING id, booking_number, model, customer_name"""),
        {"tenant_id": tenant_id, "org_unit_id": org_unit_id, "booking_number": booking_number,
         "customer_name": customer_name, "customer_mobile": customer_mobile, "model": model,
         "variant": variant, "booking_date": booking_date, "deal_type": deal_type,
         "is_financed": is_financed, "has_exchange": has_exchange,
         "is_corporate": is_corporate, "created_by": created_by}
    ).mappings().one_or_none()
    if row is None:
        existing = connection.execute(
            text("SELECT id, booking_number, model, customer_name FROM doc.deal"
                 " WHERE tenant_id=:tid AND booking_number=:bn"),
            {"tid": tenant_id, "bn": booking_number}
        ).mappings().one()
        return DealCreated(deal_id=UUID(str(existing["id"])), booking_number=existing["booking_number"],
                           model=existing.get("model"), customer_name=existing["customer_name"],
                           is_provisional=True)
    logger.info("deal_provisional_created", deal_id=str(row["id"]), booking_number=booking_number)
    return DealCreated(deal_id=UUID(str(row["id"])), booking_number=row["booking_number"],
                       model=row.get("model"), customer_name=row["customer_name"], is_provisional=True)


def confirm_deal(connection, *, tenant_id, deal_id, confirmed_by):
    connection.execute(
        text("""UPDATE doc.deal SET state='confirmed', confirmed_by=:confirmed_by,
                    confirmed_at=now(), updated_at=now()
               WHERE tenant_id=:tid AND id=:deal_id AND state='provisional'"""),
        {"tid": tenant_id, "deal_id": deal_id, "confirmed_by": confirmed_by}
    )
    logger.info("deal_confirmed", deal_id=str(deal_id))


def raise_ambiguous_review(connection, *, tenant_id, match, detail):
    connection.execute(
        text("INSERT INTO doc.review_task (tenant_id, deal_id, reason, detail, state)"
             " VALUES (:tid, :deal_id, 'deal_ambiguous', :detail, 'open') ON CONFLICT DO NOTHING"),
        {"tid": tenant_id, "deal_id": match.deal_id, "detail": detail}
    )
    logger.warning("deal_ambiguous_flagged", deal_id=str(match.deal_id), similarity=match.similarity_score)
