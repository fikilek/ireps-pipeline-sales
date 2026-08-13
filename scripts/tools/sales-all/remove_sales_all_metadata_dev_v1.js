#!/usr/bin/env node
"use strict";

/**
 * SAM-DATA-DEV-003
 *
 * Governed DEV remediation for the exact 72 sales-all-meters documents
 * identified by SAM-DATA-DEV-001 as containing the prohibited root
 * field `metadata`.
 *
 * DEFAULT MODE: DRY RUN
 *
 * Allowed Firestore change:
 *   delete the root `metadata` field
 *
 * Prohibited:
 *   - changing any other field
 *   - changing writer code
 *   - creating a collection
 *   - targeting any project except ireps2
 *   - targeting any collection except sales-all-meters
 *   - targeting any document outside the embedded assessed ID set
 */

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { isDeepStrictEqual } = require("node:util");

const { cert, deleteApp, initializeApp } = require("firebase-admin/app");
const { FieldValue, getFirestore } = require("firebase-admin/firestore");

const TASK_ID = "SAM-DATA-DEV-003";
const SCRIPT_VERSION = "1.0.0";
const SCHEMA_VERSION = "sales_all_meters/1.1.0";

const SOURCE_ASSESSMENT_TASK_ID = "SAM-DATA-DEV-001";
const SOURCE_ASSESSMENT_RUN_ID =
  "SAM-DATA-DEV-001__20260719T213025941Z";

const TARGET_PROJECT_ID = "ireps2";
const TARGET_COLLECTION = "sales-all-meters";
const TARGET_DOCUMENT_COUNT = 72;
const FIRESTORE_BATCH_SIZE = 400;
const CONFIRM_ACTION = "REMOVE_ROOT_METADATA";

const TARGET_DOCUMENT_IDS = Object.freeze([
  "01023658840",
  "01023671660",
  "01023671710",
  "01023672577",
  "01023680364",
  "01023681180",
  "01023687591",
  "04085344689",
  "04085344705",
  "04085344713",
  "04085344747",
  "04085344770",
  "04085344788",
  "04085344853",
  "04085344978",
  "04085345009",
  "04085345058",
  "04085345090",
  "04085345116",
  "04085345165",
  "04085345371",
  "04085345447",
  "04085345462",
  "04085345488",
  "04085346122",
  "04085346221",
  "04085346742",
  "04085347047",
  "04085347120",
  "04085347328",
  "04085347336",
  "04085347419",
  "04085347435",
  "04085347443",
  "04085347484",
  "04085347765",
  "04085348193",
  "04085348235",
  "04085348292",
  "04085348318",
  "04085348342",
  "04085348375",
  "04085348474",
  "04085348482",
  "04085348524",
  "04085348557",
  "04085348573",
  "04085348649",
  "04085348656",
  "04085348698",
  "04085348706",
  "04085348722",
  "04085348748",
  "04085348763",
  "04085348771",
  "04085348797",
  "04085348805",
  "04085348813",
  "04085348821",
  "04085348839",
  "04085348847",
  "04085348854",
  "04085348888",
  "04085348920",
  "04085348946",
  "04085348953",
  "04085348979",
  "04085349019",
  "04085349076",
  "04085349092",
  "04085349126",
  "04085349183"
]);

const TARGET_ID_FINGERPRINT_SHA256 =
  "87ff5d210fdc2223b45a15006fe963fdb69b3e976dac3e22e4b63ae1f02a6beb";

const APPROVED_SERVICE_ACCOUNT_PATHS = Object.freeze([
  String.raw`C:\dev\secrets\ireps2-b33892e25c20.json`,
  String.raw`C:\dev\secrets\ireps2-e72fd9dc94de.json`,
]);

const CANONICAL_ROOT_FIELDS = Object.freeze([
  "master",
  "meterNo",
  "meterNoNormalized",
  "provider",
  "customerNo",
  "accountNo",
  "totalAmountC",
  "monthlyTotalsC",
  "lastPurchaseAtISO",
  "daysSinceLastPurchase",
]);

const ALLOWED_VISIBILITY_VALUES = new Set(["VISIBLE", "INVISIBLE"]);

function nowIso() {
  return new Date().toISOString();
}

function compactUtcTimestamp(date = new Date()) {
  return date.toISOString().replace(/[-:.]/g, "").replace("Z", "Z");
}

