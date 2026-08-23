from pathlib import Path

import yaml

OPENAPI = Path("api/openapi-v1.yaml")
SENTINEL = "  # UC03_C1_BOOKING_CONTRACT\n"
MARKER = "  /tenants/{tenantId}/reference/product-skus:\n"

BLOCK = r'''  # UC03_C1_BOOKING_CONTRACT
  /tenants/{tenantId}/journeys/{journeyId}/booking/start:
    post:
      operationId: startUc03Booking
      description: Starts the Booking audit stage using idempotent optimistic concurrency.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      responses:
        '200': {description: Booking started or idempotently replayed, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/booking/close-ready:
    post:
      operationId: closeUc03BookingReady
      description: Completes Booking normally only when the versioned Booking completion gate has no blockers. Open non-blocking audit flags do not stop dealer business operations.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      responses:
        '200': {description: Booking checkpoint completed, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/booking/close-no-delivery:
    post:
      operationId: closeUc03BookingNoDelivery
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [closeReasonCode]
              properties:
                closeReasonCode: {type: string, minLength: 1, maxLength: 100}
                remarks: {type: string, maxLength: 4000}
      responses:
        '200': {description: Booking closed with no-delivery disposition, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/booking/cancel:
    post:
      operationId: cancelUc03Booking
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [closeReasonCode]
              properties:
                closeReasonCode: {type: string, minLength: 1, maxLength: 100}
                remarks: {type: string, maxLength: 4000}
      responses:
        '200': {description: Booking cancelled with configured reason, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/booking/mark-duplicate:
    post:
      operationId: markUc03BookingDuplicate
      description: Closes the Booking as duplicate and creates the mandatory HIGH duplicate finding with append-only history.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        content:
          application/json:
            schema: {type: object, properties: {remarks: {type: string, maxLength: 4000}}}
      responses:
        '200': {description: Duplicate Booking conclusion recorded, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/capture/{fieldKey}:
    put:
      operationId: recordUc03BookingCapture
      description: Records approved typed-domain Booking capture; UC03 does not create a generic 123-field business-data store.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: fieldKey, in: path, required: true, schema: {type: string}}
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [value]
              properties:
                value: {}
                sourceEvidenceId: {type: string, format: uuid}
      responses:
        '200': {description: Typed Booking capture recorded, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/extraction-proposals/{proposalId}/accept:
    post:
      operationId: acceptUc03BookingExtractionProposal
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: proposalId, in: path, required: true, schema: {type: string, format: uuid}}
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        content:
          application/json:
            schema: {type: object, properties: {acceptedValue: {}}}
      responses:
        '200': {description: Proposal accepted into its approved typed domain with provenance retained, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/extraction-proposals/{proposalId}/correct:
    post:
      operationId: correctUc03BookingExtractionProposal
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: proposalId, in: path, required: true, schema: {type: string, format: uuid}}
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        required: true
        content:
          application/json:
            schema: {type: object, required: [acceptedValue], properties: {acceptedValue: {}}}
      responses:
        '200': {description: Human correction recorded while immutable machine original/source version remain provenance, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/booking/extraction/refresh:
    post:
      operationId: refreshUc03BookingExtraction
      description: Refreshes processing and publishes proposals only for DI mappings reconciled as SUPPORTED for C1; PROVISIONAL/TBD mappings remain excluded.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
      responses:
        '200': {description: Processing and proposal refresh result, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/processing-status:
    get:
      operationId: getUc03BookingProcessingStatus
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
      responses:
        '200': {description: Progressive Booking document processing state, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/flags:
    get:
      operationId: listUc03BookingFlags
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
      responses:
        '200': {description: Booking audit flags, content: {application/json: {schema: {type: array, items: {type: object, additionalProperties: true}}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
    post:
      operationId: createUc03BookingHumanFlag
      description: Creates a PC-observed audit flag. Completion blocking is explicit and is not implied merely by an open finding.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [category, severity, summary]
              properties:
                category: {type: string, maxLength: 100}
                severity: {type: string, enum: [INFO, LOW, MEDIUM, HIGH, CRITICAL]}
                summary: {type: string, maxLength: 500}
                remarks: {type: string, maxLength: 4000}
                evidenceIds: {type: array, maxItems: 20, items: {type: string, format: uuid}}
      responses:
        '200': {description: Human audit flag recorded, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/uc03-workspace:
    get:
      operationId: getUc03BookingWorkspace
      description: Returns the Web/Android Booking workspace including typed capture, document checklist/processing, proposals, flags and completion blockers.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
      responses:
        '200': {description: Booking workspace, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/stages/BOOKING/documents/{requirementKey}/evidence:
    post:
      operationId: uploadUc03BookingDocument
      description: Human-token Audit Core facade for Booking evidence. Audit Core manages DI subject/context linkage and idempotent processing; Web/Android never calls DI directly.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: requirementKey, in: path, required: true, schema: {type: string}}
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema: {type: object, required: [file], properties: {file: {type: string, format: binary}}}
      responses:
        '201': {description: Booking evidence accepted and linked, content: {application/json: {schema: {$ref: '#/components/schemas/Evidence'}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/stages/{stageCode}/documents:
    get:
      operationId: listUc03BookingDocumentAssessments
      description: Lists versioned Booking requirements and effective applicability/assessment state. C1 accepts only BOOKING.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - {name: stageCode, in: path, required: true, schema: {type: string, enum: [BOOKING]}}
      responses:
        '200': {description: Booking document assessments, content: {application/json: {schema: {type: array, items: {type: object, additionalProperties: true}}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
  /tenants/{tenantId}/journeys/{journeyId}/stages/{stageCode}/documents/{requirementKey}:
    put:
      operationId: recordUc03BookingDocumentAssessment
      description: Records YES, NO, NA or UNANSWERED after dynamic applicability is resolved.
      parameters:
        - $ref: '#/components/parameters/TenantId'
        - $ref: '#/components/parameters/JourneyId'
        - $ref: '#/components/parameters/IdempotencyKey'
        - {name: stageCode, in: path, required: true, schema: {type: string, enum: [BOOKING]}}
        - {name: requirementKey, in: path, required: true, schema: {type: string}}
        - {name: If-Match, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 64}}
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [answer]
              properties:
                answer: {type: string, enum: [YES, NO, NA, UNANSWERED]}
                evidenceId: {type: string, format: uuid}
                remarks: {type: string, maxLength: 4000}
      responses:
        '200': {description: Booking document assessment recorded, content: {application/json: {schema: {type: object, additionalProperties: true}}}}
        default: {$ref: '#/components/responses/ProblemResponse'}
'''

