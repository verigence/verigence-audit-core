# channel.whatsapp

WhatsApp evidence capture channel for **Verigence Audit Core**.

Delivers the full booking → delivery document submission cycle over WhatsApp for Process Consultants on dealership floors.

---

## Architecture decision D-01

**WhatsApp is an Audit Core channel, not a DI channel.**

Every useful inbound message requires Journey, Requirement Profile and Dealer/Outlet assignment — all of which live in Audit Core. The channel is therefore a bounded module inside the Audit Core modular monolith.

---

## Module layout

```
channel_whatsapp/
  wa/webhook.py          HMAC-SHA256 verify, inbox write, 200-OK
  wa/webhook_api.py      FastAPI GET verify + POST receive
  wa/router.py           phone→contact→user→tenant→scope (2-dim auth)
  wa/session.py          bundle state machine, 90s debounce, SKIP LOCKED
  wa/client.py           Meta Cloud API send adapter
  wa/outbox.py           24h window-aware reliable reply delivery
  wa/copy/en,hi,pa.yaml  localised message templates (zero PII)
  media/fetch.py         streaming download, dual SHA-256, Meta-aware retry
  media/redact.py        Aadhaar digit mask + QR blank (governance gate D-08)
  media/store.py         Evidence Service adapter
  deal/builder.py        cold-start deal, exact+fuzzy dedupe
  deal/checklist.py      deal-type aware requirement checklist
  migrations/011.sql     wa schema, all tables, RLS policies, indexes
  config.py              pydantic-settings, validated at boot
  worker.py              Procrastinate task definitions
  tests/fake_meta/       CI fake Meta Cloud API server
  tests/test_whatsapp_cycle.py  e2e tests
```

---

## Booking cycle (cold start)

1. PC sends booking form via WhatsApp
2. Webhook → inbox write → identity resolved → session created → file registered
3. 90s debounce fires → session → PROCESSING
4. Evidence Service submits file to DI
5. DI classifies as BOOKING_FORM, extracts booking_number / customer / model
6. Deal builder: exact dedupe → no match → PROVISIONAL deal created
7. PC receives interactive button: *"Booking BK-12345 / Swift — correct?"*
8. PC taps ✅ → deal CONFIRMED → checklist initialised
9. Gap message sent: *"Received: Booking form. Still needed: PAN, Aadhaar, Tax Invoice…"*

## Delivery cycle

1. PC sends remaining documents in subsequent sessions
2. Each file: download → SHA-256 verify → redact (if Aadhaar) → store via Evidence Service
3. Checklist updated after each stored file
4. After each session flush: gap message refreshed
5. All blocking items satisfied → COMPLETE state, final confirmation sent

---

## Governance gate (D-08)

> **No real customer document is captured until Aadhaar redaction is verified.**

`WA_REDACTION_ENABLED=false` (default) parks sessions that would submit Aadhaar documents.
Set `WA_REDACTION_ENABLED=true` only after the redactor has been end-to-end verified.

---

## Required env vars

| Variable | Description |
|---|---|
| `WA_APP_SECRET` | Meta app secret for X-Hub-Signature-256 |
| `WA_ACCESS_TOKEN` | Meta Cloud API permanent access token |
| `WA_VERIFY_TOKEN` | Webhook verify token |
| `EVIDENCE_SERVICE_BASE_URL` | Internal Evidence Service base URL |
| `WA_REDACTION_ENABLED` | `true` after D-08 verified (default: false) |

---

## Wiring into Audit Core

Add to `src/audit_core/main.py`:

```python
# imports
from channel_whatsapp.wa.webhook_api import router as whatsapp_router
from channel_whatsapp.config import get_wa_settings

# inside _lifespan startup
get_wa_settings()  # validates secrets at boot

# inside create_app()
application.include_router(whatsapp_router)
```

Apply migration `channel_whatsapp/migrations/011_channel_whatsapp.sql` before deploying.

---

## Tests

```bash
cd channel_whatsapp
pip install -e ".[test]"
pytest tests/ -v
```
