# iREPS Firestore Schemas

This repository is the authoritative home of versioned iREPS Firestore collection schemas.

## Sales Pipeline schema catalogue

| Folder | Firestore collection | Version | Status |
|---|---|---:|---|
| `conlog-sales-atomic` | `conlog_sales_atomic` | 1.0.0 | ACTIVE — ireps-test |
| `conlog-sales-monthly` | `conlog_sales_monthly` | 1.0.0 | ACTIVE — ireps-test |
| `conlog-sales-monthly-lm` | `conlog_sales_monthly_lm` | 1.0.0 | ACTIVE — ireps-test |
| `conlog-sales-monthly-lm-groups` | `conlog_sales_monthly_lm_groups` | 1.0.0 | ACTIVE — ireps-test |
| `meter-master` | `meter_master` | 1.0.0 | ACTIVE — ireps-test |
| `sales-all-meters` | `sales-all-meters` | 1.0.0 | ACTIVE — ireps-test |

## Current governed baseline

```text
Provider:      Conlog
LM/workbase:   Lesedi / ZA7423
Coverage:      2025-09 through 2026-06
Environment:   ireps-test
Completed:     2026-07-14
```

Code must conform to these schemas. A code change does not silently change a collection schema.

Every schema change requires review, versioning, implementation alignment and a migration decision.
