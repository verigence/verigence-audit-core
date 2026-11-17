# Verigence business rules register

One line per rule, in the product owner's words where he gave them. Each rule says
what it replaced, every screen and check that reads it, and the job that put the
bookings built before it right. A pull request that changes one of these rules
changes this file in the same pull request (the `rule-note` check asks for it).

Why this file exists (17 Nov 2026, introspection after the NDC receipts issue): the
"payment receipts contain only payment receipts" instruction was given on 4, 11 and
16 Nov and was each time added on top of the existing rule instead of replacing it.
An earlier interpretation was carried forward as if it were the rule. Writing the
rule once, with what it replaced and where it is read, is what stops that.

## How a rule is changed

1. Write the rule in one line below, with the date, and what it replaces. Remove or
   strike the line it replaces; never keep both.
2. List every screen and check that reads it (Deal, Payments, Audit Summary, System
   remarks, tasks, bank reconciliation, exports, analytics). Change all of them, or
   say in the pull request why one is left.
3. Before merging: the pull request's **Figures check** shows what moves on the
   verified bookings and the live sample; open at least three bookings it names and
   check them against their papers. Say which in the pull request (`Checked against
   papers:` line).
4. After deploying: a job puts the bookings built before right. Run it as a dry run
   first (a label without `-commit`), check its result and a sample, then run the
   `-commit` job. The worker refuses a `-commit` job with no dry run of the same kind
   in the last 24 hours.

## Payments and the NDC (Mahindra Orissa)

| Rule | Since | Replaces | Read by | Job for bookings before |
|---|---|---|---|---|
| A payment is a payment receipt on file, and nothing else: no NDC line, no customer-ledger posting is a payment or listed among the receipts. | 17 Nov 2026 | 2–16 Nov: an NDC line / ledger posting with no receipt on file counted as paid | `uc03_p2_journey360._paid`, `payments()` (Payments tab, Deal, Audit Summary, rules), bank reconciliation | `PAYMENT_RECEIPTS_ONLY` (run 17 Nov) |
| The NDC's payments are checked as sums, never line by line: the payment receipts on file, added up, against the money the NDC shows received (customer payments and the loan); short → one task for the receipts not on file. | 17 Nov 2026 | 4–17 Nov: each NDC line matched to a receipt by number then amount (a misread line became a receipt "missing": UTK-SA-261103-012, ₹4,50,000) | `uc03_ledger_receipts.receipts_against_ndc` → PAYMENT_RECEIPT_MISSING, `_awaiting_receipts` (short-payment checks), Audit Summary `ndcPayments` and settlement note, System remarks (loan) | `PAYMENT_RECEIPTS_ONLY` (re-run after the change) |
| The NDC's payment lines must add up to the Total Receipts it prints (within ₹100) before its money is used; otherwise no receipt is asked for, the short-payment checks wait too, and the NDC reading task stands (one cause, one task). An NDC printing no Total Receipts: its payment lines must give its net value instead; one printing neither: no receipt is asked (the short-payment checks still run). | 17 Nov 2026 | — | `ndc_money` (`held`, `heldFor`), `_awaiting_receipts` | same |
| The receipts short by the loan only, with no receipt on file standing for the loan (none linked to it, none of its amount or within 2% under it): no task, a system remark "Loan amount received as per the NDC; no payment receipt available". | 16 Nov 2026 (amount rule 17 Nov) | the loan identified by matching NDC lines to known loan amounts | `receipts_against_ndc` (`loanWithoutReceipt`), `uc03_system_remarks._loan_remarks` | same |
| The NDC's charges less its discounts give its printed totals (within ₹10); otherwise one task to the PC to correct the reading. | 5 Nov 2026 | — | `uc03_statement_totals` (STATEMENT_ROWS_DO_NOT_ADD_UP), Audit Summary AS-11 | — |
| The sum of payment receipts equals the NDC's net value as printed, no calculation. | 26 Oct 2026 | receipts vs invoices | RECEIPTS_EQUAL_INVOICES (`_receipts_equal_statement`) | — |
| Booking is confirmed once the money received reaches the Minimum Booking Amount; receipts carry no Booking/Delivery stage. | 19 Oct 2026 | stage-tagged receipts | stage engine, Deal | — |
| Cash transactions raise no tasks. | 31 Oct 2026 | cash intimation checks | `_BOOKING_RULES` (cash checks off) | cash task clean-up job |
| A receipt issued by an insurer (self insurance) is not a payment receipt. | 11 Nov 2026 | — | receipt issuer check | — |
| A payment up to one day before the booking docket date is an observation, not a finding. | 14 Nov 2026 | — | PAYMENT_BEFORE_BOOKING | — |

