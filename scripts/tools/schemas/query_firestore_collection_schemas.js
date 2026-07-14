#!/usr/bin/env node
"use strict";

/**
 * iREPS Firestore Schema Query
 *
 * READ-ONLY utility.
 *
 * Reads sample documents from the approved ireps2 sales collections and writes:
 *   - raw sample JSON files
 *   - inferred schema JSON files
 *   - a run manifest
 *
 * Output folder:
 *   C:\dev\ireps-pipeline-sales\docs\queries\ireps2\<timestamp>\
 *
 * Credential:
 *   C:\dev\secrets\ireps2-e72fd9dc94de.json
 */

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const { initializeApp, cert, deleteApp } = require("firebase-admin/app");
const {
  getFirestore,
  FieldPath,
  Timestamp,
  GeoPoint,
  DocumentReference,
} = require("firebase-admin/firestore");

const DEFAULT_PROJECT_ID = "ireps2";

const DEFAULT_SERVICE_ACCOUNT =
  String.raw`C:\dev\secrets\ireps2-e72fd9dc94de.json`;

const DEFAULT_OUTPUT_DIR =
  String.raw`C:\dev\ireps-pipeline-sales\docs\queries`;

const DEFAULT_COLLECTIONS = [
  "conlog_sales_atomic",
  "conlog_sales_monthly",
  "conlog_sales_monthly_lm",
  "conlog_sales_monthly_lm_groups",
  "meter_master",
  "sales-all-meters",
];

function parseArgs(argv) {
  const args = {
    projectId: DEFAULT_PROJECT_ID,
    serviceAccount: DEFAULT_SERVICE_ACCOUNT,
    outputDir: DEFAULT_OUTPUT_DIR,
    sampleSize: 75,
    collections: [...DEFAULT_COLLECTIONS],
  };

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];

    const nextValue = () => {
      index += 1;

      if (index >= argv.length || argv[index].startsWith("--")) {
        throw new Error(`Missing value after ${token}`);
      }

      return argv[index];
    };

    switch (token) {
      case "--project-id":
        args.projectId = nextValue();
        break;

      case "--service-account":
        args.serviceAccount = nextValue();
        break;

      case "--output-dir":
        args.outputDir = nextValue();
        break;

      case "--sample-size":
        args.sampleSize = Number.parseInt(nextValue(), 10);
        break;

      case "--collections":
        args.collections = nextValue()
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean);
        break;

      case "--help":
      case "-h":
        printHelp();
        process.exit(0);
        break;

      default:
        throw new Error(`Unknown argument: ${token}`);
    }
  }

  if (!Number.isInteger(args.sampleSize) || args.sampleSize < 50) {
    throw new Error("--sample-size must be an integer of at least 50.");
  }

  if (args.collections.length === 0) {
    throw new Error("At least one collection must be supplied.");
  }

  return args;
}

function printHelp() {
  console.log(`
iREPS Firestore Schema Query

Usage:
  node .\\scripts\\tools\\query_firestore_schemas.js

Defaults:
  Project:
    ${DEFAULT_PROJECT_ID}

  Credential:
    ${DEFAULT_SERVICE_ACCOUNT}

  Output:
    ${DEFAULT_OUTPUT_DIR}

  Sample size:
    75 documents per collection

Optional arguments:
  --project-id <id>
  --service-account <path>
  --output-dir <path>
  --sample-size <number>
  --collections <comma-separated names>
`);
}

function loadServiceAccount(filePath, expectedProjectId) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`Service-account file not found: ${filePath}`);
  }

  const stats = fs.statSync(filePath);

  if (!stats.isFile()) {
    throw new Error(`Service-account path is not a file: ${filePath}`);
  }

  let serviceAccount;

  try {
    serviceAccount = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    throw new Error(
      `Unable to parse service-account JSON: ${error.message}`
    );
  }

  const actualProjectId = String(
    serviceAccount.project_id || ""
  ).trim();

  if (!actualProjectId) {
    throw new Error(
      "The service-account JSON does not contain project_id."
    );
  }

  if (actualProjectId !== expectedProjectId) {
    throw new Error(
      `Credential project mismatch. Expected "${expectedProjectId}" ` +
        `but found "${actualProjectId}".`
    );
  }

  return serviceAccount;
}

function timestampFolderName(date = new Date()) {
  return date
    .toISOString()
    .replace(/[:.]/g, "-")
    .replace("T", "__");
}

