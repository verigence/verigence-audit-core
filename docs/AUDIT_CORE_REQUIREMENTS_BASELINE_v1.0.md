# Verigence Audit Core — Requirements Baseline

**Document ID:** VAC-REQ-001  
**Version:** 1.0  
**Status:** BASELINED — Initial Process Baseline  
**Baseline date:** 2026-08-15  
**Repository:** `verigence/verigence-audit-core`  

> This is the initial requirements baseline. Additional business processes supplied after this baseline must be incorporated through a versioned requirements update. Open or contradictory source items are deliberately recorded as open decisions and are not silently resolved in this document.

---

## 1. Purpose

Verigence Audit Core is the core business-domain module of the Verigence platform. It SHALL manage the business context, lifecycle, controls, observations and audit outcomes for the dealer transaction audit process.

For the initial business domain, the product being audited is **four-wheel passenger/commercial vehicle sales through authorised automobile dealerships**.

Audit Core SHALL cover, at minimum, the business lifecycle represented by the supplied process material:

1. Project onboarding and operating landscape setup
2. Booking capture and classification
3. Delivery readiness and execution
4. Payment verification
5. Insurance and accessories compliance
6. Daily audit operations / end-of-day operating routines
7. Trade-in lifecycle
8. Escalation and CRM follow-up
9. System validation and business analytics
10. Audit observations, review and closure

Audit Core SHALL integrate with Verigence Security for identity/authorisation and Verigence DI for document/evidence intelligence. It SHALL remain loosely coupled from both modules.

---

## 2. Source inputs for this baseline

This baseline is derived from the following business inputs.

### 2.1 Process workbook

**Source:** `SPR_Tool_Process_SubProcess_Activity_Details (2)(1).xlsx`

The `Process Flow` sheet contains **104 numbered process activities** across eight major process groups:

| Process group | Activity numbers | Count |
|---|---:|---:|
| Booking Capture & Classification | 1–15 | 15 |
| Delivery Readiness & Execution | 16–25 | 10 |
| Payment Verification | 26–37 | 12 |
| Insurance & Accessories Compliance | 38–41 | 4 |
| Daily Audit Operations | 42–52 | 11 |
| Trade-In Lifecycle | 53–59 | 7 |
| Escalation & CRM Follow-up | 60–66 | 7 |
| System Validation & Analytics | 67–104 | 38 |

The workbook also contains two explicit additions after the numbered process:

- Daily PC & TL Activity Tracker
- Daily Activity Notepad for each PC

The `Some relevant Pic` sheet contains reference UI examples for RSA, Registration Type, Extended Warranty, Service Package, Corporate Discount and Trade-In/Exchange Discount.

### 2.2 Existing-tool screen workbook

**Source:** `SPR Details - Copy (3)(1).xlsx`

The workbook contains the existing-tool journey/screens covering, among other things:

- price-list selection;
- customer/dealer details;
- customer type;
- SC/Sales Consultant details;
- booking-file upload;
- booking and intimation dates;
- deal type, deal source and lead source;
- model, fuel type, variant and colour;
- registration state/territory/district;
- VIN/chassis and DMS details;
- registration type/category;
- booking commercial summary with Standard vs Actual values;
- RSA, EW, service package and other charges;
- discount details, exchange/trade-in and corporate discounts;
- delivery date/intimation and delivery-document checklist;
- payment receipts and payment verification;
- DO/finance verification;
- system observations, breach status, Auditor remarks and Team Lead remarks.

The existing-tool screens are treated as **business-input evidence**, not as a mandatory future UI design. The future Web/Mobile UX may be redesigned while preserving approved business requirements.

### 2.3 Business requirements provided during project discussion

The requirements in Sections 3–6 concerning Project onboarding, OEM/dealer landscape, Verigence project roles, dealership participants, price masters, model/variant/colour hierarchy and monthly discounts are based on the project-owner requirements provided on 2026-08-15.

---

## 3. Core business principles

### VAC-BP-001 — Project-centric operation

Audit activity SHALL operate within a **Project**. A Project is the primary business operating boundary below a Verigence Tenant.

A Verigence Tenant MAY contain multiple Projects.

### VAC-BP-002 — One OEM/product context per Project baseline

For the initial baseline, each Project SHALL be associated with:

- one OEM;
- one product category/type;
- for the current use case, product category = four wheeler.

The design SHOULD avoid preventing future support for other product categories.

### VAC-BP-003 — Evidence-first audit

Where a value already exists in an uploaded source document, system screenshot or authoritative upstream source, the audit team SHOULD NOT be required to re-key the same value merely to perform the audit.

Audit Core SHALL distinguish between:

- operational/business metadata legitimately entered by a user;
- source-system facts;
- document/evidence-derived facts supplied by DI;
- system-calculated facts;
- human observations/remarks.

The provenance of an audit-relevant value SHALL be retained.

### VAC-BP-004 — Configurable business rules

OEM prices, discounts, thresholds, document requirements, validation rules and similar business controls that change by Project/OEM/month SHOULD be configuration/master-data driven rather than embedded as code constants.

### VAC-BP-005 — Reproducible audit

An audit outcome SHALL remain explainable using the business configuration and evidence that were effective at the time of the relevant transaction/evaluation.

### VAC-BP-006 — Loose coupling

Audit Core SHALL NOT use Security or DI private databases as an integration mechanism. Integration SHALL occur through approved service contracts/events.

---

## 4. Project onboarding requirements

Project onboarding is the first mandatory business process for Audit Core.

### VAC-PRJ-001 — Create Project

The system SHALL support creation of a Project with at least:

- Project identifier/code;
- Project name;
- Tenant association;
- OEM;
- product type/category;
- effective start date;
- optional end date;
- operating status;
- applicable geography/time zone where required.

### VAC-PRJ-002 — Project operating hierarchy

A Project SHALL support association of multiple Dealers.

Each Dealer SHALL support multiple Dealer Locations/Outlets within the Project landscape.

