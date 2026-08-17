# Verigence Platform Document Processing Design Notes

**Scope:** Audit Core ↔ DI ↔ Object Storage ↔ Security  
**Status:** Architecture/design working note — **not implementation approval**  
**Purpose:** Capture current document-processing behavior, latency observations, and candidate design improvements before implementation decisions are made.

---

## 1. Architectural ownership boundaries

The following ownership model is the default design principle:

- **Business evidence lifecycle** → Audit Core
- **Document intelligence lifecycle** → DI
- **Binary persistence** → Object Storage
- **Identity / authentication / authorization** → Security

A processing optimization must preserve these ownership boundaries unless a separate architecture decision explicitly changes them.

---

## 2. Current DI intake and extraction flow

Current DI behavior on the `dev` implementation is asynchronous and worker-based.

```text
Client / Audit Core
        │
        │ POST document
        ▼
      DI API
        │
        ├─ create document row (RECEIVING)
        ├─ stream/read bytes and calculate SHA-256
        ├─ detect MIME / integrity checks
        ▼
   Object Storage (R2)
        │
        │ storage write completes
        ▼
      DI API
        │
        ├─ persist ORIGINAL artifact metadata
        ├─ run upload quality validation
        ├─ set upload status FIT / NOT_FIT / CORRUPT
        ├─ if FIT + requires_processing=true:
        │      create INITIAL processing job = PENDING
        └─ commit transaction
               │
               ▼
          DI API returns documentId

Standalone DI Worker
        │
        ├─ polls processing_jobs
        │   default interval: 5 seconds when idle
        ├─ claim PENDING job using FOR UPDATE SKIP LOCKED
        ├─ set document PROCESSING
        ├─ build classification candidate set
        ├─ reload ORIGINAL artifact from Object Storage
        ├─ classify document through configured DocumentAI adapter
        ├─ resolve extraction profile
        ├─ extract fields through configured DocumentAI adapter / Gemini
        ├─ normalize
        ├─ validate
        ├─ calculate confidence / human verification status
        ├─ update search index
        └─ set document PROCESSED + CONFIRMED
```

### Current key observation

The extractor is **not** called immediately after the R2 write. A successful storage write is followed by artifact persistence, quality validation, eligibility checks, processing-job creation and transaction commit. The worker then discovers the durable job by polling PostgreSQL.

This separation is intentional and should be preserved. It prevents Gemini/Document AI from running against corrupt, rejected, unsupported, or `requires_processing=false` documents.

---

## 3. Latency observation

The worker uses a configurable polling interval with a current default of 5 seconds.

Therefore the queue-pickup delay after the processing-job transaction commits is approximately:

```text
best case:   ~0 seconds
worst case:  ~5 seconds
```

This is avoidable startup latency, but it should **not** be assumed to represent the full extraction latency. Classification, provider inference, extraction, storage reads, normalization, validation and persistence each have their own contribution.

Before any performance change is approved, measure at least these timestamps:

```text
T1  storage_written
T2  processing_job_committed
T3  worker_job_claimed
T4  classification_started
T5  classification_completed
T6  extraction_started
T7  extraction_completed
T8  document_confirmed
```

Derived measures:

```text
Job pickup latency      = T3 - T2
Classification latency  = T5 - T4
Extraction latency      = T7 - T6
Post-processing latency = T8 - T7
End-to-end DI latency   = T8 - T1
```

This instrumentation is preferred over inferring provider latency from an external E2E wait window.

---

## 4. Candidate DI-PERF-001 — immediate worker wake-up

**Status:** REVIEW / not approved for implementation

### Objective

Begin DI worker processing as soon as a durable, eligible processing job has been committed, without moving Gemini execution into the HTTP upload request and without introducing a new enterprise message broker for this optimization alone.

### Candidate design

Use PostgreSQL `LISTEN/NOTIFY` as a **wake-up signal**, while keeping the existing `processing_jobs` table as the durable source of truth.

```text
DI API
   │
   ├─ persist R2 artifact
   ├─ quality = FIT
   ├─ requires_processing = true
   ├─ INSERT processing_job = PENDING
   └─ COMMIT
          │
          │ NOTIFY di_processing_ready
          ▼
      PostgreSQL
          │
          ▼
       DI Worker
          │
          ├─ wake immediately
          ├─ SELECT next PENDING job
          ├─ FOR UPDATE SKIP LOCKED
          └─ run existing processing pipeline
                    │
                    ▼
                  Gemini
```