function safeFilename(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "_");
}

function randomAnchor(length = 20) {
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

  const bytes = crypto.randomBytes(length);
  let value = "";

  for (let index = 0; index < length; index += 1) {
    value += alphabet[bytes[index] % alphabet.length];
  }

  return value;
}

function shuffle(values) {
  const copy = [...values];

  for (let index = copy.length - 1; index > 0; index -= 1) {
    const randomIndex = crypto.randomInt(0, index + 1);

    [copy[index], copy[randomIndex]] = [
      copy[randomIndex],
      copy[index],
    ];
  }

  return copy;
}

function serializeFirestoreValue(value) {
  if (value === null || value === undefined) {
    return value ?? null;
  }

  if (value instanceof Timestamp) {
    return {
      __firestoreType: "timestamp",
      iso: value.toDate().toISOString(),
      seconds: value.seconds,
      nanoseconds: value.nanoseconds,
    };
  }

  if (value instanceof Date) {
    return {
      __firestoreType: "date",
      iso: value.toISOString(),
    };
  }

  if (value instanceof GeoPoint) {
    return {
      __firestoreType: "geopoint",
      latitude: value.latitude,
      longitude: value.longitude,
    };
  }

  if (value instanceof DocumentReference) {
    return {
      __firestoreType: "document_reference",
      path: value.path,
    };
  }

  if (Buffer.isBuffer(value)) {
    return {
      __firestoreType: "bytes",
      byteLength: value.length,
      base64: value.toString("base64"),
    };
  }

  if (Array.isArray(value)) {
    return value.map(serializeFirestoreValue);
  }

  if (typeof value === "object") {
    const output = {};

    for (const [key, childValue] of Object.entries(value)) {
      output[key] = serializeFirestoreValue(childValue);
    }

    return output;
  }

  if (typeof value === "number" && !Number.isFinite(value)) {
    return {
      __firestoreType: "non_finite_number",
      value: String(value),
    };
  }

  return value;
}

function firestoreType(value) {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (value instanceof Timestamp) return "timestamp";
  if (value instanceof Date) return "date";
  if (value instanceof GeoPoint) return "geopoint";
  if (value instanceof DocumentReference) return "document_reference";
  if (Buffer.isBuffer(value)) return "bytes";
  if (Array.isArray(value)) return "array";

  if (typeof value === "number") {
    return Number.isInteger(value) ? "integer" : "double";
  }

  if (typeof value === "object") return "map";

  return typeof value;
}

function compactExample(value, maximumLength = 240) {
  let text;

  try {
    text = JSON.stringify(serializeFirestoreValue(value));
  } catch {
    text = String(value);
  }

  if (text.length > maximumLength) {
    return `${text.slice(0, maximumLength)}…`;
  }

  return text;
}

function ensureFieldStats(fieldMap, fieldPath) {
  if (!fieldMap.has(fieldPath)) {
    fieldMap.set(fieldPath, {
      path: fieldPath,
      presentCount: 0,
      nullCount: 0,
      observedTypes: new Map(),
      examples: [],
    });
  }

  return fieldMap.get(fieldPath);
}

function addType(stats, typeName) {
  stats.observedTypes.set(
    typeName,
    (stats.observedTypes.get(typeName) || 0) + 1
  );
}

function addExample(stats, value) {
  const example = compactExample(value);

  if (
    stats.examples.length < 5 &&
    !stats.examples.includes(example)
  ) {
    stats.examples.push(example);
  }
}

function analyseValue(
  fieldMap,
  fieldPath,
  value,
  seenInDocument
) {
  const stats = ensureFieldStats(fieldMap, fieldPath);

  if (!seenInDocument.has(fieldPath)) {
    stats.presentCount += 1;
    seenInDocument.add(fieldPath);
  }

  addType(stats, firestoreType(value));
  addExample(stats, value);

  if (value === null) {
    stats.nullCount += 1;
  }

  if (Array.isArray(value)) {
    const arrayPath = `${fieldPath}[]`;

    if (value.length === 0) {
      const arrayStats = ensureFieldStats(fieldMap, arrayPath);

      if (!seenInDocument.has(arrayPath)) {
        arrayStats.presentCount += 1;
        seenInDocument.add(arrayPath);
      }

      addType(arrayStats, "empty_array");
      addExample(arrayStats, []);
    }

    for (const element of value) {
      analyseValue(
        fieldMap,
        arrayPath,
        element,
        seenInDocument
      );
    }

    return;
  }

  if (
    value &&
    typeof value === "object" &&
    !(value instanceof Timestamp) &&
    !(value instanceof Date) &&
    !(value instanceof GeoPoint) &&
    !(value instanceof DocumentReference) &&
    !Buffer.isBuffer(value)
  ) {
    for (const [childKey, childValue] of Object.entries(value)) {
      analyseValue(
        fieldMap,
        `${fieldPath}.${childKey}`,
        childValue,
        seenInDocument
      );
    }
  }
}