Conceptually:

```text
Tenant
  -> Project
      -> OEM / Product Type
      -> Dealer(s)
          -> Outlet / Location(s)
```

### VAC-PRJ-003 — Dealer location classification

A Dealer Location SHALL support classification as at least:

- `ONSITE`
- `SATELLITE`

A Satellite location means a Dealer location selling fewer than a defined monthly vehicle-volume threshold.

The threshold SHALL be configurable and SHALL NOT be hard-coded.

The exact threshold value and whether classification is automatic or approved manually is an open business decision.

### VAC-PRJ-004 — Project resource assignment

Users SHALL be assignable to a Project and, where applicable, to subsets of Dealers/Locations within that Project.

The model SHALL support changes in assignment over time without destroying historical responsibility for completed audit work.

### VAC-PRJ-005 — Project configuration lifecycle

Project configuration SHALL support effective dating/versioning where required so that new price lists, discounts, rules or assignments do not rewrite historical audit context.

---

## 5. People, roles and organisational participants

There are two distinct populations: **Verigence project team roles** and **Dealership participants**.

### 5.1 Verigence project roles

The initial Project SHALL support the following business roles.

#### PC — Process Consultant

The PC performs the field/process audit work and is the primary user interacting with booking, delivery, payment and daily operational evidence.

Typical responsibilities from the source process include:

- receive the booking file from dealership Sales Executive/SC;
- validate completeness and evidence;
- upload/capture booking/delivery/payment documents and photos;
- conduct payment-source checks;
- perform delivery physical verification;
- perform trade-in physical/record verification;
- perform daily reconciliation routines;
- record observations/remarks and follow-up activity.

#### TL — Team Lead

The TL performs supervisory review of PC-completed files/work.

The process requires TL capability to:

- review completed booking/delivery cases;
- review system and PC observations;
- classify/tag the reviewed case as `BREACH`, `NO_BREACH`, or `SEND_BACK`;
- record TL remarks/comments;
- monitor/review daily PC activity.

#### PM — Project Manager

The PM is the project-management/oversight role across assigned TLs, Dealers and Locations.

The source workbook uses the label **PMO** for several activities. For this baseline, those source activities are associated with the PM business capability unless a separate PMO role is later confirmed.

Typical requirements include:

- project-level daily breach/exception oversight;
- escalation management;
- project remarks/review;
- management visibility across assigned project landscape.

#### CRM

CRM is responsible for mandatory customer/verification calls triggered by business events and for recording call outcomes/completion.

#### Executive

Executive is the senior Project oversight role and conceptually leads the Project.

The Executive SHALL have project-level visibility appropriate to the permissions granted through Security. Exact operational actions/approval rights are not yet baselined and remain an open decision.

### VAC-ROLE-001 — Multiple assignments

A user MAY hold different roles in different Projects and MAY have different Dealer/Location scopes by Project.

### VAC-ROLE-002 — Historical ownership

Case/activity records SHALL preserve the actor and Project role/context under which a material action was performed.

### VAC-ROLE-003 — Security mapping

Business role names SHALL NOT be the only security enforcement mechanism.

Verigence Security SHALL remain authoritative for identity and effective permissions. Audit Core SHALL use the authenticated actor/tenant/permission context provided by Security and SHALL additionally enforce Project/Dealer/Location business scope where required.

The final role-to-permission matrix will be baselined during solution/security integration design.

### 5.2 Dealership participants

Dealership personnel are business participants but are not automatically Verigence application users.

The initial landscape includes at least:

- Sales Executive / Sales Consultant (SC);
- Sales Executive Team Lead / Sales Manager;
- dealership CRM team;
- General Manager / Business Head;
- Accounts team;
- Delivery Coordinator;
- other dealership roles as required by a Project.

### VAC-DLR-001 — Dealership Sales Executive initiates audit flow

The dealership Sales Executive/SC is the participant who initiates the business process by handing the booking file to the assigned PC after booking-file preparation.

There is no baseline requirement for the dealership Sales Executive to use Verigence directly.

### VAC-DLR-002 — Dealership participant master/reference

Audit Core SHALL be capable of recording relevant dealership participant information needed for audit traceability, including Sales Executive/SC identity/contact and other responsible dealership contacts where required.

---

## 6. Master data requirements

Audit Core requires configurable master data that can be effective-dated/versioned where business values change over time.

### 6.1 OEM and product catalogue

### VAC-MST-001 — OEM master

The system SHALL maintain OEM master information and associate a Project to an OEM.

### VAC-MST-002 — Product hierarchy

For the automobile use case, the product hierarchy SHALL support:

```text
OEM
  -> Model
      -> Variant
          -> Colour
```

The source screens also contain Fuel Type; therefore the catalogue SHALL support product attributes such as fuel/powertrain where applicable.

### VAC-MST-003 — Effective product availability

Models, variants, colours and combinations SHALL support active/effective status so historical bookings remain valid after a product is discontinued or changed.

### 6.2 Price master

### VAC-PRICE-001 — OEM price master

The system SHALL support OEM/Project price lists with an effective period/version.

A price list SHALL be selectable/resolvable for a booking based on the applicable date/project context.

### VAC-PRICE-002 — Product-level pricing

Price values SHALL be capable of varying by relevant product combination, including Model/Variant/Colour where required.

### VAC-PRICE-003 — Standard versus Actual

Audit Core SHALL distinguish **Standard** amounts expected from approved master/rules from **Actual** amounts observed in the business transaction/evidence.

The supplied screens/process identify Standard/Actual comparison for several commercial components including Ex-Showroom and Scheme Discount.

### VAC-PRICE-004 — Booking commercial components

The baseline commercial summary must be able to represent applicable components visible in the supplied material, including as relevant:

- Ex-Showroom;
- Registration;
- Genuine Accessories;
- Non-Genuine Accessories;
- Insurance;
- TCS;
- Extended Warranty (EW);
- Green Tax;
- Service Package;
- Other Charges;
- HP Charges;
- deal/net-deal totals;
- discounts and discount components.

