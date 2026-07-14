# iREPS Conlog Monthly Meter Sales Schema

## Document Control

| Item | Value |
|---|---|
| Schema version | `1.0.0` |
| Status | `ACTIVE — ireps-test baseline` |
| Effective date | `2026-07-14` |
| Firestore collection | `conlog_sales_monthly` |

## Purpose

One document summarizes one meter's approved Atomic Sales for one LM and one month.

## Document identity

```text
conlog_sales_monthly/{lmPcode}__{meterNo}__{ym}
```

Example:

```text
conlog_sales_monthly/ZA7423__04085345850__2026-06
```

## Canonical document

```json
{
  "lmPcode": "ZA7423",
  "meterNo": "04085345850",
  "ym": "2026-06",
  "y": 2026,
  "m": 6,
  "purchasesCount": 8,
  "amountTotalC": 145000,
  "costC": 126087,
  "vatC": 18913,
  "firstPurchaseAtISO": "2026-06-02T08:10:00Z",
  "lastPurchaseAtISO": "2026-06-27T10:35:00Z",
  "firstPurchaseAtMs": 1780387800000,
  "lastPurchaseAtMs": 1782556500000,
  "salesGroupId": "GR5",
  "salesGroupLabel": "R1,000.00 and above"
}
```

## Required fields

```text
lmPcode
meterNo
ym
y
m
purchasesCount
amountTotalC
costC
vatC
firstPurchaseAtISO
lastPurchaseAtISO
firstPurchaseAtMs
lastPurchaseAtMs
salesGroupId
salesGroupLabel
```

## Sales groups

```text
GR1  below R100.00
GR2  R100.00 to R299.99
GR3  R300.00 to R499.99
GR4  R500.00 to R999.99
GR5  R1,000.00 and above
```

## Rules

- Meter identity is a string and preserves leading zeroes.
- `ym` must agree with `y` and `m`.
- All money fields are non-negative integer cents.
- `amountTotalC = costC + vatC`.
- The first and last timestamp pairs describe the earliest and latest included Atomic transactions.
- There is one document per LM + meter + month.

## Deployed TEST baseline

```text
Project:      ireps-test
LM:           ZA7423
Coverage:     2025-09 through 2026-06
Documents:    157,940
Status:       PASS
```