function inferSchema(
  collectionName,
  snapshots,
  samplingMetadata
) {
  const fieldMap = new Map();

  for (const snapshot of snapshots) {
    const data = snapshot.data() || {};
    const seenInDocument = new Set();

    for (const [fieldName, value] of Object.entries(data)) {
      analyseValue(
        fieldMap,
        fieldName,
        value,
        seenInDocument
      );
    }
  }

  const sampleCount = snapshots.length;

  const fields = [...fieldMap.values()]
    .map((stats) => ({
      path: stats.path,
      presentCount: stats.presentCount,
      missingCount: sampleCount - stats.presentCount,
      presencePercent:
        sampleCount === 0
          ? 0
          : Number(
              (
                (stats.presentCount / sampleCount) *
                100
              ).toFixed(2)
            ),
      nullCount: stats.nullCount,
      observedTypes: Object.fromEntries(
        [...stats.observedTypes.entries()].sort(
          ([left], [right]) => left.localeCompare(right)
        )
      ),
      examples: stats.examples,
    }))
    .sort((left, right) =>
      left.path.localeCompare(right.path)
    );

  return {
    collection: collectionName,
    projectId: DEFAULT_PROJECT_ID,
    generatedAtISO: new Date().toISOString(),
    sampleCount,
    fieldCount: fields.length,
    sampling: samplingMetadata,
    fields,
  };
}

async function addQueryCandidates(
  candidateMap,
  query,
  sourceLabel
) {
  const snapshot = await query.get();

  for (const documentSnapshot of snapshot.docs) {
    if (!candidateMap.has(documentSnapshot.id)) {
      candidateMap.set(documentSnapshot.id, {
        snapshot: documentSnapshot,
        sources: new Set([sourceLabel]),
      });
    } else {
      candidateMap
        .get(documentSnapshot.id)
        .sources.add(sourceLabel);
    }
  }

  return snapshot.size;
}

async function sampleCollection(
  database,
  collectionName,
  sampleSize
) {
  const collectionReference =
    database.collection(collectionName);

  const documentId = FieldPath.documentId();
  const candidateMap = new Map();
  const queryStats = [];

  const firstWindowSize = await addQueryCandidates(
    candidateMap,
    collectionReference
      .orderBy(documentId, "asc")
      .limit(Math.min(sampleSize, 25)),
    "first_window"
  );

  queryStats.push({
    source: "first_window",
    returned: firstWindowSize,
  });

  for (
    let windowIndex = 0;
    windowIndex < 24;
    windowIndex += 1
  ) {
    const anchor = randomAnchor();
    const source = `random_window_${windowIndex + 1}`;

    const returned = await addQueryCandidates(
      candidateMap,
      collectionReference
        .orderBy(documentId, "asc")
        .startAt(anchor)
        .limit(15),
      source
    );

    queryStats.push({
      source,
      anchor,
      returned,
    });

    if (candidateMap.size >= sampleSize * 4) {
      break;
    }
  }

  if (candidateMap.size < sampleSize) {
    const fallbackLimit = Math.max(sampleSize * 4, 200);

    const fallbackSize = await addQueryCandidates(
      candidateMap,
      collectionReference
        .orderBy(documentId, "asc")
        .limit(fallbackLimit),
      "bounded_fallback_pool"
    );

    queryStats.push({
      source: "bounded_fallback_pool",
      returned: fallbackSize,
    });
  }

  const candidates = [...candidateMap.values()].map(
    (entry) => entry.snapshot
  );

  const selected = shuffle(candidates).slice(0, sampleSize);

  return {
    snapshots: selected,
    metadata: {
      method: "ascending_document_id_windows",
      note:
        "Bounded read-only schema sample. " +
        "This is suitable for schema discovery but is not a " +
        "statistically uniform sample of the collection.",
      requestedSampleSize: sampleSize,
      returnedSampleSize: selected.length,
      uniqueCandidateCount: candidates.length,
      queryStats,
    },
  };
}