The exact OEM-specific component catalogue SHALL be configurable.

### 6.3 Discount master

### VAC-DISC-001 — Time-bound discount schemes

The system SHALL support discount/scheme masters that become effective for defined periods, commonly monthly.

### VAC-DISC-002 — Discount types

The model SHALL support multiple discount types/categories, including examples supplied by the business/process material such as:

- corporate discount;
- festival/monthly OEM scheme;
- sales discount;
- buffer discount;
- exchange discount;
- insurance discount;
- corporate/POI discount;
- other OEM/Project-defined scheme or discretionary components.

### VAC-DISC-003 — Eligibility

A discount scheme MAY be limited by one or more of:

- Project/OEM;
- date/effective period;
- Model;
- Variant;
- Colour;
- customer type;
- Dealer/Location where required;
- other configured eligibility criteria.

### VAC-DISC-004 — Discount audit

Audit Core SHALL support comparison of approved/standard scheme value to actual discount applied and generation of an observation/finding when configured tolerance/rules are breached.

### VAC-DISC-005 — Price/discount snapshot

The effective price-list and scheme context used for a booking SHALL remain identifiable after masters change.

### 6.4 Supporting masters/configuration

The baseline requires configurable values for, at minimum:

- Customer Type (`Individual`, `Corporate`, `CSD`, `Leasing`, extensible);
- Deal Type / booking classification;
- Out-of-Scope reasons;
- Deal Source;
- Lead Generation Source;
- Finance Type;
- Insurance Type;
- Registration Type;
- Registration Category;
- Territory Category;
- RSA plan/options;
- EW plan/options;
- Service Package options;
- Exchange Discount Type;
- Corporate Discount Type;
- payment modes;
- observation/control catalogue;
- document requirement catalogue;
- thresholds/tolerances.

---

## 7. End-to-end Audit Core business storyline

The current baseline business journey is:

```text
Project Onboarding
  -> OEM / Product / Dealer / Outlet / Team / Masters configured
  -> Dealer Sales Executive confirms booking
  -> Dealership prepares signed Booking Docket + KYC + booking-payment proof
  -> Physical/digital booking file handed to PC
  -> PC registers/accepts case and uploads evidence
  -> DI extracts/reads evidence where supported
  -> Audit Core validates booking, customer, duplicates, pricing and classifications
  -> Booking remains active while payment/delivery evidence evolves
  -> Delivery file is supplied before planned delivery
  -> PC validates delivery evidence and performs physical verification/photos
  -> Payments, finance/DO/PO, insurance, accessories and trade-in are verified
  -> System raises rule-based observations
  -> TL reviews completed case and marks Breach / No Breach / Send Back
  -> PM performs project-level oversight/escalation management
  -> CRM completes mandatory calls triggered by defined risk/events
  -> Daily/EOD routines reconcile dealership operational records with audited cases
  -> Analytics expose trends, ageing, exceptions, workload and business controls
```

This storyline SHALL be extendable as additional processes are supplied.

---

## 8. Booking Capture & Classification requirements

The source process contains activities 1–15.

### VAC-BKG-001 — Booking-file preparation inputs

A booking file is expected to include at least:

- duly signed Booking Docket;
- customer KYC documents;
- Minimum Booking Amount payment proof.

The Booking Docket is expected to be signed by applicable parties identified in the process: Customer, SC, SM/GM and Accounts Team.

### VAC-BKG-002 — Booking handoff

The Sales Executive/SC SHALL be able to hand the completed booking file to the PC through the operational process. The initial source process explicitly permits a manual/physical handoff rather than requiring real-time dealership-system integration.

### VAC-BKG-003 — Booking validation

The PC process SHALL support validation of:

- booking-docket completeness/signatures;
- KYC presence/validity;
- initial/minimum booking payment evidence.

### VAC-BKG-004 — Minimum booking amount and trade-in exemption

The process SHALL support configured Minimum Booking Amount validation and an exchange/trade-in exception when an old vehicle/keys are handed over at booking.

Where the trade-in RC is not in the new-car customer's name, a valid Transfer Letter or Authorization Letter is mandatory supporting evidence according to the supplied process.

### VAC-BKG-005 — Booking record

The booking business record SHALL support the fields/business facts represented by the supplied process/screens, including as applicable:

- Booking Number;
- Booking Date;
- booking/intimation date and time;
- Project, Dealer and Location;
- customer information and customer type;
- PAN, Aadhaar, GST where applicable;
- contact/mobile number;
- pincode, district/state and registration geography;
- SC/Sales Executive details;
- Deal Type;
- Deal Source;
- Lead Generated Through/Source;
- Model, fuel type, Variant and Colour;
- territory categorisation;
- VIN/Chassis when available;
- DMS customer/invoice details when available;
- registration type/category;
- applicable price-list context.

Where values can be extracted from authoritative uploaded documents, Audit Core SHOULD receive those facts from DI instead of requiring duplicate manual re-entry.

### VAC-BKG-006 — Booking classification

The process SHALL support In-Scope/Out-of-Scope and referral/classification concepts represented in the source materials.

The exact canonical set contains a source inconsistency and is listed in Open Decisions.

### VAC-BKG-007 — DSA and corporate identification

The booking SHALL support business classification needed for DSA/deemed-DSA analysis and Corporate customer/discount controls.

### VAC-BKG-008 — Duplicate-booking controls

The system SHALL perform duplicate/customer-match checks using configured identity rules based on supplied identifiers/facts, including PAN, Aadhaar, GST and mobile/contact number.

The supplied process also contains matching concepts involving father's/spouse's name, address and Last Name + Pincode.

A weaker match such as Last Name + Pincode SHALL be capable of generating a manual-verification flag without being treated as definitive identity proof.

### VAC-BKG-009 — Cross-dealer duplicate visibility

Duplicate booking detection SHALL operate across Dealers within the appropriate Project/Tenant scope so a customer booking at a second dealership can be flagged for investigation.

