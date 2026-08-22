# Verigence Audit Core — API Contract UC02 Project Reference Data Addendum

**Document ID:** VAC-API-003  
**Version:** 1.2  
**Status:** IMPLEMENTED FOR UC02 DEV  
**Date:** 2026-08-22  
**Base contract:** `VAC-API-002 v1.1` / `docs/AUDIT_CORE_API_CONTRACT_v1.1.md`  

This addendum changes only the Project-creation reference-data contract required by UC02. All other VAC-API-002 v1.1 semantics remain unchanged.

## 1. Platform Project reference data

Project creation occurs before a Tenant/Project identifier exists, so OEM and Product Category selectors require a platform-scope read endpoint.

```text
GET /v1/project-reference-data
```

Headers:

```text
Authorization: Bearer <Security-issued human SuperAdmin JWT>
X-Correlation-ID: optional/recommended
```

The endpoint is read-only and returns active reference masters. The Web UI displays business names and retains the returned UUID identifiers for `POST /v1/projects`; users do not enter technical identifiers.

Response:

```json
{
  "oems": [
    {
      "oemId": "uuid",
      "oemCode": "MAHINDRA",
      "oemName": "Mahindra"
    }
  ],
  "productCategories": [
    {
      "productCategoryId": "uuid",
      "categoryCode": "FOUR_WHEELERS",
      "categoryName": "Four Wheelers"
    }
  ]
}
```

Only active reference rows are returned. Ordering is deterministic by business name/code.

Normal Audit Core `application/problem+json` behavior remains unchanged for authentication, authorization and server errors. Correlation IDs remain available in the API response/header for tracing; the Web UI is not required to display them.

## 2. Initial UC02 DEV master values

The UC02 DEV baseline seeds these active OEM business values:

- Mahindra
- Hyundai
- Maruti
- Mercedes Benz
- BMW
- Skoda
- Volkswagen
- Tata Motors

The initial Product Category master value is:

- Four Wheelers

These values are master data persisted in Audit Core, not hard-coded Web dropdown labels.

## 3. Project creation relationship

`POST /v1/projects` continues to require:

```text
oemId              existing active OEM UUID
productCategoryId  existing active Product Category UUID
```

The Web dropdowns must submit the identifiers returned by `GET /v1/project-reference-data`.
