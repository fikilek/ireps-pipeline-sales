# iREPS Conlog Atomic Sales Schema

## Document Control

| Item | Value |
|---|---|
| Schema version | `1.0.0` |
| Status | `ACTIVE — ireps-test baseline` |
| Effective date | `2026-07-14` |
| Firestore collection | `conlog_sales_atomic` |

## Purpose

One document represents one approved normalized Conlog vending transaction. Atomic Sales is the detailed sales source of truth.

## Document identity

```text
conlog_sales_atomic/{atomicId}
```

`atomicId` is deterministic and is not repeated inside the Firestore document.

## Canonical document

```json
{
  "vendingProviderId": "vpr_7f4d3c91a2b84e6f",
  "lmPcode": "ZA7423",
  "meterNo": "04085345850",
  "txAtISO": "2026-06-27T10:35:00Z",
  "txAtMs": 1782556500000,
  "ym": "2026-06",
  "y": 2026,
  "m": 6,
  "amountTotalC": 20000,
  "costC": 17391,
  "vatC": 2609,
  "currency": "ZAR",
  "sourceFileId": "conlog_prepaid_sales__ZA7423__2026-06.csv",
  "sourceRow": 1,
  "ingestedAtISO": "2026-07-14T09:00:00Z",
  "ingestedAtMs": 1784022000000
}
```

## Required fields

```text
vendingProviderId
lmPcode
meterNo
txAtISO
txAtMs
ym
y
m
amountTotalC
costC
vatC
currency
sourceFileId
sourceRow
ingestedAtISO
ingestedAtMs
```

## Rules

- Meter identity is a string and preserves leading zeroes.
- ISO and millisecond timestamp pairs must represent the same time.
- `ym`, `y` and `m` must agree.
- All monetary values are non-negative integer cents.
- `amountTotalC = costC + vatC`.
- `currency = ZAR`.
- `sourceRow` is a positive source sequence.
- Normal uploads use Firestore create operations, not merge or update.
- `resume` is restricted to verified recovery from the same approved CSV.

## Deployed TEST baseline

```text
Project:      ireps-test
LM:           ZA7423
Coverage:     2025-09 through 2026-06
Documents:    822,527
Status:       PASS
```