function sha256Text(value) {
  return require("node:crypto")
    .createHash("sha256")
    .update(value, "utf8")
    .digest("hex");
}

function printHelp() {
  console.log(`
${TASK_ID} — remove prohibited root metadata from exactly 72 DEV documents

DRY RUN:
  node scripts/tools/sales-all/remove_sales_all_metadata_dev_v1.js \\
    --service-account "C:\\dev\\secrets\\ireps2-e72fd9dc94de.json"

APPLY:
  node scripts/tools/sales-all/remove_sales_all_metadata_dev_v1.js \\
    --service-account "C:\\dev\\secrets\\ireps2-e72fd9dc94de.json" \\
    --apply \\
    --confirm-project ireps2 \\
    --confirm-collection sales-all-meters \\
    --confirm-count 72 \\
    --confirm-action REMOVE_ROOT_METADATA

Hard-locked target:
  Project:    ${TARGET_PROJECT_ID}
  Collection: ${TARGET_COLLECTION}
  Documents:  ${TARGET_DOCUMENT_COUNT}
  Field:      root metadata
  Action:     delete

Default mode performs zero Firestore writes.
`);
}

function parseArgs(argv) {
  const args = {
    serviceAccountPath: null,
    apply: false,
    confirmProject: null,
    confirmCollection: null,
    confirmCount: null,
    confirmAction: null,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];

    if (token === "--service-account") {
      const value = argv[index + 1];
      if (!value) throw new Error("--service-account requires a file path.");
      args.serviceAccountPath = value;
      index += 1;
      continue;
    }

    if (token.startsWith("--service-account=")) {
      args.serviceAccountPath = token.slice("--service-account=".length);
      continue;
    }

    if (token === "--apply") {
      args.apply = true;
      continue;
    }

    if (token === "--confirm-project") {
      const value = argv[index + 1];
      if (!value) throw new Error("--confirm-project requires a value.");
      args.confirmProject = value;
      index += 1;
      continue;
    }

    if (token.startsWith("--confirm-project=")) {
      args.confirmProject = token.slice("--confirm-project=".length);
      continue;
    }

    if (token === "--confirm-collection") {
      const value = argv[index + 1];
      if (!value) throw new Error("--confirm-collection requires a value.");
      args.confirmCollection = value;
      index += 1;
      continue;
    }

    if (token.startsWith("--confirm-collection=")) {
      args.confirmCollection = token.slice("--confirm-collection=".length);
      continue;
    }

    if (token === "--confirm-count") {
      const value = argv[index + 1];
      if (!value) throw new Error("--confirm-count requires a value.");
      args.confirmCount = Number(value);
      index += 1;
      continue;
    }

    if (token.startsWith("--confirm-count=")) {
      args.confirmCount = Number(token.slice("--confirm-count=".length));
      continue;
    }

    if (token === "--confirm-action") {
      const value = argv[index + 1];
      if (!value) throw new Error("--confirm-action requires a value.");
      args.confirmAction = value;
      index += 1;
      continue;
    }

    if (token.startsWith("--confirm-action=")) {
      args.confirmAction = token.slice("--confirm-action=".length);
      continue;
    }

    if (token === "--help" || token === "-h") {
      printHelp();
      process.exit(0);
    }

    throw new Error(`Unknown argument: ${token}`);
  }

  if (args.apply) {
    if (args.confirmProject !== TARGET_PROJECT_ID) {
      throw new Error(
        `APPLY LOCK FAILED: --confirm-project must be exactly ${TARGET_PROJECT_ID}.`,
      );
    }

    if (args.confirmCollection !== TARGET_COLLECTION) {
      throw new Error(
        `APPLY LOCK FAILED: --confirm-collection must be exactly ${TARGET_COLLECTION}.`,
      );
    }

    if (args.confirmCount !== TARGET_DOCUMENT_COUNT) {
      throw new Error(
        `APPLY LOCK FAILED: --confirm-count must be exactly ${TARGET_DOCUMENT_COUNT}.`,
      );
    }

    if (args.confirmAction !== CONFIRM_ACTION) {
      throw new Error(
        `APPLY LOCK FAILED: --confirm-action must be exactly ${CONFIRM_ACTION}.`,
      );
    }
  }

  return args;
}

