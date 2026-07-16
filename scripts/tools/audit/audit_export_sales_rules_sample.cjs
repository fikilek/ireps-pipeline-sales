#!/usr/bin/env node
"use strict";

/**
 * iREPS Sales Rules Firestore Audit Exporter
 *
 * READ-ONLY.
 *
 * This version uses only ascending document-ID queries.
 * It does not require custom Firestore indexes and does not scan
 * complete large collections.
 *
 * Large collections:
 *   - Build a bounded candidate pool using ascending document-ID windows.
 *   - Export a deterministic pseudo-random sample of at most 300 documents.
 *
 * Small collections:
 *   - Export every document.
 *
 * Default project:
 *   ireps-test
 *
 * Default credential:
 *   C:\dev\secrets\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json
 *
 * Default output:
 *   C:\dev\ireps-pipeline-sales\audit-private\
 *   sales_rules_sample_ireps-test_20260716
 */

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const {
  cert,
  deleteApp,
  initializeApp,
} = require("firebase-admin/app");

const {
  DocumentReference,
  FieldPath,
  GeoPoint,
  Timestamp,
  getFirestore,
} = require("firebase-admin/firestore");

const DEFAULT_PROJECT_ID = "ireps-test";
const DEFAULT_SERVICE_ACCOUNT =
  String.raw`C:\dev\secrets\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json`;
const DEFAULT_OUTPUT_DIR =
  String.raw`C:\dev\ireps-pipeline-sales\audit-private\sales_rules_sample_ireps-test_20260716`;
const DEFAULT_RANDOM_SEED = "20260716";
const DEFAULT_SAMPLE_SIZE = 300;
const DEFAULT_WINDOW_COUNT = 80;
const DEFAULT_WINDOW_SIZE = 8;
const DEFAULT_INITIAL_WINDOW_SIZE = 50;
const DEFAULT_FALLBACK_POOL_SIZE = 1200;

const COLLECTIONS = [
  {
    name: "conlog_sales_atomic",
    mode: "sample",
    fileName: "conlog_sales_atomic__sample_300.json",
  },
  {
    name: "conlog_sales_monthly",
    mode: "sample",
    fileName: "conlog_sales_monthly__sample_300.json",
  },
  {
    name: "conlog_sales_monthly_lm",
    mode: "all",
    fileName: "conlog_sales_monthly_lm__all.json",
  },
  {
    name: "conlog_sales_monthly_lm_groups",
    mode: "all",
    fileName: "conlog_sales_monthly_lm_groups__all.json",
  },
  {
    name: "meter_master",
    mode: "sample",
    fileName: "meter_master__sample_300.json",
  },
  {
    name: "sales-all-meters",
    mode: "sample",
    fileName: "sales-all-meters__sample_300.json",
  },
];

function parseArgs(argv) {
  const args = {
    projectId: DEFAULT_PROJECT_ID,
    serviceAccountPath: DEFAULT_SERVICE_ACCOUNT,
    outputDir: DEFAULT_OUTPUT_DIR,
    randomSeed: DEFAULT_RANDOM_SEED,
    sampleSize: DEFAULT_SAMPLE_SIZE,
    windowCount: DEFAULT_WINDOW_COUNT,
    windowSize: DEFAULT_WINDOW_SIZE,
    initialWindowSize: DEFAULT_INITIAL_WINDOW_SIZE,
    fallbackPoolSize: DEFAULT_FALLBACK_POOL_SIZE,
  };

  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];

    if (token === "--project") {
      args.projectId = requireNextValue(argv, ++index, token);
    } else if (token === "--service-account") {
      args.serviceAccountPath = path.resolve(
        requireNextValue(argv, ++index, token),
      );
    } else if (token === "--out") {
      args.outputDir = path.resolve(
        requireNextValue(argv, ++index, token),
      );
    } else if (token === "--seed") {
      args.randomSeed = requireNextValue(argv, ++index, token);
    } else if (token === "--sample-size") {
      args.sampleSize = parsePositiveInteger(
        requireNextValue(argv, ++index, token),
        token,
      );
    } else if (token === "--window-count") {
      args.windowCount = parsePositiveInteger(
        requireNextValue(argv, ++index, token),
        token,
      );
    } else if (token === "--window-size") {
      args.windowSize = parsePositiveInteger(
        requireNextValue(argv, ++index, token),
        token,
      );
    } else if (token === "--fallback-pool-size") {
      args.fallbackPoolSize = parsePositiveInteger(
        requireNextValue(argv, ++index, token),
        token,
      );
    } else if (token === "--help" || token === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${token}`);
    }
  }

  if (args.fallbackPoolSize < args.sampleSize) {
    throw new Error(
      "--fallback-pool-size must be at least --sample-size.",
    );
  }

  return args;
}

function requireNextValue(argv, index, optionName) {
  const value = argv[index];

  if (!value || value.startsWith("--")) {
    throw new Error(`${optionName} requires a value.`);
  }

  return value;
}

function parsePositiveInteger(value, optionName) {
  const parsed = Number.parseInt(value, 10);

  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${optionName} must be a positive integer.`);
  }

  return parsed;
}