---

## 9. Delivery Readiness & Execution requirements

The source process contains activities 16–25.

### VAC-DEL-001 — Delivery-file advance submission

The process expects the delivery-stage file approximately **30 minutes before scheduled delivery**.

The implementation SHALL support recording delivery intimation and whether delivery was informed in advance.

### VAC-DEL-002 — Delivery evidence checklist

The system SHALL support a configurable delivery-document checklist with required/conditional/optional status and evidence links.

The supplied process/screens include, as applicable:

- Wholesale Invoice;
- Customer Invoice / Tax Invoice — DMS;
- Tax Invoice — Tally or other books;
- Registration Invoice / RTO Challan;
- Insurance Cover Note;
- Accessory Invoice — DMS;
- Accessory Invoice — Tally/other books;
- handwritten/challan accessory evidence where the DMS invoice is not yet generated;
- Customer Ledger;
- Cost Sheet;
- Gate Pass;
- Customer KYC/Customer ID;
- EW Invoice;
- RSA Invoice;
- other Value Added Service documents;
- No Dues Certificate;
- Payment Receipt(s);
- Delivery Order (DO);
- Trade-In documents;
- Corporate ID/claim evidence;
- supporting documents for third-party payments;
- Debit Note for insurance/registration where applicable;
- Docket Audit Form;
- other Project-defined document;
- pictures of the delivered car;
- petrol/diesel slip where applicable.

### VAC-DEL-003 — Incomplete file control

If one or more mandatory delivery documents are missing, the system SHALL be capable of raising an incomplete-document observation.

### VAC-DEL-004 — Uninformed delivery control

If a delivery occurs without required prior file/intimation, the PC SHALL be able to record a delivery-not-informed observation and still perform the physical delivery audit.

### VAC-DEL-005 — Mandatory exception remarks

Where a delivery discrepancy/exception is identified, the process requires a Remarks/Exception entry before submission/closure of that audit action.

### VAC-DEL-006 — Physical verification

At delivery the PC SHALL support capture/upload of approximately 5–6 verification photos, including VIN and representative exterior/interior views.

Mobile-device camera capability will be consumed through the Verigence Web/Mobile layer; Audit Core SHALL retain the resulting business/evidence references and audit context.

### VAC-DEL-007 — Delivery completion facts

Delivery completion SHALL support relevant final facts such as:

- Delivery Date;
- DMS Invoice Date;
- DMS Invoice Number;
- final delivery/audit status;
- physical-verification completion.

### VAC-DEL-008 — Registration controls

The system SHALL support configured registration types/categories. The source process requires applicable registration amount to be derived/populated according to the selected registration type/master/rules.

---

## 10. Payment Verification requirements

The source process contains activities 26–37.

### VAC-PAY-001 — Multiple receipts/payments

A booking SHALL support multiple payment/receipt records and multiple payment modes.

Baseline modes represented in the source include:

- Bank Transfer / RTGS / Credit / Debit / eWallet / UPI;
- Card Swipe;
- Cheque;
- Cash/DD;
- DO;
- PO;
- Trade-In value;
- Refund;
- other configured modes.

### VAC-PAY-002 — Payment record attributes

Payment records SHALL support applicable facts such as:

- payment mode;
- receipt/payment date;
- amount;
- payer/source;
- UTR/reference number;
- bank name;
- supporting evidence/document links;
- verification state;
- realised amount;
- verifier/verification time;
- finance context where applicable.

### VAC-PAY-003 — Direct customer payment

The process SHALL support verification that payment was made directly by the customer where required.

### VAC-PAY-004 — Third-party payment evidence

For payments not made by the customer, the system SHALL support conditional documentary requirements.

The supplied process states:

- family-member payment: relationship proof + handwritten authorization;
- other third-party payment: notarized authorization + supporting documents.

### VAC-PAY-005 — Finance/loan/PO/DO

The system SHALL support financed/partial-payment structures, Finance Type, Bank Name and DO/PO-related verification.

### VAC-PAY-006 — PO schedule

A corporate PO payment structure SHALL support outstanding/pending/realisation tracking against agreed commercial terms.

The exact `Total Receipt Realised: PO` business logic is not yet final and SHALL remain configurable/open until approved.

### VAC-PAY-007 — DSA observation

Where a payment source is identified as a DSA and the configured control requires it, the system SHALL raise the corresponding observation/analysis input.

### VAC-PAY-008 — Trade-in as payment component

Trade-in purchase value SHALL be capable of contributing to payment/receipt reconciliation when the business rule permits it.

### VAC-PAY-009 — Cash controls

Cash payment audit SHALL support evidence/confirmation that:

- cash was received in front of CCTV where required;
- cash was received in the presence of an auditor where required;
- off-site cash collection received prior intimation where required.

Missing prior intimation SHALL be capable of raising an observation.

### VAC-PAY-010 — Month-end cash CRM trigger

A cash payment received on the last day of the month SHALL be capable of triggering a mandatory CRM call according to the supplied process.

### VAC-PAY-011 — Payment Verification Tracker

The system SHALL provide booking-level and operational visibility showing:

- total number of payments;
- payment mode;
- verification status;
- verified count;
- unverified/pending count;
- relevant transaction detail for follow-up.

### VAC-PAY-012 — Payment reconciliation status

The system SHALL support an aggregate Payment Verification Status such as:

- `PENDING`;
- `PARTIALLY_PENDING`;
- `COMPLETED`.

The exact state names may be normalised during solution design while preserving these semantics.

---

## 11. Insurance, RSA, EW, accessories and value-added services

The source process contains activities 38–41 and supporting UI references.

### VAC-INS-001 — Insurance type

The booking/delivery audit SHALL support at least:

- In-House Insurance;
- Self Insurance.

### VAC-INS-002 — Insurance details

Where applicable, the process SHALL capture/derive:

- insurance company;
- premium/amount;
- agent code;
- relevant insurance evidence.

