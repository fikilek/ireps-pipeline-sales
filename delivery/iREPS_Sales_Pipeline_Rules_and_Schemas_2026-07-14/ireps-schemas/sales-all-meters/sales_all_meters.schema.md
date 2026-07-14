# iREPS Sales All Meters Schema

## Document Control

| Item | Value |
|---|---|
| Schema version | `1.0.0` |
| Status | `ACTIVE — ireps-test baseline` |
| Effective date | `2026-07-14` |
| Firestore collection | `sales-all-meters` |

## Purpose

`sales-all-meters` provides one ready-to-use sales-awareness profile for every approved Meter Master record, including meters with no sales in the selected period.

It is a derived supporting projection, not the Atomic source of truth.

## Document identity

```text
sales-all-meters/{masterId}
```

`masterId` equals the normalized meter identity and the corresponding Meter Master document ID.

## Canonical document

```json
{
  "master": {
    "id": "04085345850",
    "visibility": "INVISIBLE"
  },
  "meterNo": "04085345850",
  "meterNoNormalized": "04085345850",
  "provider": "conlog",
  "customerNo": "101517546",
  "accountNo": "101517546",
  "totalAmountC": 125000,
  "monthlyTotalsC": {
    "2025-09": 10000,
    "2025-10": 15000,
    "2025-11": 0,
    "2025-12": 20000,
    "2026-01": 10000,
    "2026-02": 15000,
    "2026-03": 10000,
    "2026-04": 15000,
    "2026-05": 10000,
    "2026-06": 20000
  },
  "lastPurchaseAtISO": "2026-06-27T10:35:00Z",
  "daysSinceLastPurchase": 17
}
```

## Field contract

| Field | Type | Required | Rule |
|---|---:|---:|---|
| `master.id` | string | yes | Equals Firestore document ID |
| `master.visibility` | string | yes | `VISIBLE` or `INVISIBLE` supporting projection |
| `meterNo` | string | yes | Meter Master raw meter value |
| `meterNoNormalized` | string | yes | Equals document ID |
| `provider` | string | yes | Current value `conlog` |
| `customerNo` | string | yes | Empty string when unknown |
| `accountNo` | string | yes | Empty string when unknown |
| `totalAmountC` | integer | yes | Sum of every monthly total |
| `monthlyTotalsC` | map | yes | Dynamic `YYYY-MM` integer-cent keys |
| `lastPurchaseAtISO` | string or null | yes | Latest included purchase, or null |
| `daysSinceLastPurchase` | integer or null | yes | As-of-date difference, or null |

## Dynamic month rule

`monthlyTotalsC` is generated from the explicit approved continuous source range. Code must not assume a fixed historical month list.

The completed Lesedi TEST baseline contains keys from `2025-09` through `2026-06`.

## Zero-sales rule

Every Meter Master identity remains present. A meter with no included sales uses zero for `totalAmountC` and every monthly total, with null last-purchase fields.

## Visibility rule

```text
Meter Master astId populated -> VISIBLE
Meter Master astId blank     -> INVISIBLE
```

This is a supporting projection and must not be written back as canonical Meter Master state.

## Reconciliation rules

```text
totalAmountC = sum(monthlyTotalsC values)
```

The Sales All Meters document count must equal the approved Meter Master row count for the same build.

## Upload safety

Normal mode is `create-only` against an empty collection.

`resume` is restricted to verified recovery from a partial upload of the same frozen CSV.

Create operations are required. Conflicts and unexpected extra documents block the upload. Broad merge writes are prohibited.

The uploader records the CSV SHA-256 and writes a JSON run report.

## Metadata rule

The current TEST schema does not add a `metadata` object to Sales All Meters documents. A future metadata addition requires a versioned schema change and coordinated migration.

## Deployed TEST baseline

```text
Project:              ireps-test
LM:                   ZA7423
Coverage:             2025-09 through 2026-06
Documents:            35,295
Meters with sales:    19,904
Meters without sales: 15,391
Total amount cents:   9,728,029,408
CSV SHA-256:          139e1775ed4404696077ccf5df4355288eabcb0357fbb7ddeebe578d69179087
Status:               PASS
```