function normalizedWindowsPath(value) {
  return path.win32.normalize(String(value)).replace(/\\+$/, "").toLowerCase();
}

function isApprovedServiceAccountPath(value) {
  const candidate = normalizedWindowsPath(value);
  return APPROVED_SERVICE_ACCOUNT_PATHS.some(
    (approved) => normalizedWindowsPath(approved) === candidate,
  );
}

async function selectAndValidateServiceAccount(requestedPath) {
  let selectedPath = requestedPath;

  if (!selectedPath) {
    selectedPath = APPROVED_SERVICE_ACCOUNT_PATHS.find((candidate) =>
      fs.existsSync(candidate),
    );
  }

  if (!selectedPath) {
    throw new Error(
      `No approved service-account file was found. Pass --service-account using one of: ${APPROVED_SERVICE_ACCOUNT_PATHS.join(
        " | ",
      )}`,
    );
  }

  if (!isApprovedServiceAccountPath(selectedPath)) {
    throw new Error(
      `Service-account path is not approved for this governed DEV task: ${selectedPath}`,
    );
  }

  const resolvedPath = path.win32.normalize(selectedPath);
  const raw = await fsp.readFile(resolvedPath, "utf8");
  const serviceAccount = JSON.parse(raw);

  if (serviceAccount.project_id !== TARGET_PROJECT_ID) {
    throw new Error(
      `PROJECT LOCK FAILED: service-account project_id is ${JSON.stringify(
        serviceAccount.project_id,
      )}; required project_id is exactly ${TARGET_PROJECT_ID}.`,
    );
  }

  if (!serviceAccount.client_email || !serviceAccount.private_key) {
    throw new Error("Service-account JSON is missing client_email or private_key.");
  }

  return {
    path: resolvedPath,
    basename: path.win32.basename(resolvedPath),
    serviceAccount,
  };
}

function isPlainMap(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    value.constructor === Object
  );
}

function sortedKeys(value) {
  return Object.keys(value || {}).sort();
}

function arraysEqual(left, right) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function withoutMetadata(data) {
  const copy = { ...data };
  delete copy.metadata;
  return copy;
}

function validateTargetDocument(snapshot, documentId) {
  const issues = [];

  if (!snapshot.exists) {
    return {
      result: "CONFLICT",
      issues: ["TARGET_DOCUMENT_NOT_FOUND"],
      data: null,
      metadataPresent: false,
      metadataKeys: [],
    };
  }

  const data = snapshot.data() || {};
  const metadataPresent = Object.prototype.hasOwnProperty.call(data, "metadata");
  const expectedRootFields = metadataPresent
    ? [...CANONICAL_ROOT_FIELDS, "metadata"].sort()
    : [...CANONICAL_ROOT_FIELDS].sort();
  const actualRootFields = sortedKeys(data);

  if (!arraysEqual(actualRootFields, expectedRootFields)) {
    issues.push(
      `ROOT_FIELD_SET_UNEXPECTED:actual=${actualRootFields.join("|")}`,
    );
  }

  if (!isPlainMap(data.master)) {
    issues.push("MASTER_NOT_MAP");
  } else {
    const actualMasterFields = sortedKeys(data.master);
    const expectedMasterFields = ["id", "visibility"];

    if (!arraysEqual(actualMasterFields, expectedMasterFields)) {
      issues.push(
        `MASTER_FIELD_SET_UNEXPECTED:actual=${actualMasterFields.join("|")}`,
      );
    }

    if (data.master.id !== documentId) {
      issues.push(
        `MASTER_ID_MISMATCH:${JSON.stringify(data.master.id)}!=${documentId}`,
      );
    }

    if (!ALLOWED_VISIBILITY_VALUES.has(data.master.visibility)) {
      issues.push(
        `MASTER_VISIBILITY_INVALID:${JSON.stringify(data.master.visibility)}`,
      );
    }
  }

  if (data.meterNoNormalized !== documentId) {
    issues.push(
      `METER_NO_NORMALIZED_MISMATCH:${JSON.stringify(
        data.meterNoNormalized,
      )}!=${documentId}`,
    );
  }

  if (data.provider !== "conlog") {
    issues.push(`PROVIDER_MISMATCH:${JSON.stringify(data.provider)}`);
  }

  if (metadataPresent && !isPlainMap(data.metadata)) {
    issues.push("METADATA_NOT_MAP");
  }

  return {
    result: issues.length > 0
      ? "CONFLICT"
      : metadataPresent
        ? "WOULD_UPDATE"
        : "UNCHANGED",
    issues,
    data,
    metadataPresent,
    metadataKeys:
      metadataPresent && isPlainMap(data.metadata)
        ? sortedKeys(data.metadata)
        : [],
  };
}