### VAC-INS-003 — Approved insurance calculation

The process states that OEMs may use an approved Insurance Calculator and that premium/OD discount should be checked against the applicable approved calculation.

Audit Core SHALL be capable of comparing the approved/standard insurance result to actual charged values. The external-calculator integration method is deferred to solution design.

### VAC-INS-004 — Self-insurance CRM call

Every Self Insurance case SHALL be capable of triggering a mandatory CRM call.

### VAC-ACC-001 — Accessories classification

Accessories SHALL support classification as at least:

- Genuine/OEM;
- Non-Genuine.

Standard and actual accessory values SHALL be supportable for audit comparison.

### VAC-VAS-001 — RSA/EW/Service Package

Project/OEM configuration SHALL support RSA, Extended Warranty and Service Package options/plans such as the examples supplied in the reference screenshots. The catalogue SHALL be configurable rather than restricted to the example list.

---

## 12. Daily Audit Operations / End-of-Day requirements

The source process contains activities 42–52 plus the Daily PC/TL Activity Tracker and Daily Activity Notepad additions.

### VAC-DAY-001 — Gate-out register evidence

The PC SHALL upload/capture a photograph of the Gate Issue/Gate-Out register at the start of the daily dealership visit.

### VAC-DAY-002 — Delivery reconciliation

The PC SHALL reconcile gate-register delivery count against delivery files/cases received and follow up with the dealership GM/Business Head when a gap is found.

### VAC-DAY-003 — Daily financial/operational source collection

The daily routine SHALL support receipt/upload/reference of applicable source records, including:

- cash ledger;
- bank statement;
- booking/retail dump;
- delivery dump.

### VAC-DAY-004 — Previous-day exception review

The daily audit process SHALL support review of previous-day transactions to identify activity that was not intimated in advance or occurred without the auditor's physical presence where such presence was required.

### VAC-DAY-005 — Common booking-amount bank scan

The process requires review of bank statements for transactions resembling common booking amounts (examples in source: ₹11,000, ₹21,000, ₹25,000, ₹51,000) and follow-up with Accounts to confirm nature/supporting record.

The amounts SHALL be configuration driven rather than hard-coded if this control is automated.

### VAC-DAY-006 — TL file review

On PC file completion, TL SHALL review the file and record one of the required business outcomes:

- Breach;
- No Breach;
- Send Back.

TL remarks SHALL be retained.

### VAC-DAY-007 — PM daily oversight

PM SHALL have daily project-level visibility of breach/exception counts across the Dealers/Locations within their assignment scope.

### VAC-DAY-008 — Daily PC/TL Activity Tracker

The system SHALL support a daily activity/productivity view capturing, at minimum:

For PC:

- booking points/cases handled;
- delivery points/cases handled;
- payment reconciliations/updates;
- activities attended;
- approximate/tentative time where required.

For TL:

- number of review cases;
- activities/cases attended;
- approximate/tentative time where required.

### VAC-DAY-009 — PC Daily Activity Notepad

Each PC SHALL have a daily notepad/activity record to capture:

- tasks;
- important activities;
- follow-ups;
- assigned actions;
- notes for future reference.

Historical notes SHALL remain retrievable and attributable to date/PC.

---

## 13. Trade-In Lifecycle requirements

The source process contains activities 53–59.

### VAC-TRD-001 — Trade-in booking data

The system SHALL support trade-in/exchange details including as applicable:

- trade-in date;
- old vehicle purchase/value amount;
- old vehicle registration number;
- old vehicle model/year;
- RC availability;
- whether RC/vehicle is in the new-car owner's name;
- supporting authorization/transfer documents;
- exchange discount type;
- standard discount;
- actual discount;
- variance.

### VAC-TRD-002 — Exchange discount types

The source UI contains examples `Exchange Bonus` and `Scrappage`. The type catalogue SHALL be configurable.

### VAC-TRD-003 — Purchase and sale lifecycle

The trade-in lifecycle SHALL separately track:

- purchase amount/date;
- current sale status;
- sale amount/date when sold;
- calculated profit/loss;
- ageing while unsold.

Trade-in sale value SHALL NOT be conflated with trade-in receipt/payment value.

### VAC-TRD-004 — Ageing control

The system SHALL support a configurable resale/ageing threshold and escalation of old vehicles remaining unsold beyond the applicable threshold.

The source specifies **60 or 90 days — to be confirmed**; therefore no numeric value is baselined yet.

### VAC-TRD-005 — Physical presence verification

While a trade-in vehicle remains unsold, the PC SHALL be able to record physical-presence verification. A vehicle shown unsold in system but physically missing SHALL be capable of raising an escalation/observation.

### VAC-TRD-006 — Mandatory CRM call

Each trade-in case SHALL be capable of triggering the required CRM follow-up/call, including recurring/weekly follow-up if configured.

---

## 14. Escalation and CRM Follow-up requirements

The source process contains activities 60–66.

### VAC-ESC-001 — Escalation case

The system SHALL support escalation cases, including disputes/conflicts between dealerships such as a poached booking.

An escalation SHALL support at least:

- date/time;
- customer/booking reference where applicable;
- originating Dealer/Location;
- counterparty Dealer/Location;
- stage;
- remarks/details;
- status;
- owner/responsible role;
- resolution/closure detail.

### VAC-ESC-002 — Cross-check new booking against escalation history

On a new booking, the system SHALL be capable of cross-checking relevant existing escalation records and flagging/linking a prior escalation when configured matching criteria are met.

### VAC-ESC-003 — Fresh/Open escalation CRM triggers

The process SHALL support mandatory CRM call triggers for:

- Fresh/new escalation;
- Open escalation remaining unresolved beyond the applicable SLA/threshold.

### VAC-CRM-001 — CRM call task/event

Mandatory CRM calls SHALL be generated/recorded as actionable business items associated with the triggering booking/case/event.

### VAC-CRM-002 — CRM call outcome