function printHelp() {
  console.log(`
Usage:
  node audit_export_sales_rules_sample.cjs [options]

Options:
  --project <projectId>
  --service-account <jsonPath>
  --out <folder>
  --seed <value>
  --sample-size <number>
  --window-count <number>
  --window-size <number>
  --fallback-pool-size <number>
  --help

Defaults:
  Project:
    ${DEFAULT_PROJECT_ID}

  Credential:
    ${DEFAULT_SERVICE_ACCOUNT}

  Output:
    ${DEFAULT_OUTPUT_DIR}

  Sample size:
    ${DEFAULT_SAMPLE_SIZE}

  Candidate windows:
    ${DEFAULT_WINDOW_COUNT} windows x ${DEFAULT_WINDOW_SIZE} documents

  Bounded fallback pool:
    first ${DEFAULT_FALLBACK_POOL_SIZE} documents
`);
}

function loadServiceAccount(serviceAccountPath, expectedProjectId) {
  if (!fs.existsSync(serviceAccountPath)) {
    throw new Error(
      `Service-account file does not exist: ${serviceAccountPath}`,
    );
  }

  const stats = fs.statSync(serviceAccountPath);

  if (!stats.isFile()) {
    throw new Error(
      `Service-account path is not a file: ${serviceAccountPath}`,
    );
  }

  let serviceAccount;

  try {
    serviceAccount = JSON.parse(
      fs.readFileSync(serviceAccountPath, "utf8"),
    );
  } catch (error) {
    throw new Error(
      `Unable to parse service-account JSON: ${error.message}`,
    );
  }

  const credentialProjectId = String(
    serviceAccount?.project_id ?? "",
  ).trim();

  if (!credentialProjectId) {
    throw new Error(
      "The service-account JSON does not contain project_id.",
    );
  }

  if (credentialProjectId !== expectedProjectId) {
    throw new Error(
      `Credential project mismatch. Expected "${expectedProjectId}" ` +
        `but found "${credentialProjectId}".`,
    );
  }

  return serviceAccount;
}

function initialiseFirestore({ projectId, serviceAccountPath }) {
  const serviceAccount = loadServiceAccount(
    serviceAccountPath,
    projectId,
  );

  const application = initializeApp({
    credential: cert(serviceAccount),
    projectId,
  });

  return {
    application,
    database: getFirestore(application),
    credentialProjectId: serviceAccount.project_id,
  };
}

