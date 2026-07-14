# iREPS Conlog Monthly LM Sales Groups Schema

## Document Control

| Item | Value |
|---|---|
| Schema version | `1.0.0` |
| Status | `ACTIVE — ireps-test baseline` |
| Effective date | `2026-07-14` |
| Firestore collection | `conlog_sales_monthly_lm_groups` |

## Purpose

One document summarizes one LM, one month and one monthly meter-sales value group.

## Document identity

```text
conlog_sales_monthly_lm_groups/{lmPcode}__{ym}__{salesGroupId}
```

Example:

```text
conlog_sales_monthly_lm_groups/ZA7423__2026-06__GR3
```

## Canonical document

```json
{
  "lmPcode": "ZA7423",
  "ym": "2026-06",
  "y": 2026,
  "m": 6,
  "salesGroupId": "GR3",
  "salesGroupLabel": "R300.00 to R499.99",
  "metersCount": 2800,
  "purchasesCount": 6450,
  "amountTotalC": 112000000,
  "costC": 97391304,
  "vatC": 14608696,
  "firstPurchaseAtISO": "2026-06-01T00:10:00Z",
  "lastPurchaseAtISO": "2026-06-30T23:40:00Z",
  "firstPurchaseAtMs": 1780265400000,
  "lastPurchaseAtMs": 1782862800000
}
```

Example counts and amounts are illustrative; the deployed Firestore values are authoritative.

## Required fields

```text
lmPcode
ym
y
m
salesGroupId
salesGroupLabel
metersCount
purchasesCount
amountTotalC
costC
vatC
firstPurchaseAtISO
lastPurchaseAtISO
firstPurchaseAtMs
lastPurchaseAtMs
```

## Group contract

```text
GR1  below R100.00
GR2  R100.00 to R299.99
GR3  R300.00 to R499.99
GR4  R500.00 to R999.99
GR5  R1,000.00 and above
```

The five group documents for one LM/month must reconcile to the corresponding LM monthly totals.

## Deployed TEST baseline

```text
Project:      ireps-test
LM:           ZA7423
Coverage:     2025-09 through 2026-06
Documents:    50
Grain:        five group documents per month
Status:       PASS
```