CRM SHALL record the outcome/status of each mandatory call.

### VAC-CRM-003 — Mandatory-call completeness

CRM users and authorised management SHALL be able to see completion/pending status across mandatory-call categories.

---

## 15. System validation requirements

The source process contains activities 67–82 for field/business validation.

The validation catalogue SHALL be configurable and versionable where practical.

### VAC-VAL-001 — Mobile/contact number

Contact/mobile numbers SHALL be validated according to applicable format rules. The source baseline requires exactly 10 numeric digits for the current India use case and indicates the rule should apply to all mobile-number fields such as customer and SC.

### VAC-VAL-002 — PAN format

PAN SHALL be validated against the current pattern represented in the source (`ABCDE1234F` shape).

### VAC-VAL-003 — Conditional mandatory fields

The system SHALL support conditional mandatory fields, including source examples:

- In-House Insurance -> Amount, Company Name, Agent Code required;
- financed deal -> Bank Name required;
- Corporate customer -> GST required.

### VAC-VAL-004 — Duplicate contacts

The system SHALL detect/report duplicate customer contact numbers, including same-model and different-model cases within the configured scope.

### VAC-VAL-005 — Invalid contact patterns

Invalid/repeated-digit contact patterns SHALL be capable of generating an observation according to configured rules.

### VAC-VAL-006 — Standard vs Actual controls

The system SHALL support rule-driven comparison of Standard versus Actual values including Ex-Showroom and Scheme Discount.

### VAC-VAL-007 — Actual without Standard

Where configured, Actual value populated while Standard is blank/zero SHALL raise a finding, subject to documented tolerance/exclusions. The source references a ≤₹100 tolerance and Self-deal exclusion; these shall be confirmed/configured before implementation.

### VAC-VAL-008 — Delivery-before-booking

Delivery Date earlier than Booking Date SHALL be treated as an invalid condition/observation.

### VAC-VAL-009 — Territory validation

Out-of-Territory/Out-of-State reason/category SHALL be validated against dealership and registration geography according to approved rules.

### VAC-VAL-010 — Insurance agent-code controls

The system SHALL support duplicate/cross-use agent-code checks represented in the source, including:

- duplicate Self Insurance agent code;
- duplicate agent code within a dealership;
- same agent code used for both Self and In-House insurance.

---

## 16. Analytics and derived business measures

The source process contains activities 83–104 plus the daily productivity additions.

Audit Core SHALL own authoritative business facts/calculations for its domain. Observability/analytics presentation layers may aggregate these facts but SHALL NOT redefine authoritative booking/audit state.

### VAC-ANA-001 — Duplicate Booking analytics

Support duplicate-booking metrics/trends.

### VAC-ANA-002 — Turnaround from first receipt

Support turnaround analysis from First Receipt to relevant downstream milestone such as delivery, including slab-wise and Model/Dealership views and MoM trend where required.

### VAC-ANA-003 — Finance analysis

Support finance-type breakdown, including source categories such as In-House, Outright Purchase and Self.

### VAC-ANA-004 — Accessories analysis

Support accessory value/penetration analysis, with Genuine/Non-Genuine distinction where needed.

### VAC-ANA-005 — Insurance penetration

Support insurance penetration and Self/In-House analysis. The source contains a historical reporting heuristic (`>₹100 charged = In-House`); authoritative classification SHOULD use the explicit Insurance Type when available, with any heuristic treated only as a configured analytic rule.

### VAC-ANA-006 — Self-insurance agent-code analysis

Support identical/reused insurance-agent-code analysis.

### VAC-ANA-007 — EW analysis

Support Extended Warranty penetration/analysis by relevant Project/Dealer dimensions.

### VAC-ANA-008 — Deemed DSA analysis

Support configured Deemed DSA classification/analytics based on relevant deal-source, insurance and finance facts. Exact business formula must be approved before implementation.

### VAC-ANA-009 — Trade-in analytics

Support:

- trade-in purchase totals;
- trade-in sales totals;
- profit/loss;
- ageing.

### VAC-ANA-010 — Receipt/payment summary

Support Total Receipt and Total Receipt Realised roll-up by mode.

DO, PO and Refund realisation logic are explicitly unresolved in the source and must not be guessed.

### VAC-ANA-011 — Short/Excess

Support calculation of Short/Excess against Standard and Actual commercial basis once the formula/tolerance is approved.

### VAC-ANA-012 — Observation/Error Summary

System validation/control breaches SHALL be aggregatable into an Error/Observation Summary with traceable rule/control source.

### VAC-ANA-013 — Multi-tier remarks

Observations/cases SHALL retain remarks from applicable review tiers including PC, TL and PM.

### VAC-ANA-014 — Per-car discount analysis

Support analysis of:

- Total Discount per car;
- discount from OEM Schemes;
- discount Above Scheme.

The source explicitly marks Total Discount and Above Scheme logic as unresolved. No formula is baselined in v1.0.

### VAC-ANA-015 — Productivity/workload

Support PC/TL daily workload/productivity analytics using the Daily Activity Tracker facts without turning the tracker into the authoritative source for booking/delivery/payment business state.

---

## 17. Observation, finding and review requirements

### VAC-OBS-001 — First-class observation/finding

An audit exception SHALL be represented as a first-class business record rather than only free-text remarks.

The record SHALL be capable of linking:

- Project/Dealer/Location;
- booking/case;
- process stage;
- control/rule;
- expected versus observed value where applicable;
- severity/category where configured;
- supporting evidence/document(s);
- responsible actor;
- status;
- PC remarks;
- TL remarks;
- PM remarks;
- timestamps and resolution history.

### VAC-OBS-002 — Source observation examples

The existing screens demonstrate observations such as:

- Deal Undercharged;
- Non-Compliance during Delivery Verification;
- Excess Discount;
- Non-Genuine Receipts;
- breach in trade-in/corporate claims;
- car delivered on short payment.

These are examples of configurable controls and SHALL NOT be treated as the complete future observation catalogue.