function createSeededRandom(seedText) {
  const digest = crypto
    .createHash("sha256")
    .update(String(seedText), "utf8")
    .digest();

  let state = digest.readUInt32LE(0) || 0x6d2b79f5;

  return function random() {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);

    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function deterministicShuffle(values, random) {
  const copy = [...values];

  for (let index = copy.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(random() * (index + 1));

    [copy[index], copy[swapIndex]] = [
      copy[swapIndex],
      copy[index],
    ];
  }

  return copy;
}

function serializeFirestoreValue(value) {
  if (value === null) {
    return null;
  }

  if (value === undefined) {
    return {
      __type: "undefined",
    };
  }

  if (value instanceof Timestamp) {
    return {
      __type: "firestore_timestamp",
      iso: value.toDate().toISOString(),
      seconds: value.seconds,
      nanoseconds: value.nanoseconds,
    };
  }

  if (value instanceof Date) {
    return {
      __type: "date",
      iso: value.toISOString(),
    };
  }

  if (value instanceof GeoPoint) {
    return {
      __type: "firestore_geopoint",
      latitude: value.latitude,
      longitude: value.longitude,
    };
  }

  if (value instanceof DocumentReference) {
    return {
      __type: "firestore_document_reference",
      path: value.path,
    };
  }

  if (Buffer.isBuffer(value)) {
    return {
      __type: "bytes",
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

  return value;
}

function toExportDocument(snapshot) {
  return {
    id: snapshot.id,
    path: snapshot.ref.path,
    data: serializeFirestoreValue(snapshot.data()),
  };
}

function addSnapshots(candidateMap, snapshots, source) {
  for (const snapshot of snapshots) {
    if (!candidateMap.has(snapshot.id)) {
      candidateMap.set(snapshot.id, {
        snapshot,
        sources: new Set([source]),
      });
    } else {
      candidateMap.get(snapshot.id).sources.add(source);
    }
  }
}

function commonPrefix(values) {
  if (values.length === 0) {
    return "";
  }

  let prefix = values[0];

  for (const value of values.slice(1)) {
    while (prefix && !value.startsWith(prefix)) {
      prefix = prefix.slice(0, -1);
    }

    if (!prefix) {
      break;
    }
  }

  return prefix;
}

function inferAnchorProfile(documentIds) {
  const ids = documentIds
    .map((value) => String(value))
    .filter(Boolean);

  if (ids.length === 0) {
    return {
      type: "generic",
      prefix: "",
      length: 20,
      alphabet:
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    };
  }

  if (ids.every((id) => /^\d+$/.test(id))) {
    return {
      type: "numeric",
      prefix: "",
      length: Math.max(...ids.map((id) => id.length)),
      alphabet: "0123456789",
    };
  }

  if (
    ids.every((id) => /^[0-9a-f]+$/i.test(id)) &&
    new Set(ids.map((id) => id.length)).size === 1
  ) {
    return {
      type: "hex",
      prefix: "",
      length: ids[0].length,
      alphabet: "0123456789abcdef",
    };
  }

  const prefix = commonPrefix(ids);
  const suffixes = ids.map((id) => id.slice(prefix.length));
  const observedCharacters = new Set();

  for (const suffix of suffixes) {
    for (const character of suffix) {
      observedCharacters.add(character);
    }
  }

  const alphabet = [...observedCharacters]
    .filter((character) => /[0-9A-Za-z_-]/.test(character))
    .sort()
    .join("");

  return {
    type: "prefixed_or_generic",
    prefix,
    length: Math.max(
      8,
      ...suffixes.map((suffix) => suffix.length),
    ),
    alphabet:
      alphabet ||
      "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
  };
}

function randomText(length, alphabet, random) {
  let output = "";

  for (let index = 0; index < length; index += 1) {
    output += alphabet[
      Math.floor(random() * alphabet.length)
    ];
  }

  return output;
}

function generateAnchor(profile, random) {
  if (profile.type === "numeric") {
    return randomText(
      profile.length,
      "0123456789",
      random,
    );
  }

  if (profile.type === "hex") {
    return randomText(
      profile.length,
      "0123456789abcdef",
      random,
    );
  }

  return (
    profile.prefix +
    randomText(
      profile.length,
      profile.alphabet,
      random,
    )
  );
}

async function getPopulationCount(collectionReference) {
  const aggregateSnapshot =
    await collectionReference.count().get();

  return Number(aggregateSnapshot.data().count);
}

async function boundedSampleCollection({
  database,
  collectionName,
  sampleSize,
  randomSeed,
  windowCount,
  windowSize,
  initialWindowSize,
  fallbackPoolSize,
}) {
  const collectionReference =
    database.collection(collectionName);

  const populationCount =
    await getPopulationCount(collectionReference);

  if (populationCount === 0) {
    return {
      populationCount,
      candidateCount: 0,
      documents: [],
      profile: null,
      queryStats: [],
      fallbackUsed: false,
    };
  }

  const documentId = FieldPath.documentId();
  const random = createSeededRandom(
    `${randomSeed}:${collectionName}:anchors`,
  );
  const candidateMap = new Map();
  const queryStats = [];

  const initialSnapshot = await collectionReference
    .orderBy(documentId, "asc")
    .limit(initialWindowSize)
    .get();

  addSnapshots(
    candidateMap,
    initialSnapshot.docs,
    "initial_window",
  );

  queryStats.push({
    source: "initial_window",
    returned: initialSnapshot.size,
  });

  const profile = inferAnchorProfile(
    initialSnapshot.docs.map((snapshot) => snapshot.id),
  );

  for (
    let windowIndex = 0;
    windowIndex < windowCount;
    windowIndex += 1
  ) {
    const anchor = generateAnchor(profile, random);

    const snapshot = await collectionReference
      .orderBy(documentId, "asc")
      .startAt(anchor)
      .limit(windowSize)
      .get();

    addSnapshots(
      candidateMap,
      snapshot.docs,
      `random_window_${windowIndex + 1}`,
    );

    queryStats.push({
      source: `random_window_${windowIndex + 1}`,
      anchor,
      returned: snapshot.size,
    });
  }

  let fallbackUsed = false;

  if (
    candidateMap.size <
    Math.min(sampleSize, populationCount)
  ) {
    fallbackUsed = true;

    const fallbackSnapshot = await collectionReference
      .orderBy(documentId, "asc")
      .limit(
        Math.min(fallbackPoolSize, populationCount),
      )
      .get();

    addSnapshots(
      candidateMap,
      fallbackSnapshot.docs,
      "bounded_fallback_pool",
    );

    queryStats.push({
      source: "bounded_fallback_pool",
      returned: fallbackSnapshot.size,
    });
  }

  const candidates = [...candidateMap.values()]
    .map((entry) => entry.snapshot);

  const selected = deterministicShuffle(
    candidates,
    createSeededRandom(
      `${randomSeed}:${collectionName}:selection`,
    ),
  )
    .slice(0, Math.min(sampleSize, populationCount))
    .map(toExportDocument)
    .sort((left, right) =>
      left.id.localeCompare(right.id),
    );

  return {
    populationCount,
    candidateCount: candidates.length,
    documents: selected,
    profile,
    queryStats,
    fallbackUsed,
  };
}

async function exportCompleteCollection({
  database,
  collectionName,
}) {
  const snapshot = await database
    .collection(collectionName)
    .orderBy(FieldPath.documentId(), "asc")
    .get();

  return {
    populationCount: snapshot.size,
    candidateCount: snapshot.size,
    documents: snapshot.docs.map(toExportDocument),
    profile: null,
    queryStats: [
      {
        source: "complete_collection",
        returned: snapshot.size,
      },
    ],
    fallbackUsed: false,
  };
}

function writeJson(filePath, value) {
  fs.writeFileSync(
    filePath,
    `${JSON.stringify(value, null, 2)}\n`,
    "utf8",
  );
}

async function main() {
  const args = parseArgs(process.argv);
  const startedAt = new Date();

  if (process.env.FIRESTORE_EMULATOR_HOST) {
    throw new Error(
      "FIRESTORE_EMULATOR_HOST is set. Remove it before " +
        "running this ireps-test audit export.",
    );
  }

  fs.mkdirSync(args.outputDir, {
    recursive: true,
  });

  const {
    application,
    database,
    credentialProjectId,
  } = initialiseFirestore(args);

  console.log("============================================================");
  console.log("iREPS SALES RULES FIRESTORE SAMPLE EXPORT");
  console.log("============================================================");
  console.log(`Project:       ${args.projectId}`);
  console.log(`Credential:    ${args.serviceAccountPath}`);
  console.log(`Credential ID: ${credentialProjectId}`);
  console.log(`Output folder: ${args.outputDir}`);
  console.log(`Random seed:   ${args.randomSeed}`);
  console.log(`Sample size:   ${args.sampleSize}`);
  console.log(
    `Bounded reads: ${args.windowCount} windows x ` +
      `${args.windowSize} documents`,
  );
  console.log("Firestore:     READ ONLY");
  console.log("Full scans:    DISABLED");
  console.log("Custom indexes: NOT REQUIRED");
  console.log("============================================================");

  const manifestCollections = [];

  try {
    for (const collectionConfig of COLLECTIONS) {
      console.log(
        `\n[START] ${collectionConfig.name} | ` +
          `mode=${collectionConfig.mode}`,
      );

      const result =
        collectionConfig.mode === "all"
          ? await exportCompleteCollection({
              database,
              collectionName: collectionConfig.name,
            })
          : await boundedSampleCollection({
              database,
              collectionName: collectionConfig.name,
              sampleSize: args.sampleSize,
              randomSeed: args.randomSeed,
              windowCount: args.windowCount,
              windowSize: args.windowSize,
              initialWindowSize: args.initialWindowSize,
              fallbackPoolSize: args.fallbackPoolSize,
            });

      if (
        collectionConfig.mode === "sample" &&
        result.documents.length <
          Math.min(args.sampleSize, result.populationCount)
      ) {
        throw new Error(
          `${collectionConfig.name} returned only ` +
            `${result.documents.length} sampled documents; ` +
            `expected ${Math.min(
              args.sampleSize,
              result.populationCount,
            )}.`,
        );
      }

      const payload = {
        projectId: args.projectId,
        collection: collectionConfig.name,
        exportMode: collectionConfig.mode,
        populationCount: result.populationCount,
        requestedSampleSize:
          collectionConfig.mode === "sample"
            ? args.sampleSize
            : null,
        exportCount: result.documents.length,
        samplingMethod:
          collectionConfig.mode === "sample"
            ? "bounded_deterministic_document_id_windows"
            : "complete_collection_export",
        statisticallyUniform: false,
        purpose:
          "rules-and-schema validation against current data",
        anchorProfile: result.profile,
        candidateCount: result.candidateCount,
        fallbackUsed: result.fallbackUsed,
        queryStats: result.queryStats,
        readOnly: true,
        fullCollectionScanPerformed: false,
        customIndexRequired: false,
        timestampEncoding: {
          marker: "__type",
          timestampValue: "firestore_timestamp",
          fields: ["iso", "seconds", "nanoseconds"],
        },
        documents: result.documents,
      };

      const outputFile = path.join(
        args.outputDir,
        collectionConfig.fileName,
      );

      writeJson(outputFile, payload);

      manifestCollections.push({
        collection: collectionConfig.name,
        fileName: collectionConfig.fileName,
        exportMode: collectionConfig.mode,
        populationCount: result.populationCount,
        requestedSampleSize:
          collectionConfig.mode === "sample"
            ? args.sampleSize
            : null,
        exportCount: result.documents.length,
        candidateCount: result.candidateCount,
        fallbackUsed: result.fallbackUsed,
        documentIds: result.documents.map(
          (document) => document.id,
        ),
      });

      console.log(
        `[DONE] ${collectionConfig.name} | ` +
          `population=${result.populationCount} | ` +
          `candidates=${result.candidateCount} | ` +
          `exported=${result.documents.length} | ` +
          `fallback=${result.fallbackUsed ? "yes" : "no"}`,
      );
    }

    const completedAt = new Date();

    const manifest = {
      auditName:
        "iREPS Sales Pipeline Rules Data Validation Sample",
      projectId: args.projectId,
      credentialProjectId,
      serviceAccountPathUsed: args.serviceAccountPath,
      serviceAccountCopiedToOutput: false,
      readOnly: true,
      fullCollectionScansPerformed: false,
      customIndexesRequired: false,
      startedAt: startedAt.toISOString(),
      completedAt: completedAt.toISOString(),
      durationSeconds: Number(
        (
          (completedAt.getTime() - startedAt.getTime()) /
          1000
        ).toFixed(3),
      ),
      outputDirectory: args.outputDir,
      randomSeed: args.randomSeed,
      sampleMethod:
        "bounded_deterministic_document_id_windows",
      statisticallyUniform: false,
      purpose:
        "rules-and-schema validation against current data",
      sampleSizePerLargeCollection: args.sampleSize,
      windowCount: args.windowCount,
      windowSize: args.windowSize,
      fallbackPoolSize: args.fallbackPoolSize,
      collections: manifestCollections,
      totalDocumentsExported:
        manifestCollections.reduce(
          (sum, collection) =>
            sum + collection.exportCount,
          0,
        ),
    };

    const manifestPath = path.join(
      args.outputDir,
      "manifest.json",
    );

    writeJson(manifestPath, manifest);

    console.log("\n============================================================");
    console.log("EXPORT COMPLETE");
    console.log("============================================================");
    console.log(`Manifest: ${manifestPath}`);
    console.log(
      `Total exported: ${manifest.totalDocumentsExported}`,
    );
    console.log("Firestore writes performed: 0");
    console.log("Full large-collection scans: 0");
    console.log("Custom indexes created: 0");
    console.log("============================================================");
  } finally {
    await deleteApp(application);
  }
}

main().catch((error) => {
  console.error(
    `\n[FAILED] ${error?.stack || error?.message || String(error)}`,
  );
  process.exitCode = 1;
});