function writeJson(filePath, value) {
  fs.writeFileSync(
    filePath,
    `${JSON.stringify(value, null, 2)}\n`,
    "utf8"
  );
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  const serviceAccount = loadServiceAccount(
    args.serviceAccount,
    args.projectId
  );

  const application = initializeApp({
    credential: cert(serviceAccount),
    projectId: args.projectId,
  });

  const database = getFirestore(application);

  const runStartedAt = new Date();

  const runFolder = path.join(
    args.outputDir,
    args.projectId,
    timestampFolderName(runStartedAt)
  );

  fs.mkdirSync(runFolder, { recursive: true });

  console.log("======================================================");
  console.log("iREPS FIRESTORE SCHEMA QUERY");
  console.log("READ-ONLY MODE");
  console.log("======================================================");
  console.log(`Project         : ${args.projectId}`);
  console.log(`Credential file : ${args.serviceAccount}`);
  console.log(`Output parent   : ${args.outputDir}`);
  console.log(`Output run      : ${runFolder}`);
  console.log(`Sample size     : ${args.sampleSize}`);
  console.log(`Collections     : ${args.collections.join(", ")}`);

  const manifest = {
    tool: "iREPS Firestore Schema Query",
    mode: "read-only",
    projectId: args.projectId,
    serviceAccountProjectId: serviceAccount.project_id,
    serviceAccountPathUsed: args.serviceAccount,
    serviceAccountCopiedToOutput: false,
    outputParent: args.outputDir,
    outputRunFolder: runFolder,
    startedAtISO: runStartedAt.toISOString(),
    completedAtISO: null,
    requestedSampleSizePerCollection: args.sampleSize,
    collections: [],
  };

  let hadErrors = false;

  for (const collectionName of args.collections) {
    console.log(`\n[COLLECTION] ${collectionName}`);

    try {
      const { snapshots, metadata } =
        await sampleCollection(
          database,
          collectionName,
          args.sampleSize
        );

      const safeCollectionName =
        safeFilename(collectionName);

      const sampleFile = path.join(
        runFolder,
        `${safeCollectionName}__sample.json`
      );

      const schemaFile = path.join(
        runFolder,
        `${safeCollectionName}__schema.json`
      );

      const samplePayload = {
        collection: collectionName,
        projectId: args.projectId,
        generatedAtISO: new Date().toISOString(),
        sampleCount: snapshots.length,
        sampling: metadata,
        documents: snapshots.map((snapshot) => ({
          id: snapshot.id,
          path: snapshot.ref.path,
          data: serializeFirestoreValue(snapshot.data()),
        })),
      };

      const schemaPayload = inferSchema(
        collectionName,
        snapshots,
        metadata
      );

      writeJson(sampleFile, samplePayload);
      writeJson(schemaFile, schemaPayload);

      const status =
        snapshots.length >= 50
          ? "OK"
          : snapshots.length === 0
            ? "EMPTY_OR_NOT_FOUND"
            : "FEWER_THAN_50_DOCUMENTS";

      console.log(`  Candidates : ${metadata.uniqueCandidateCount}`);
      console.log(`  Sampled    : ${snapshots.length}`);
      console.log(`  Status     : ${status}`);
      console.log(`  Raw JSON   : ${sampleFile}`);
      console.log(`  Schema JSON: ${schemaFile}`);

      manifest.collections.push({
        collection: collectionName,
        status,
        sampleCount: snapshots.length,
        uniqueCandidateCount:
          metadata.uniqueCandidateCount,
        sampleFile,
        schemaFile,
      });
    } catch (error) {
      hadErrors = true;

      console.error(`  [ERROR] ${error.message}`);

      manifest.collections.push({
        collection: collectionName,
        status: "ERROR",
        error: error.message,
      });
    }
  }

  manifest.completedAtISO = new Date().toISOString();

  const manifestFile = path.join(
    runFolder,
    "_run_manifest.json"
  );

  writeJson(manifestFile, manifest);

  console.log("\n======================================================");
  console.log("SCHEMA QUERY COMPLETE");
  console.log("======================================================");
  console.log(`Manifest: ${manifestFile}`);

  await deleteApp(application);

  if (hadErrors) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(`\n[FATAL] ${error.stack || error.message}`);
  process.exitCode = 1;
});
