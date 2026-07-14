# iREPS Firestore Schema Sampler — Index-Free Version

This is a read-only schema snapshot tool for the `ireps2` Firestore project.

It does **not** require new Firestore indexes. It avoids descending document-ID queries and samples through ascending document-ID windows.

## Install

From `C:\dev\ireps-pipeline-sales`:

```powershell
npm install firebase-admin
```

## Place script

Copy `query_firestore_collection_schemas.js` to:

```text
C:\dev\ireps-pipeline-sales\scripts\tools\query_firestore_collection_schemas.js
```

## Run

```powershell
node .\scripts\tools\query_firestore_collection_schemas.js
```

Default outputs are written under:

```text
C:\docs\queries\ireps2\<timestamp>\
```

The service-account file is read from the configured local secrets path and is never copied into the output.