async function appendJsonLine(stream, value) {
  if (!stream.write(`${JSON.stringify(value)}\n`)) {
    await new Promise((resolve) => stream.once("drain", resolve));
  }
}

async function closeStream(stream) {
  await new Promise((resolve, reject) => {
    stream.end((error) => (error ? reject(error) : resolve()));
  });
}

async function writeJson(filePath, value) {
  await fsp.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function createCounters() {
  return {
    totalTargets: TARGET_DOCUMENT_COUNT,
    preflightRead: 0,
    wouldUpdate: 0,
    updated: 0,
    unchanged: 0,
    conflicts: 0,
    failed: 0,
    writesAttempted: 0,
    writesCompleted: 0,
    verificationPassed: 0,
    verificationFailed: 0,
  };
}

function timestampKey(value) {
  if (!value) return "";
  return `${value.seconds ?? ""}:${value.nanoseconds ?? ""}`;
}

async function bulkReadDocuments(db, documentIds) {
  if (documentIds.length > FIRESTORE_BATCH_SIZE) {
    throw new Error(`Bulk read exceeds governed ${FIRESTORE_BATCH_SIZE}-reference limit.`);
  }
  const refs = documentIds.map((id) => db.collection(TARGET_COLLECTION).doc(id));
  const snapshots = await db.getAll(...refs);
  return new Map(snapshots.map((snapshot) => [snapshot.id, snapshot]));
}

function buildMetadataDeleteBatch(db, records) {
  if (records.length > FIRESTORE_BATCH_SIZE) {
    throw new Error(`Write batch exceeds governed ${FIRESTORE_BATCH_SIZE}-operation limit.`);
  }
  const batch = db.batch();
  for (const record of records) {
    batch.update(
      record.documentRef,
      { metadata: FieldValue.delete() },
      { lastUpdateTime: record.snapshot.updateTime },
    );
  }
  return batch;
}

async function main() {
  const startedAt = nowIso();
  const args = parseArgs(process.argv.slice(2));
  const mode = args.apply ? "APPLY" : "DRY_RUN";
  const runId = `${TASK_ID}__${compactUtcTimestamp()}`;
  const runDirectory = path.resolve(__dirname, "reports", runId);
  const reportPath = path.join(runDirectory, "run_report.json");
  const resultsPath = path.join(runDirectory, "record_results.jsonl");
  const conflictsPath = path.join(runDirectory, "conflicts.json");

  await fsp.mkdir(runDirectory, { recursive: true });

  const counters = createCounters();
  const conflicts = [];
  const preflightRecords = [];

  let app = null;
  let serviceAccountInfo = null;
  let status = "STARTED";
  let result = null;
  let failure = null;
  let resultsStream = null;

  console.log(`[TASK] ${TASK_ID}`);
  console.log(`[SCHEMA] ${SCHEMA_VERSION}`);
  console.log(`[SOURCE] ${SOURCE_ASSESSMENT_RUN_ID}`);
  console.log(`[LOCK] Project: ${TARGET_PROJECT_ID}`);
  console.log(`[LOCK] Collection: ${TARGET_COLLECTION}`);
  console.log(`[LOCK] Documents: ${TARGET_DOCUMENT_COUNT} exact assessed IDs`);
  console.log(`[LOCK] Field action: delete root metadata only`);
  console.log(`[MODE] ${mode}`);
  console.log(`[OUT] ${runDirectory}`);

  try {
    if (TARGET_DOCUMENT_IDS.length !== TARGET_DOCUMENT_COUNT) {
      throw new Error(
        `EMBEDDED TARGET COUNT FAILED: expected ${TARGET_DOCUMENT_COUNT}, received ${TARGET_DOCUMENT_IDS.length}.`,
      );
    }

    const sortedIds = [...TARGET_DOCUMENT_IDS].sort();
    if (!arraysEqual(TARGET_DOCUMENT_IDS, sortedIds)) {
      throw new Error("EMBEDDED TARGET ORDER FAILED: document IDs are not sorted.");
    }

    if (new Set(TARGET_DOCUMENT_IDS).size !== TARGET_DOCUMENT_COUNT) {
      throw new Error("EMBEDDED TARGET UNIQUENESS FAILED: duplicate document IDs.");
    }

    const calculatedFingerprint = sha256Text(TARGET_DOCUMENT_IDS.join("\n"));
    if (calculatedFingerprint !== TARGET_ID_FINGERPRINT_SHA256) {
      throw new Error(
        `EMBEDDED TARGET FINGERPRINT FAILED: expected ${TARGET_ID_FINGERPRINT_SHA256}, received ${calculatedFingerprint}.`,
      );
    }

    console.log(
      `[TARGETS] count=${TARGET_DOCUMENT_COUNT} sha256=${TARGET_ID_FINGERPRINT_SHA256}`,
    );

    console.log("[STEP 1/5] Validating approved service account...");
    const selected = await selectAndValidateServiceAccount(
      args.serviceAccountPath,
    );

    serviceAccountInfo = {
      file: selected.basename,
      projectId: selected.serviceAccount.project_id,
      clientEmail: selected.serviceAccount.client_email,
    };

    console.log(
      `[KEY] ${selected.basename} (project_id ${TARGET_PROJECT_ID} validated)`,
    );

    console.log("[STEP 2/5] Initializing Firebase Admin SDK...");
    app = initializeApp(
      {
        credential: cert(selected.serviceAccount),
        projectId: TARGET_PROJECT_ID,
      },
      `${TASK_ID}-${Date.now()}`,
    );

    const db = getFirestore(app);
    db.settings({
      ignoreUndefinedProperties: false,
      useBigInt: true,
    });

    resultsStream = fs.createWriteStream(resultsPath, {
      flags: "wx",
      encoding: "utf8",
    });

    console.log("[STEP 3/5] Preflighting all 72 hard-locked documents...");

    const preflightSnapshots = await bulkReadDocuments(db, TARGET_DOCUMENT_IDS);
    counters.preflightRead += TARGET_DOCUMENT_IDS.length;
    for (let index = 0; index < TARGET_DOCUMENT_IDS.length; index += 1) {
      const documentId = TARGET_DOCUMENT_IDS[index];
      const documentRef = db.collection(TARGET_COLLECTION).doc(documentId);
      try {
        const snapshot = preflightSnapshots.get(documentId);
        if (!snapshot) throw new Error("BULK_READ_SNAPSHOT_MISSING");
        const assessment = validateTargetDocument(snapshot, documentId);
        const record = {
          index: index + 1,
          documentId,
          phase: "PREFLIGHT",
          result: assessment.result,
          issues: assessment.issues,
          metadataPresent: assessment.metadataPresent,
          metadataKeys: assessment.metadataKeys,
          updateTime: snapshot.updateTime?.toDate().toISOString() || null,
        };
        preflightRecords.push({ documentId, documentRef, snapshot, assessment });
        if (assessment.result === "WOULD_UPDATE") counters.wouldUpdate += 1;
        else if (assessment.result === "UNCHANGED") counters.unchanged += 1;
        else {
          counters.conflicts += 1;
          conflicts.push(record);
        }
        await appendJsonLine(resultsStream, record);
        console.log(`[PREFLIGHT] ${index + 1}/${TARGET_DOCUMENT_COUNT} id=${documentId} result=${assessment.result}`);
      } catch (error) {
        counters.failed += 1;
        const record = {
          index: index + 1,
          documentId,
          phase: "PREFLIGHT",
          result: "FAILED",
          issues: [error.message || String(error)],
          code: error.code || null,
        };
        conflicts.push(record);
        await appendJsonLine(resultsStream, record);
        console.log(`[PREFLIGHT] ${index + 1}/${TARGET_DOCUMENT_COUNT} id=${documentId} result=FAILED`);
      }
    }

    console.log(
      `[PREFLIGHT-SUMMARY] targets=${TARGET_DOCUMENT_COUNT} wouldUpdate=${counters.wouldUpdate} unchanged=${counters.unchanged} conflicts=${counters.conflicts} failed=${counters.failed}`,
    );

    if (counters.conflicts > 0 || counters.failed > 0) {
      throw new Error(
        "APPLY BLOCKED: one or more target documents failed preflight validation.",
      );
    }

    if (!args.apply) {
      status = "COMPLETED";
      result = counters.wouldUpdate > 0 ? "WOULD_UPDATE" : "UNCHANGED";
      console.log("[STEP 4/5] Dry-run decision complete.");
      console.log(
        `[DRY-RUN] wouldDeleteMetadata=${counters.wouldUpdate} alreadyClean=${counters.unchanged}`,
      );
      console.log("[WRITE] attempted=0 completed=0");
    } else {
      console.log(
        `[STEP 4/5] Applying root metadata deletion to ${counters.wouldUpdate} documents...`,
      );

      const updateRecords = preflightRecords.filter(
        (record) => record.assessment.result === "WOULD_UPDATE",
      );
      counters.writesAttempted += updateRecords.length;
      let committedRecords = updateRecords;

      if (updateRecords.length > 0) {
        try {
          await buildMetadataDeleteBatch(db, updateRecords).commit();
        } catch (error) {
          const isConcurrencyFailure =
            error.code === 6 || error.code === 9 || error.code === 10 ||
            error.code === "already-exists" ||
            error.code === "failed-precondition" ||
            error.code === "aborted" ||
            error.code === "ALREADY_EXISTS" ||
            error.code === "FAILED_PRECONDITION" ||
            error.code === "ABORTED";
          if (!isConcurrencyFailure) throw error;

          const refreshed = await bulkReadDocuments(
            db,
            updateRecords.map((record) => record.documentId),
          );
          const retryRecords = [];
          committedRecords = [];
          for (const record of updateRecords) {
            const current = refreshed.get(record.documentId);
            if (
              !current || !current.exists ||
              timestampKey(current.updateTime) !== timestampKey(record.snapshot.updateTime)
            ) {
              counters.conflicts += 1;
              conflicts.push({
                documentId: record.documentId,
                phase: "APPLY",
                result: "CONFLICT",
                code: error.code || null,
                issues: ["PRECONDITION_CHANGED_AFTER_PREFLIGHT"],
              });
              continue;
            }
            retryRecords.push({ ...record, snapshot: current });
          }
          if (retryRecords.length > 0) {
            counters.writesAttempted += retryRecords.length;
            try {
              await buildMetadataDeleteBatch(db, retryRecords).commit();
            } catch (retryError) {
              throw new Error(
                `BOUNDED_BATCH_RECOVERY_FAILED:${retryError.message || String(retryError)}`,
              );
            }
            committedRecords = retryRecords;
          }
        }
      }

      counters.updated += committedRecords.length;
      counters.writesCompleted += committedRecords.length;

      console.log("[STEP 5/5] Verifying all 72 target documents...");
      const finalSnapshots = await bulkReadDocuments(db, TARGET_DOCUMENT_IDS);
      let finalMetadataPresent = 0;
      let finalValidationFailures = 0;
      const committedIds = new Set(committedRecords.map((record) => record.documentId));

      for (let index = 0; index < TARGET_DOCUMENT_IDS.length; index += 1) {
        const documentId = TARGET_DOCUMENT_IDS[index];
        const snapshot = finalSnapshots.get(documentId);
        if (!snapshot) throw new Error(`FINAL_BULK_READ_SNAPSHOT_MISSING:${documentId}`);
        const assessment = validateTargetDocument(snapshot, documentId);
        if (assessment.metadataPresent) finalMetadataPresent += 1;
        if (assessment.result !== "UNCHANGED") finalValidationFailures += 1;

        if (committedIds.has(documentId)) {
          const before = preflightRecords.find((record) => record.documentId === documentId);
          const afterData = snapshot.data() || {};
          if (!before || !isDeepStrictEqual(withoutMetadata(before.assessment.data), afterData)) {
            throw new Error(`POST_WRITE_NON_METADATA_FIELDS_CHANGED:${documentId}`);
          }
          counters.verificationPassed += 1;
          await appendJsonLine(resultsStream, {
            documentId,
            phase: "APPLY",
            result: "UPDATED",
            changedField: "metadata",
            operation: "DELETE_ROOT_FIELD",
            postWriteVerified: true,
            updateTime: snapshot.updateTime?.toDate().toISOString() || null,
          });
        }
        if ((index + 1) % 10 === 0 || index + 1 === TARGET_DOCUMENT_COUNT) {
          console.log(
            `[VERIFY] checked=${index + 1}/${TARGET_DOCUMENT_COUNT} metadataPresent=${finalMetadataPresent} validationFailures=${finalValidationFailures}`,
          );
        }
      }

      if (finalMetadataPresent !== 0 || finalValidationFailures !== 0) {
        throw new Error(
          `FINAL VERIFICATION FAILED: metadataPresent=${finalMetadataPresent} validationFailures=${finalValidationFailures}.`,
        );
      }

      if (counters.conflicts > 0 || counters.failed > 0) {
        status = "COMPLETED_WITH_CONFLICTS";
        result = "COMPLETED_WITH_CONFLICTS";
      } else {
        status = "COMPLETED";
        result = counters.updated > 0 ? "UPDATED" : "UNCHANGED";
      }

      console.log(
        `[APPLY-SUMMARY] updated=${counters.updated} unchanged=${counters.unchanged} conflicts=${counters.conflicts} failed=${counters.failed} writes=${counters.writesCompleted}`,
      );
    }
  } catch (error) {
    status = "FAILED";
    result = result || "FAILED";
    failure = {
      name: error.name || "Error",
      message: error.message || String(error),
      code: error.code || null,
    };

    console.error(`[FAILED] ${failure.code || ""} ${failure.message}`.trim());
  } finally {
    if (resultsStream) {
      try {
        await closeStream(resultsStream);
      } catch (streamError) {
        console.error(`[RESULTS-CLOSE-WARN] ${streamError.message}`);
      }
    }

    const completedAt = nowIso();
    const report = {
      taskId: TASK_ID,
      runId,
      scriptVersion: SCRIPT_VERSION,
      schemaVersion: SCHEMA_VERSION,
      sourceAssessment: {
        taskId: SOURCE_ASSESSMENT_TASK_ID,
        runId: SOURCE_ASSESSMENT_RUN_ID,
        targetIdCount: TARGET_DOCUMENT_COUNT,
        targetIdFingerprintSha256: TARGET_ID_FINGERPRINT_SHA256,
      },
      status,
      result,
      mode,
      startedAt,
      completedAt,
      durationMs:
        new Date(completedAt).getTime() - new Date(startedAt).getTime(),
      target: {
        projectId: TARGET_PROJECT_ID,
        collection: TARGET_COLLECTION,
        documentCount: TARGET_DOCUMENT_COUNT,
        allowedField: "metadata",
        allowedOperation: "DELETE_ROOT_FIELD",
      },
      serviceAccount: serviceAccountInfo,
      counters,
      outputs: {
        runDirectory,
        runReport: "run_report.json",
        recordResults: "record_results.jsonl",
        conflicts: conflicts.length > 0 ? "conflicts.json" : null,
      },
      governance: {
        projectHardLocked: true,
        collectionHardLocked: true,
        targetDocumentSetHardLocked: true,
        targetDocumentSetFingerprintValidated: true,
        rootMetadataDeleteOnly: true,
        maximumPermittedWrites: args.apply ? TARGET_DOCUMENT_COUNT : 0,
        writerCodeAssessed: false,
        writerCodeChanged: false,
        newFirestoreCollectionCreated: false,
        firestoreBatchSize: FIRESTORE_BATCH_SIZE,
        bulkPreflightReads: true,
        batchedWrites: true,
        bulkVerificationReads: true,
        perDocumentFallback: false,
      },
      failure,
    };

    try {
      if (conflicts.length > 0) {
        await writeJson(conflictsPath, conflicts);
      }

      await writeJson(reportPath, report);
      console.log(`[REPORT] ${reportPath}`);
      console.log(`[RECORDS] ${resultsPath}`);

      if (conflicts.length > 0) {
        console.log(`[CONFLICTS] ${conflictsPath}`);
      }
    } catch (reportError) {
      console.error(`[REPORT-FAILED] ${reportError.message}`);
      status = "FAILED";
    }

    if (app) {
      try {
        await deleteApp(app);
      } catch (deleteError) {
        console.error(`[CLEANUP-WARN] ${deleteError.message}`);
      }
    }
  }

  if (status !== "COMPLETED") {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(`[FATAL] ${error.stack || error.message || String(error)}`);
  process.exitCode = 1;
});
