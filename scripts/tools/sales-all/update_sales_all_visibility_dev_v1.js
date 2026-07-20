#!/usr/bin/env node
"use strict";

/**
 * SAM-DATA-DEV-002
 * Governed targeted remediation for exactly one ireps2 DEV document:
 *
 *   sales-all-meters/01023672577
 *
 * Intended change:
 *   add master.visibility = "INVISIBLE" only when visibility is absent.
 *
 * Default mode: DRY RUN.
 * Maximum Firestore writes in APPLY mode: ONE.
 * No writer code is assessed or changed by this script.
 */

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");

const { cert, deleteApp, initializeApp } = require("firebase-admin/app");
const { getFirestore } = require("firebase-admin/firestore");

const TASK_ID = "SAM-DATA-DEV-002";
const SCRIPT_VERSION = "1.0.0";
const SCHEMA_VERSION = "sales_all_meters/1.1.0";

const TARGET_PROJECT_ID = "ireps2";
const TARGET_COLLECTION = "sales-all-meters";
const TARGET_DOCUMENT_ID = "01023672577";
const TARGET_VISIBILITY = "INVISIBLE";

const APPROVED_SERVICE_ACCOUNT_PATHS = Object.freeze([
  String.raw`C:\dev\secrets\ireps2-b33892e25c20.json`,
  String.raw`C:\dev\secrets\ireps2-e72fd9dc94de.json`,
]);

function nowIso() {
  return new Date().toISOString();
}

function compactUtcTimestamp(date = new Date()) {
  return date.toISOString().replace(/[-:.]/g, "").replace("Z", "Z");
}

function printHelp() {
  console.log(`
${TASK_ID} — governed one-document Sales All Meters visibility remediation

DRY RUN:
  node scripts/tools/sales-all/update_sales_all_visibility_dev_v1.js \\
    --service-account "C:\\dev\\secrets\\ireps2-e72fd9dc94de.json"

APPLY:
  node scripts/tools/sales-all/update_sales_all_visibility_dev_v1.js \\
    --service-account "C:\\dev\\secrets\\ireps2-e72fd9dc94de.json" \\
    --apply \\
    --confirm-project ireps2 \\
    --confirm-document 01023672577

Target:
  ${TARGET_PROJECT_ID} / ${TARGET_COLLECTION} / ${TARGET_DOCUMENT_ID}

Allowed change:
  master.visibility: absent -> ${TARGET_VISIBILITY}

Maximum Firestore writes:
  DRY RUN: 0
  APPLY:   1
`);
}

