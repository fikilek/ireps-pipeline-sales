# iREPS Conlog Monthly LM Sales Schema

## Document Control

| Item | Value |
|---|---|
| Schema version | `1.0.0` |
| Status | `ACTIVE — ireps-test baseline` |
| Effective date | `2026-07-14` |
| Firestore collection | `conlog_sales_monthly_lm` |

## Purpose

One document summarizes all approved Atomic Sales for one LM/workbase and one month.

## Document identity

```text
conlog_sales_monthly_lm/{lmPcode}__{ym}
```

Example:

```text
conlog_sales_monthly_lm/ZA7423__2026-06
```

## Canonical document

```json
{
  "lmPcode": "ZA7423",
  "ym": "2026-06",
  "y": 2026,
  "m": 6,
  "purchasesCount": 85375,
  "metersCount": 16089,
  "amountTotalC": 1000000000,
  "costC": 869565217,
  "vatC": 130434783,
  "firstPurchaseAtISO": "2026-06-01T00:01:00Z",
  "lastPurchaseAtISO": "2026-06-30T23:59:00Z",
  "firstPurchaseAtMs": 1780264860000,
  "lastPurchaseAtMs": 1782863940000
}
```

Example amounts are illustrative; the deployed Firestore values are authoritative.

## Required fields

```text
lmPcode
ym
y
m
purchasesCount
metersCount
amountTotalC
costC
vatC
firstPurchaseAtISO
lastPurchaseAtISO
firstPurchaseAtMs
lastPurchaseAtMs
```

## Reconciliation rules

For the same LM and month:

- `purchasesCount` equals the Atomic transaction count;
- `metersCount` equals the unique Atomic meter count;
- amount, cost and VAT equal the Atomic totals;
- `amountTotalC = costC + vatC`;
- first and last timestamps span the included Atomic transactions.

## Deployed TEST baseline

```text
Project:      ireps-test
LM:           ZA7423
Coverage:     2025-09 through 2026-06
Documents:    10
Grain:        one LM document per month
Status:       PASS
```