### VAC-OBS-003 — Review state

The system SHALL support the TL review semantics Breach / No Breach / Send Back and preserve review history.

### VAC-OBS-004 — No silent overwrite

A reviewer SHALL not erase the original system/PC observation. Review outcomes/remarks SHALL be appended/retained as a traceable history.

---

## 18. Document and evidence requirements

### VAC-DOC-001 — Configurable requirement catalogue

Document requirements SHALL be configuration driven by Project/process stage/booking characteristics where appropriate.

The system SHALL support at least:

- mandatory;
- conditional;
- optional;
- not applicable.

### VAC-DOC-002 — Evidence references, not duplicate binary ownership

Audit Core SHALL store business associations/references to documents/evidence managed by DI; it SHALL NOT duplicate DI as a second document-intelligence repository.

### VAC-DOC-003 — Evidence provenance

For an audit-relevant fact derived from evidence, Audit Core SHALL be able to retain/link enough information to identify the source document and, where the DI contract supports it, the extracted/verified field source.

### VAC-DOC-004 — Document lifecycle by process stage

A booking MAY accumulate documents throughout booking, payment, delivery, trade-in and daily-audit stages. The document set SHALL therefore be appendable/evolving rather than a one-time upload bundle.

### VAC-DOC-005 — Rejected/missing evidence

Missing, rejected, unprocessed or review-required DI evidence SHALL not be silently treated as verified business fact. Audit Core SHALL expose the appropriate pending/exception state.

---

## 19. Verigence DI integration requirements

Audit Core and DI are separate modules with separate responsibilities.

### VAC-DI-001 — DI responsibility

DI is responsible for document/evidence ingestion, processing/extraction, evidence quality/processing status and document-level intelligence according to the DI contract.

### VAC-DI-002 — Audit Core responsibility

Audit Core is responsible for the business transaction context, process lifecycle, configured business controls, observations/findings and audit outcome.

### VAC-DI-003 — Subject/document linkage

Audit Core SHALL support linking its customer/booking context to DI Subject/Document identifiers according to the final integration contract.

### VAC-DI-004 — Evidence-derived facts

Audit Core SHALL be able to consume relevant verified/extracted DI facts for business control evaluation instead of requiring the PC to manually reproduce those values.

### VAC-DI-005 — Processing state

Audit Core SHALL be able to distinguish document states such as pending processing, processed, failed and requiring review according to the stable DI public contract.

### VAC-DI-006 — No direct database access

Audit Core SHALL NOT read/write DI database tables directly.

### VAC-DI-007 — Idempotent/event-safe integration

Document-status/event/API processing SHALL be designed so duplicate delivery/retry of integration messages does not create duplicate booking documents, findings or business transitions.

---

## 20. Verigence Security integration requirements

### VAC-SEC-001 — Security authority

Verigence Security remains authoritative for:

- user identity/authentication;
- Tenant identity/context;
- effective permissions;
- access/session/device controls supplied by the Security contract.

### VAC-SEC-002 — Security token

Audit Core SHALL accept only the approved Verigence Security service authentication/authorisation contract for protected APIs.

### VAC-SEC-003 — Permission-based enforcement

Protected Audit Core actions SHALL be permission enforced. Business role names such as PC/TL/PM/CRM/Executive SHALL be used for business assignment and role semantics but SHALL not replace effective-permission checks.

### VAC-SEC-004 — Project business scope

In addition to Tenant-level security, Audit Core SHALL enforce the actor's Project/Dealer/Location assignment scope where the operation is scope limited.

### VAC-SEC-005 — Role mapping

The solution design SHALL define a role-to-capability/permission matrix for PC, TL, PM, CRM and Executive and identify the required Security permission catalogue changes, if any.

No Security repository change is implied by this requirements document.

---

## 21. Observability integration requirements

Audit Core SHALL conform to the baselined Verigence Observability requirements.

At minimum it SHALL emit/propagate the governed telemetry context necessary for:

- correlation across Security, Audit Core and DI;
- structured logging;
- service/API health and performance monitoring;
- audit-process operational analytics;
- failure diagnosis without treating observability telemetry as the authoritative audit trail.

Audit Core's authoritative audit/business history SHALL remain within the responsible domain records rather than relying on application logs as evidence.

---

## 22. Workflow / orchestration requirements

The business process contains assignments, review states, mandatory CRM actions, send-back loops, escalations and daily routines.

### VAC-WF-001 — State-driven process support

Audit Core SHALL support explicit business states/transitions and actionable assignments sufficient to execute the baseline process.

### VAC-WF-002 — Startup/lean architecture constraint

A separate enterprise workflow/BPM engine is **not a baseline requirement** for the initial implementation.

The solution design SHALL first evaluate whether the required state transitions, assignments, due dates and event-triggered actions can be implemented cleanly within Audit Core using a lightweight state/task model.

A separate workflow capability MAY be introduced later if complexity, dynamic configuration, SLA/escalation requirements or cross-module orchestration justify it.

### VAC-WF-003 — Future decoupling

If a workflow service is introduced later, Audit Core SHALL remain the source of truth for business state and SHALL integrate through stable commands/events rather than moving core business rules into the workflow product.

---

## 23. Non-functional requirements

### VAC-NFR-001 — Multi-tenant isolation

All Project/business data SHALL be Tenant isolated.

### VAC-NFR-002 — Scalability

The design SHALL support growth in:

- Projects;
- Dealers and Locations;
- users/assignments;
- bookings;
- documents/evidence references;
- payments;
- observations/findings;
- daily operational records;
- analytics volume.

### VAC-NFR-003 — API-first / modular

Audit Core SHALL expose stable service contracts suitable for Web/Mobile, DI integration and future services without coupling consumers to database schema.

### VAC-NFR-004 — Configuration driven

Frequently changing OEM/Project business values SHALL be data/configuration driven wherever practical.

### VAC-NFR-005 — Auditability