function parseArgs(argv) {
  const args = {
    serviceAccountPath: null,
    apply: false,
    confirmProject: null,
    confirmDocument: null,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];

    if (token === "--service-account") {
      const value = argv[i + 1];
      if (!value) throw new Error("--service-account requires a file path.");
      args.serviceAccountPath = value;
      i += 1;
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
      const value = argv[i + 1];
      if (!value) throw new Error("--confirm-project requires a value.");
      args.confirmProject = value;
      i += 1;
      continue;
    }

    if (token.startsWith("--confirm-project=")) {
      args.confirmProject = token.slice("--confirm-project=".length);
      continue;
    }

    if (token === "--confirm-document") {
      const value = argv[i + 1];
      if (!value) throw new Error("--confirm-document requires a value.");
      args.confirmDocument = value;
      i += 1;
      continue;
    }

    if (token.startsWith("--confirm-document=")) {
      args.confirmDocument = token.slice("--confirm-document=".length);
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

    if (args.confirmDocument !== TARGET_DOCUMENT_ID) {
      throw new Error(
        `APPLY LOCK FAILED: --confirm-document must be exactly ${TARGET_DOCUMENT_ID}.`,
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

function validateTargetDocument(snapshot) {
  const issues = [];

  if (!snapshot.exists) {
    issues.push("TARGET_DOCUMENT_NOT_FOUND");
    return { issues, data: null };
  }

  const data = snapshot.data() || {};

  if (!isPlainMap(data.master)) {
    issues.push("MASTER_NOT_MAP");
  } else {
    if (data.master.id !== TARGET_DOCUMENT_ID) {
      issues.push(
        `MASTER_ID_MISMATCH:${JSON.stringify(data.master.id)}!=${TARGET_DOCUMENT_ID}`,
      );
    }

    if (Object.prototype.hasOwnProperty.call(data.master, "visibility")) {
      if (data.master.visibility === TARGET_VISIBILITY) {
        issues.push("ALREADY_INVISIBLE");
      } else {
        issues.push(
          `VISIBILITY_CONFLICT:${JSON.stringify(data.master.visibility)}`,
        );
      }
    }
  }

  if (data.meterNoNormalized !== TARGET_DOCUMENT_ID) {
    issues.push(
      `METER_NO_NORMALIZED_MISMATCH:${JSON.stringify(
        data.meterNoNormalized,
      )}!=${TARGET_DOCUMENT_ID}`,
    );
  }

  if (data.provider !== "conlog") {
    issues.push(`PROVIDER_MISMATCH:${JSON.stringify(data.provider)}`);
  }

  return { issues, data };
}

async function writeJson(filePath, value) {
  await fsp.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function main() {
  const startedAt = nowIso();
  const args = parseArgs(process.argv.slice(2));
  const mode = args.apply ? "APPLY" : "DRY_RUN";
  const runId = `${TASK_ID}__${compactUtcTimestamp()}`;
  const runDirectory = path.resolve(
    __dirname,
    "reports",
    runId,
  );
  const reportPath = path.join(runDirectory, "run_report.json");

  await fsp.mkdir(runDirectory, { recursive: true });

  let app = null;
  let writesAttempted = 0;
  let writesCompleted = 0;
  let status = "STARTED";
  let outcome = null;
  let failure = null;
  let serviceAccountInfo = null;

  console.log(`[TASK] ${TASK_ID}`);
  console.log(`[SCHEMA] ${SCHEMA_VERSION}`);
  console.log(`[LOCK] Project: ${TARGET_PROJECT_ID}`);
  console.log(`[LOCK] Collection: ${TARGET_COLLECTION}`);
  console.log(`[LOCK] Document: ${TARGET_DOCUMENT_ID}`);
  console.log(`[MODE] ${mode}`);
  console.log(`[OUT] ${runDirectory}`);

  try {
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

    const targetRef = db
      .collection(TARGET_COLLECTION)
      .doc(TARGET_DOCUMENT_ID);

    console.log("[STEP 3/5] Reading and validating the target document...");
    const beforeSnap = await targetRef.get();
    const assessment = validateTargetDocument(beforeSnap);

    if (assessment.issues.includes("TARGET_DOCUMENT_NOT_FOUND")) {
      throw new Error("Target document does not exist.");
    }

    const alreadyInvisible = assessment.issues.includes("ALREADY_INVISIBLE");
    const blockingIssues = assessment.issues.filter(
      (issue) => issue !== "ALREADY_INVISIBLE",
    );

    console.log(
      `[READ] exists=${beforeSnap.exists} updateTime=${
        beforeSnap.updateTime?.toDate().toISOString() || "NAv"
      }`,
    );
    console.log(
      `[CURRENT] master.visibility=${
        Object.prototype.hasOwnProperty.call(
          assessment.data?.master || {},
          "visibility",
        )
          ? JSON.stringify(assessment.data.master.visibility)
          : "<ABSENT>"
      }`,
    );

    if (blockingIssues.length > 0) {
      outcome = "CONFLICT";
      throw new Error(
        `Target validation failed: ${blockingIssues.join(" | ")}`,
      );
    }

    if (alreadyInvisible) {
      outcome = "UNCHANGED";
      status = "COMPLETED";
      console.log(
        `[UNCHANGED] master.visibility is already ${TARGET_VISIBILITY}; no write required.`,
      );
    } else if (!args.apply) {
      outcome = "WOULD_UPDATE";
      status = "COMPLETED";
      console.log("[STEP 4/5] Dry-run decision...");
      console.log(
        `[DRY-RUN] Would set master.visibility=${TARGET_VISIBILITY}`,
      );
      console.log("[WRITE] attempted=0 completed=0");
    } else {
      console.log("[STEP 4/5] Applying the single allowed field update...");
      writesAttempted = 1;

      await targetRef.update(
        {
          "master.visibility": TARGET_VISIBILITY,
        },
        {
          lastUpdateTime: beforeSnap.updateTime,
        },
      );

      writesCompleted = 1;
      console.log(
        `[WRITE] completed=1 field=master.visibility value=${TARGET_VISIBILITY}`,
      );

      console.log("[STEP 5/5] Re-reading and verifying the result...");
      const afterSnap = await targetRef.get();
      const afterData = afterSnap.data() || {};

      if (afterData?.master?.visibility !== TARGET_VISIBILITY) {
        throw new Error(
          `POST-WRITE VERIFICATION FAILED: expected ${TARGET_VISIBILITY}, received ${JSON.stringify(
            afterData?.master?.visibility,
          )}.`,
        );
      }

      if (
        afterData?.master?.id !== TARGET_DOCUMENT_ID ||
        afterData?.meterNoNormalized !== TARGET_DOCUMENT_ID
      ) {
        throw new Error(
          "POST-WRITE IDENTITY VERIFICATION FAILED after the targeted update.",
        );
      }

      outcome = "UPDATED";
      status = "COMPLETED";
      console.log(
        `[VERIFIED] master.visibility=${afterData.master.visibility}`,
      );
    }
  } catch (error) {
    status = "FAILED";
    failure = {
      name: error.name || "Error",
      message: error.message || String(error),
      code: error.code || null,
    };
    console.error(`[FAILED] ${failure.code || ""} ${failure.message}`.trim());
  } finally {
    const completedAt = nowIso();
    const report = {
      taskId: TASK_ID,
      runId,
      scriptVersion: SCRIPT_VERSION,
      schemaVersion: SCHEMA_VERSION,
      status,
      mode,
      outcome,
      startedAt,
      completedAt,
      durationMs: new Date(completedAt).getTime() - new Date(startedAt).getTime(),
      target: {
        projectId: TARGET_PROJECT_ID,
        collection: TARGET_COLLECTION,
        documentId: TARGET_DOCUMENT_ID,
        documentPath: `${TARGET_COLLECTION}/${TARGET_DOCUMENT_ID}`,
      },
      intendedChange: {
        fieldPath: "master.visibility",
        from: "ABSENT",
        to: TARGET_VISIBILITY,
      },
      serviceAccount: serviceAccountInfo,
      firestoreWrites: {
        maximumPermitted: args.apply ? 1 : 0,
        attempted: writesAttempted,
        completed: writesCompleted,
      },
      governance: {
        projectHardLocked: true,
        collectionHardLocked: true,
        documentHardLocked: true,
        allowedFieldHardLocked: true,
        writerCodeAssessed: false,
        writerCodeChanged: false,
        metadataChanged: false,
      },
      failure,
    };

    try {
      await writeJson(reportPath, report);
      console.log(`[REPORT] ${reportPath}`);
    } catch (reportError) {
      console.error(`[REPORT-FAILED] ${reportError.message}`);
      if (status !== "FAILED") {
        status = "FAILED";
      }
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