## Deal: RTO, scrappage, FASTag and RSA

| Rule | Since | Replaces | Read by | Job for bookings before |
|---|---|---|---|---|
| On a scrappage deal the RTO standard is the price master's RTO (plus the hypothecation fee when financed) less the scrappage concession; the concession is the rebate the RTO challan prints on the motor vehicle tax, else 25% of the standard (within ₹100) when a Certificate of Deposit or a scrappage benefit is on record. The Deal applies it once; nothing else works it out. | 17 Nov 2026 | 9–17 Nov: the concession only inside DEAL_UNDERCHARGED (25% within ₹100 of a standard carrying the HP fee); the Deal and its variance kept the full RTO | `uc03_scrappage_registration.apply_concession` → Deal (RTO line, variance, list price variance), DEAL_UNDERCHARGED, System remarks (SCRAPPAGE_CONCESSION), `DEAL_SCRAPPAGE_REVIEW` | `RULES_RERUN` + `ACCOUNT_STATEMENT` (17 Nov) |
| The Deal shows scrappage: the Certificates of Deposit on file, the challan's rebate, the concession and the scrappage benefits; the RTO took the concession or a benefit was given with no certificate on file → the certificate is asked for in the one missing-documents task. | 17 Nov 2026 | — (certificates shown nowhere on the Deal; certificate asked only when a scrappage bonus was given) | Deal `scrappage` block (Deal tab Scrappage card), `condition_reasons` (`scrappageClaimed`) → DOCUMENT_MISSING | same |
| A charge the NDC prints reaches its Deal line: FASTag written "OTHERS FASTAG 600" / "FAST-TAG"; RSA and the certificate of deposit posted as journals on Aditya's computer statement; the certificate of deposit however misread on the "If any others" / "Misc" row, "COD SALE", a scrappage row charged with no bonus, the CV combined row → other charges. A word on a general row naming no known charge stays unmapped. | 17 Nov 2026 | such rows were dropped or read as other charges | `uc03_account_statement.read_statement` → Deal, statement totals | `ACCOUNT_STATEMENT` |

## Discounts (Excess discount)

| Rule | Since | Replaces | Read by | Job for bookings before |
|---|---|---|---|---|
| A discount read from an NDC / cost sheet whose charges and discounts do not give its own printed totals is not judged as excess: listed apart ("NDC reading in doubt") with why, until the reading is corrected. A PC's closure of the reading task does not change it. | 17 Nov 2026 | — (such figures were judged as read: UTK-SA-261027-004 ₹71,000 vs grid 0–5,000) | `uc03_discount_given.assess` (`in_doubt`), EXCESS_DISCOUNT, KIT_GIVEN_AS_CASH, Excess discount screen, Insights discount report | `DISCOUNT_VARIANCE_RECOMPUTE` |
| The NDC decides the amount given; a Tally ledger or DMS invoice giving another amount is shown beside the row, not a status. | 15 Nov 2026 | 14 Nov: Tally disagreement listed apart | `assess`, readings on each row | — |
| A dealer-grid amount of 0 means no dealer discount is allowed: anything given is excess. | 17 Nov 2026 (confirmed) | — | grid entitlement | — |
| Consumer-scheme cash amounts like 7,001 / 80,001 are the bulletin's own: cash is the total offer less the EW price (7,001 + 17,999 + kit 50,000 = 75,000). Not a misread; never "corrected". | 17 Nov 2026 (checked) | 15 Nov recheck wrongly called them master errors | scheme master import (cash + other = total check) | — |

## Verified bookings

| Rule | Since | Read by |
|---|---|---|
| A Team Lead or Reviewer marks a booking verified once its figures were checked against its papers; the system compares it every night (Jobs page, 9:40 pm), after each deploy and on demand; a change that moves its figures goes live and is reported, it does not block. | 17 Nov 2026 | `uc03_verified_bookings`, Verified bookings page, Journey 360 mark, `pr-figures-check` workflow |