Material business actions and changes SHALL retain actor, timestamp, prior/current business context as appropriate, correlation and provenance sufficient for audit reconstruction.

### VAC-NFR-006 — Time handling

System timestamps SHALL be stored consistently, with Project/Location timezone applied for business reporting and daily/EOD logic where required.

### VAC-NFR-007 — Concurrency/idempotency

The design SHALL protect critical transitions and integrations from duplicate processing and conflicting concurrent updates.

### VAC-NFR-008 — Historical correctness

Master/config changes SHALL NOT retroactively alter the meaning of completed historical audits unless an explicit authorised re-evaluation process is invoked.

### VAC-NFR-009 — Security/privacy

Sensitive customer/payment/evidence data SHALL be accessed and exposed only according to Security permissions and applicable masking/privacy requirements.

### VAC-NFR-010 — Extensibility

The architecture SHALL support new process areas, controls, document requirements and OEM-specific configuration without requiring a full rewrite of the booking core.

---

## 24. Initial business entities identified from requirements

This section is **requirements-level terminology**, not the final physical data model.

The solution design is expected to address at least the following concepts:

- Tenant
- Project
- OEM
- Product Type
- Dealer
- Dealer Location / Outlet
- Project User Assignment
- Dealership Participant / SC
- Model
- Variant
- Colour
- Product attributes (e.g. Fuel Type)
- Price List / Price Master Version
- Price Component
- Discount / Scheme Master
- Discount Eligibility
- Customer
- Booking / Audit Case
- Booking Classification
- Commercial Summary
- Payment / Receipt
- Finance / DO / PO context
- Insurance
- Accessory
- RSA / EW / Service Package
- Registration
- Delivery
- Trade-In
- Document Requirement
- Booking/Evidence Document Reference
- Physical Verification / Photo Reference
- Audit Control / Rule
- Observation / Finding
- Review / Remarks
- CRM Call / Action
- Escalation
- Daily Audit Activity
- PC/TL Activity Tracker
- PC Daily Notepad
- business status/history/event records

The final aggregate boundaries, tables, indexes and ownership will be determined in the Solution Design and Data Model documents.

---

## 25. Open decisions / source contradictions

The following items are explicitly **OPEN** and SHALL NOT be assumed as resolved during implementation.

| ID | Open item | Source/context |
|---|---|---|
| OD-001 | Exact monthly-volume threshold for `SATELLITE` Dealer Location and whether classification is automatic/manual | Project onboarding requirement |
| OD-002 | Whether a Project may eventually contain more than one OEM/product category | Initial baseline assumes one OEM/product category per Project |
| OD-003 | PM vs PMO terminology | User defines PM; spreadsheet uses PMO for some activities |
| OD-004 | Executive exact create/update/approve rights | Executive defined as project leader/boss; detailed rights not supplied |
| OD-005 | Canonical Deal Type list | Process row includes In-Scope, Management Referral, Open Booking, OEM Referral and OOS sub-reasons; current screen shows In-Scope, Out of Scope, Management Referral, OEM Referral |
| OD-006 | Canonical Registration Type names | Process wording and current-screen wording differ; business semantics need normalisation |
| OD-007 | Trade-in ageing threshold: 60 vs 90 days | Process explicitly marks to be confirmed |
| OD-008 | `Total Receipt Realised: PO` formula/lifecycle | Marked pending in source |
| OD-009 | `Total Receipt Realised: DO` logic | Source payment summary marks pending |
| OD-010 | Refund realisation logic | Source payment summary marks pending |
| OD-011 | Short/Excess Standard/Actual formula/tolerance | Source flags confirmation required |
| OD-012 | Per-car Total Discount formula | Source explicitly unresolved |
| OD-013 | Discount Above Scheme formula | Source explicitly unresolved |
| OD-014 | Final Deemed DSA formula/exceptions | Source gives directional categories/exceptions; final rule needs approval |
| OD-015 | Whether the ₹100 Actual-vs-Standard tolerance and Self-deal exclusion are final | Source validation note |
| OD-016 | Final list of mandatory CRM call categories and SLA/timing | Current process contains multiple triggers; detailed SLA not supplied |
| OD-017 | Exact price-component applicability by OEM/model/variant/colour/registration geography | Needs master-data design/input |
| OD-018 | Exact required/conditional document matrix by Project, deal type and process stage | Source provides substantial list but future processes may add/change documents |
| OD-019 | Whether any dealership roles become authenticated Verigence users later | Current requirement has no direct dealership-team dependency |
| OD-020 | External DMS/OEM/insurance-calculator integrations and timing | Not yet supplied; current process permits manual file handoff |

---

## 26. Requirements change control

This document is the **Audit Core Requirements Baseline v1.0**.

Because additional processes will be supplied, future changes SHALL follow this rule:

1. new source process/input is recorded;
2. impacted requirements/use cases are identified;
3. contradictions/open decisions are made explicit;
4. approved requirements are added in a new version (`v1.1`, `v1.2`, etc. or a major version for breaking scope changes);
5. prior baselines remain traceable and are not silently rewritten.

The Solution Design, Logical Data Model and Physical Database Schema SHALL reference the current approved requirements baseline and SHALL NOT invent unresolved business rules.

---

## 27. Next design deliverables

After business review of this baseline, the next deliverables SHALL be produced separately:

1. **Audit Core Solution Design** — module boundaries, aggregates, APIs/events, Security/DI/Observability integration, process/state model and decision on lightweight workflow versus separate workflow capability.
2. **Logical Data Model** — entities, ownership, relationships, cardinality, effective dating/versioning and provenance.
3. **Physical Database Schema** — tables, keys, constraints, indexes, Tenant isolation and migration strategy.
4. **Role/Permission Matrix** — PC/TL/PM/CRM/Executive capability mapping and Security integration needs.
5. **Document Requirement Matrix** — process stage/deal condition/document type requirements aligned with DI.
6. **Business Rule Catalogue** — approved calculations, validations, thresholds, observations and open-rule resolution.
