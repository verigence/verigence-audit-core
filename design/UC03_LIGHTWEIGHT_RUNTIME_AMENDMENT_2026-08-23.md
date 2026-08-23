# UC03 Lightweight Runtime Amendment

Date: 2026-08-23
Status: implementation amendment from `dev`

This amendment extends the UC01/UC02 lightweight-runtime principles to UC03 journey flows without weakening functional authorization or creating a generic master-data cache.

## Runtime rules

1. The Security human JWT is validated on every protected Audit Core request through the shared server-side JWT validator/JWKS client. Normal requests must not fetch JWKS repeatedly.
2. `/v1/me/projects` resolves operational Project membership, role and Dealer/Outlet scope from the Audit Core `business_assignments` projection after human authentication. It does not require a separate Security administrative round trip.
3. Security remains the functional authorization source of truth for protected UC03 business APIs. To collapse a burst of identical lazy reads/actions, Audit Core may reuse a successful `ALLOW` decision for the same USER + Tenant + permission for a very short server-side window. Denials, errors and mismatched decisions are never cached.
4. ServiceIntegration access tokens and the backend Security HTTP client are reused server-side. Browser state is never an authorization cache.
5. UC03 landing metrics and work items remain separate lazy API calls. They must not be combined into a large bootstrap response merely to reduce call count.
6. React Query/window lifecycle behaviour must not refetch operational data merely because the browser receives both focus and visibility events for one resume action.
7. Booking document processing must have one polling/refresh loop only. Parallel polling of processing status plus extraction refresh plus workspace reload for the same four-second interval is prohibited.
8. After a mutation, re-read only the state genuinely required to render the next correct aggregate version. Do not automatically reload unrelated Project, Dealer, Outlet or master data.
9. Do not introduce a generic UC03 master cache. Only master/reference data proven to be repeatedly read on a hot journey path may be cached server-side, with explicit invalidation/version rules.
10. The centralized Audit Core database pool (`pool_pre_ping` plus bounded pool/connect/statement timeouts) applies unchanged to UC03.

## Short-lived functional authorization reuse

A successful Security authorization decision may be reused in Audit Core process memory for at most 10 seconds using the key:

`(security_user_id, tenant_id, permission_key)`

The purpose is request coalescing for one UI interaction, not long-lived authorization caching. The following rules are mandatory:

- human JWT validation still happens for every request;
- only `allowed=true` decisions may be reused;
- DENY responses are never cached;
- Security errors are never cached;
- the cache is server-side only;
- the decision key includes USER, Tenant and permission;
- the existing ServiceIntegration token reuse remains separate from this decision window.

This means a dashboard that loads `landing-metrics` and `work-items` concurrently should normally generate one Security `audit.journey.read` decision during the page burst rather than two identical decisions.

## Web request-path corrections

### Project context

`ProjectContextGate` keeps its small lazy `/v1/me/projects` request but must not refetch the Project directory because of routine window focus/reconnect events. Project changes and explicit query invalidation remain valid reload triggers.

### UC03 landing

Landing metrics and the 10-row work list remain separate queries. Neither query should automatically refetch on window focus/reconnect. User filtering and explicit retry/refetch remain valid triggers.

### Booking workspace

The Booking workspace must use one background-processing loop. While processing is pending, the loop may request extraction refresh and then reload the workspace at the configured interval. A second independent `/processing-status` polling interval is not allowed at the same time.

Browser resume handling must not register both `focus` and `visibilitychange` as equivalent refresh triggers because one tab resume can emit both events. Keep a single visibility-based resume trigger plus reconnect handling.

### Delivery workspace

Delivery follows the same resume rule: no duplicate focus + visibility reload for one resume event. Successful mutations may invalidate/reload the one Delivery workspace query required to obtain the next aggregate state.

## Acceptance criteria

- One UC03 page burst does not issue multiple ServiceIntegration tokens.
- Landing metrics + work items do not issue duplicate identical live Security authorization checks within the short reuse window.
- A Booking with pending document processing has one four-second processing loop, not overlapping polling loops.
- One browser resume event does not produce duplicate Booking/Delivery workspace GETs.
- Lazy loading remains intact.
- No browser authorization/master cache is introduced.
- No generic master-result cache is introduced.
- Existing UC03 role, Tenant, Dealer and Outlet scope enforcement remains intact.