text = OPENAPI.read_text()
if SENTINEL not in text:
    if MARKER not in text:
        raise SystemExit("OpenAPI insertion marker not found")
    OPENAPI.write_text(text.replace(MARKER, BLOCK + MARKER, 1))

spec = yaml.safe_load(OPENAPI.read_text())
required = {
    "/tenants/{tenantId}/journeys/{journeyId}/booking/start",
    "/tenants/{tenantId}/journeys/{journeyId}/booking/close-ready",
    "/tenants/{tenantId}/journeys/{journeyId}/booking/close-no-delivery",
    "/tenants/{tenantId}/journeys/{journeyId}/booking/cancel",
    "/tenants/{tenantId}/journeys/{journeyId}/booking/mark-duplicate",
    "/tenants/{tenantId}/journeys/{journeyId}/capture/{fieldKey}",
    "/tenants/{tenantId}/journeys/{journeyId}/booking/extraction/refresh",
    "/tenants/{tenantId}/journeys/{journeyId}/uc03-workspace",
    "/tenants/{tenantId}/journeys/{journeyId}/stages/BOOKING/documents/{requirementKey}/evidence",
}
missing = sorted(required - set(spec.get("paths", {})))
if missing:
    raise SystemExit("Missing C1 OpenAPI paths: " + ", ".join(missing))
print("UC03_C1_OPENAPI=PASS")
