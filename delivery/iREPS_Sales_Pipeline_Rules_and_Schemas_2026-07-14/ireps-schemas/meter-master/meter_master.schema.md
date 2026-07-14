# iREPS Meter Master Schema

## Document Control

| Item | Value |
|---|---|
| Schema version | `1.0.0` |
| Status | `ACTIVE — ireps-test baseline` |
| Effective date | `2026-07-14` |
| Firestore collection | `meter_master` |

## Purpose

`meter_master` is the thin canonical identity and cross-reference bridge between the sales-side meter universe and iREPS operational meter identity.

It is not a transaction-history, premise, ERF, TRN, status, visibility or service-provider collection.

## Document identity

```text
meter_master/{normalizedMeterNo}
```

The Firestore document ID equals `meterNo.normalized`.

Meter numbers are strings. Normalization removes whitespace, trims, uppercases letters and preserves leading zeroes. There is no fixed meter-number length rule.

## Canonical document

```json
{
  "lmPcode": "ZA7423",
  "meterNo": {
    "raw": "04085345850",
    "normalized": "04085345850"
  },
  "meterType": "electricity",
  "customerNo": "101516969",
  "accountNo": "101516969",
  "refs": {
    "asts": {
      "id": ""
    },
    "sales": {
      "id": "04085345850",
      "provider": "conlog"
    }
  },
  "metadata": {
    "createdAt": "Firestore Timestamp",
    "createdByUid": "SYSTEM",
    "createdByUser": "METER MASTER PIPELINE",
    "updatedAt": "Firestore Timestamp",
    "updatedByUid": "SYSTEM",
    "updatedByUser": "METER MASTER PIPELINE"
  }
}
```

## Canonical root fields

Only these root fields are permitted:

```text
lmPcode
meterNo
meterType
customerNo
accountNo
refs
metadata
```

Meter Master does not carry premise, TRN, status, service-provider or visibility fields.

## References

Only these reference paths are canonical:

```text
refs.asts.id
refs.sales.id
refs.sales.provider
```

A blank reference ID is valid.

## Metadata constitution

`metadata` contains exactly these six fields:

```text
createdAt
createdByUid
createdByUser
updatedAt
updatedByUid
updatedByUser
```

The timestamp values are native Firestore Timestamp values. No additional provenance field belongs at the root or inside metadata in the current locked schema.

## Staging mapping

```text
masterId            -> Firestore document ID
lmPcode             -> lmPcode
meterNoRaw          -> meterNo.raw
meterNoNormalized   -> meterNo.normalized
meterType           -> meterType
customerNo          -> customerNo
accountNo           -> accountNo
astId               -> refs.asts.id
salesId             -> refs.sales.id
salesProvider       -> refs.sales.provider
```

## Duplicate resolution

Duplicate customer-reference rows may be resolved only through the governed identity, account-status and latest-purchase rules.

Customer name, ERF and address are not identity-resolution inputs. Remaining competing identities stop the build.

## Write safety

Initial population uses `create-only` against an empty collection.

Controlled `resume` may create missing documents and skip exact matches, but must stop on conflicts.

Broad merge writes are prohibited. Existing populated `refs.asts.id` values must never be erased by a blank sales staging value.

## Deployed TEST baseline

```text
Project:      ireps-test
LM:           ZA7423
Documents:    35,295
CSV SHA-256:  44a604c0ca06e0f4bb624be69ad20155d07fbb6c903d911d4d19fa3fe084108d
Status:       PASS
```