### Critical design rule

`NOTIFY` must **not** become the work queue or source of truth.

The notification means only:

> Work may be available; wake up and check the durable job table.

The worker must continue to claim work using the existing database contract.

### Recovery path

Periodic polling remains enabled as a fallback.

```text
Fast path:
processing_job commit → NOTIFY → worker wakes immediately

Recovery path:
missed notification / worker restart → periodic poll → claim durable PENDING job
```

This protects against lost transient notifications while preserving current recovery semantics.

---

## 5. Explicit non-goals for DI-PERF-001

The following are **not** part of this candidate:

- Do not call Gemini directly from `intake_document()`.
- Do not make document upload wait for extraction to finish.
- Do not bypass the quality gate.
- Do not process `NOT_FIT`, `CORRUPT`, unsupported, or `requires_processing=false` documents.
- Do not replace `processing_jobs` with PostgreSQL notifications.
- Do not change Gemini classification/extraction behavior.
- Do not change normalization, validation, confidence scoring, human-verification logic, retry/backout, or EOD retry behavior.
- Do not introduce Kafka/RabbitMQ/Event Streams solely for this small optimization.

---

## 6. Why direct extraction immediately after R2 write is not recommended

A literal design of:

```text
R2 write → Gemini extract
```

would start intelligence processing before DI has completed its own eligibility controls.

The correct logical trigger point is:

```text
R2 write complete
   ↓
artifact metadata persisted
   ↓
quality gate passed
   ↓
requires_processing = true
   ↓
durable processing job committed
   ↓
worker starts immediately
```

This retains the existing API/worker architecture while reducing avoidable queue-pickup delay.

---

## 7. Relationship to future presigned-upload architecture

A separate architecture candidate is under discussion for direct client upload to object storage using a presigned URL. That design is **not approved or implemented** here.

The current DI worker architecture is compatible with that future direction because the worker already reloads the ORIGINAL artifact from object storage before classification/extraction.

Potential future shape:

```text
Client
   │
   │ presigned upload
   ▼
Object Storage
   │
   ▼
Document registered / processing authorized
   │
   ▼
DI durable processing job
   │
   ▼
DI Worker → Object Storage → Gemini
```

The future upload mechanism should not alter DI's ownership of the document-intelligence lifecycle.

---

## 8. Enterprise evolution path

`LISTEN/NOTIFY` is an optimization candidate for the current DI deployment, not the final enterprise integration architecture.

If DI later needs to process high volumes for multiple independent enterprise consumers, durable messaging/event infrastructure can be evaluated separately for commands and lifecycle events.

The current candidate deliberately keeps that future decision open:

```text
Today / near term:
PostgreSQL durable job + immediate wake-up + polling fallback

Future enterprise option:
Durable work queue / event backbone + scalable DI worker consumers
```

No broker technology is selected by this note.

---

## 9. Decision gates before implementation

Before **DI-PERF-001** moves from REVIEW to APPROVED:

1. Measure the current timing breakdown from storage write to document confirmation.
2. Confirm actual worker poll configuration in the target runtime.
3. Quantify how much latency is attributable to job pickup versus Gemini/provider processing.
4. Validate PostgreSQL connection behavior for a long-lived `LISTEN` connection in the target deployment.
5. Define reconnect behavior if the listener connection drops.
6. Verify multiple worker instances wake safely and still rely on `FOR UPDATE SKIP LOCKED` for single-job ownership.
7. Add tests proving a missed notification cannot strand a durable PENDING job.
8. Keep current polling fallback enabled.
9. Run the existing DI document/Gemini E2E unchanged after implementation.

---

## 10. Related remediation register items

- **DI-PERF-001** — immediate worker wake-up after durable processing-job commit.
- **PLAT-OBS-001** — cross-module trace/correlation and stage-level latency instrumentation.
- **PLAT-CONTRACT-001** — Audit Core ↔ DI API-contract compatibility.

See `docs/PLATFORM_REMEDIATION_REGISTER.md` for status and implementation tracking.

---

## 11. Change log

| Date | Change |
|---|---|
| 2026-08-17 | Initial document-processing design note created. Captured current DI intake/worker flow, processing latency measurement points, and DI-PERF-001 PostgreSQL `LISTEN/NOTIFY` wake-up candidate. No application code changed. |
