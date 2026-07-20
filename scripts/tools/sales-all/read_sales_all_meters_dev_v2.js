#!/usr/bin/env node
"use strict";

/**
 * SAM-DATA-DEV-004
 * Governed READ-ONLY reassessment against sales_all_meters/1.1.0 of ireps2 / sales-all-meters.
 *
 * Firestore writes: NONE.
 * Target project: ireps2 only.
 * Target collection: sales-all-meters only.
 */

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const { once } = require("node:events");

const { cert, deleteApp, initializeApp } = require("firebase-admin/app");
const {
  DocumentReference,
  FieldPath,
  GeoPoint,
  Timestamp,
  getFirestore,
} = require("firebase-admin/firestore");

const TASK_ID = "SAM-DATA-DEV-004";
const SCRIPT_VERSION = "2.0.1";
const TARGET_PROJECT_ID = "ireps2";
const TARGET_COLLECTION = "sales-all-meters";
const DEFAULT_PAGE_SIZE = 500;
const MAX_PAGE_SIZE = 1000;
const MAX_SAMPLES_PER_GROUP = 20;

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

const CANONICAL_ROOT_FIELD_SET = new Set(CANONICAL_ROOT_FIELDS);
const CANONICAL_MASTER_FIELD_SET = new Set(["id", "visibility"]);
const CANONICAL_VISIBILITY_VALUES = new Set(["VISIBLE", "INVISIBLE"]);
const CANONICAL_ID_RE = /^[A-Z0-9]+$/;
const CANONICAL_MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const TIMEZONE_SUFFIX_RE = /(Z|[+-]\d{2}:\d{2})$/i;

function utcNowIso() {
  return new Date().toISOString();
}

function compactUtcTimestamp(date = new Date()) {
  return date.toISOString().replace(/[-:.]/g, "").replace("Z", "Z");
}

function parseArgs(argv) {
  const args = {
    serviceAccountPath: null,
    pageSize: DEFAULT_PAGE_SIZE,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];

    if (token === "--service-account") {
      const value = argv[i + 1];
      if (!value) {
        throw new Error("--service-account requires a file path.");
      }
      args.serviceAccountPath = value;
      i += 1;
      continue;
    }

    if (token.startsWith("--service-account=")) {
      args.serviceAccountPath = token.slice("--service-account=".length);
      continue;
    }

    if (token === "--page-size") {
      const value = argv[i + 1];
      if (!value) {
        throw new Error("--page-size requires an integer value.");
      }
      args.pageSize = Number(value);
      i += 1;
      continue;
    }

    if (token.startsWith("--page-size=")) {
      args.pageSize = Number(token.slice("--page-size=".length));
      continue;
    }

    if (token === "--help" || token === "-h") {
      printHelp();
      process.exit(0);
    }

    throw new Error(`Unknown argument: ${token}`);
  }

  if (!Number.isInteger(args.pageSize) || args.pageSize < 1 || args.pageSize > MAX_PAGE_SIZE) {
    throw new Error(`--page-size must be an integer from 1 to ${MAX_PAGE_SIZE}.`);
  }

  return args;
}

function printHelp() {
  console.log(`
${TASK_ID} — governed read-only Sales All Meters reassessment against schema 1.1.0

Usage:
  node scripts/tools/sales-all/read_sales_all_meters_dev_v1.js \\
    --service-account "C:\\dev\\secrets\\ireps2-b33892e25c20.json"

Options:
  --service-account <path>  One of the two approved ireps2 service-account files.
  --page-size <1-${MAX_PAGE_SIZE}>  Document-ID page size. Default: ${DEFAULT_PAGE_SIZE}.
  --help                    Show this help.

The script reads only:
  ${TARGET_PROJECT_ID} / ${TARGET_COLLECTION}

It performs zero Firestore writes.
`);
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
    selectedPath = APPROVED_SERVICE_ACCOUNT_PATHS.find((candidate) => fs.existsSync(candidate));
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

  let raw;
  try {
    raw = await fsp.readFile(resolvedPath, "utf8");
  } catch (error) {
    throw new Error(`Unable to read service-account file ${resolvedPath}: ${error.message}`);
  }

  let serviceAccount;
  try {
    serviceAccount = JSON.parse(raw);
  } catch (error) {
    throw new Error(`Service-account file is not valid JSON: ${error.message}`);
  }

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
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }

  if (
    value instanceof Timestamp ||
    value instanceof GeoPoint ||
    value instanceof DocumentReference ||
    Buffer.isBuffer(value) ||
    value instanceof Uint8Array ||
    value instanceof Date
  ) {
    return false;
  }

  const ctorName = value?.constructor?.name;
  if (ctorName === "VectorValue") {
    return false;
  }

  return true;
}

function firestoreTypeOf(value) {
  if (value === null) return "null";
  if (typeof value === "string") return "string";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "bigint") return "integer";
  if (typeof value === "number") return "double";
  if (value instanceof Timestamp) return "timestamp";
  if (value instanceof GeoPoint) return "geopoint";
  if (value instanceof DocumentReference) return "reference";
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) return "bytes";
  if (Array.isArray(value)) return "array";
  if (value instanceof Date) return "date_fallback";
  if (value?.constructor?.name === "VectorValue") return "vector";
  if (isPlainMap(value)) return "map";
  return `unknown:${value?.constructor?.name || typeof value}`;
}

/**
 * Encodes every Firestore value in a tagged envelope so JSONL preserves
 * integer-vs-double and all Firestore special types.
 */
function encodeFirestoreValue(value) {
  const type = firestoreTypeOf(value);

  switch (type) {
    case "null":
      return { __firestoreType: "null", value: null };
    case "string":
      return { __firestoreType: "string", value };
    case "boolean":
      return { __firestoreType: "boolean", value };
    case "integer":
      return { __firestoreType: "integer", value: value.toString() };
    case "double": {
      let encoded = value;
      if (Number.isNaN(value)) encoded = "NaN";
      else if (value === Infinity) encoded = "Infinity";
      else if (value === -Infinity) encoded = "-Infinity";
      else if (Object.is(value, -0)) encoded = "-0";
      return { __firestoreType: "double", value: encoded };
    }
    case "timestamp":
      return {
        __firestoreType: "timestamp",
        seconds: String(value.seconds),
        nanoseconds: value.nanoseconds,
        iso: value.toDate().toISOString(),
      };
    case "geopoint":
      return {
        __firestoreType: "geopoint",
        latitude: value.latitude,
        longitude: value.longitude,
      };
    case "reference":
      return {
        __firestoreType: "reference",
        path: value.path,
        projectId: value.firestore?.projectId || null,
        databaseId: value.firestore?.databaseId || "(default)",
      };
    case "bytes":
      return {
        __firestoreType: "bytes",
        base64: Buffer.from(value).toString("base64"),
      };
    case "array":
      return {
        __firestoreType: "array",
        value: value.map((item) => encodeFirestoreValue(item)),
      };
    case "map": {
      const encodedMap = {};
      for (const key of Object.keys(value).sort()) {
        encodedMap[key] = encodeFirestoreValue(value[key]);
      }
      return { __firestoreType: "map", value: encodedMap };
    }
    case "vector": {
      const vectorValues = typeof value.toArray === "function" ? value.toArray() : [];
      return {
        __firestoreType: "vector",
        value: vectorValues.map((item) => ({
          __firestoreType: "double",
          value: item,
        })),
      };
    }
    case "date_fallback":
      return {
        __firestoreType: "date_fallback",
        iso: value.toISOString(),
      };
    default:
      return {
        __firestoreType: type,
        value: String(value),
      };
  }
}

function appendPath(parent, key) {
  if (!parent) {
    return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key)
      ? key
      : `[${JSON.stringify(key)}]`;
  }

  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key)
    ? `${parent}.${key}`
    : `${parent}[${JSON.stringify(key)}]`;
}

function createFieldInventoryTracker() {
  return new Map();
}

function recordInventoryValue(inventory, fieldPath, value, seenInDocument) {
  const type = firestoreTypeOf(value);
  let entry = inventory.get(fieldPath);

  if (!entry) {
    entry = {
      path: fieldPath,
      depth: (fieldPath.match(/\.|\[/g) || []).length + 1,
      occurrences: 0,
      documentsWithField: 0,
      types: new Map(),
    };
    inventory.set(fieldPath, entry);
  }

  entry.occurrences += 1;
  entry.types.set(type, (entry.types.get(type) || 0) + 1);

  if (!seenInDocument.has(fieldPath)) {
    entry.documentsWithField += 1;
    seenInDocument.add(fieldPath);
  }

  if (type === "map") {
    for (const key of Object.keys(value).sort()) {
      recordInventoryValue(inventory, appendPath(fieldPath, key), value[key], seenInDocument);
    }
  } else if (type === "array") {
    for (const item of value) {
      recordInventoryValue(inventory, `${fieldPath}[]`, item, seenInDocument);
    }
  }
}

function mapIncrement(map, key, sampleDocumentId = null) {
  let entry = map.get(key);
  if (!entry) {
    entry = { count: 0, sampleDocumentIds: [] };
    map.set(key, entry);
  }
  entry.count += 1;
  if (
    sampleDocumentId &&
    entry.sampleDocumentIds.length < MAX_SAMPLES_PER_GROUP &&
    !entry.sampleDocumentIds.includes(sampleDocumentId)
  ) {
    entry.sampleDocumentIds.push(sampleDocumentId);
  }
}

function summarizedValueKey(value) {
  const type = firestoreTypeOf(value);

  if (type === "string" || type === "boolean") {
    return JSON.stringify({ type, value });
  }
  if (type === "integer") {
    return JSON.stringify({ type, value: value.toString() });
  }
  if (type === "double") {
    const numberValue = Number.isFinite(value) ? value : String(value);
    return JSON.stringify({ type, value: numberValue });
  }
  if (type === "null") {
    return JSON.stringify({ type, value: null });
  }

  return JSON.stringify({ type, value: null });
}

function parseSummarizedValueKey(key) {
  return JSON.parse(key);
}

function normalizeCanonicalMeterNo(value) {
  return String(value ?? "")
    .trim()
    .replace(/[^A-Za-z0-9]/g, "")
    .toUpperCase();
}

function isCanonicalIdentity(value) {
  return typeof value === "string" && value.length > 0 && CANONICAL_ID_RE.test(value);
}

function monthIndex(month) {
  if (!CANONICAL_MONTH_RE.test(month)) return null;
  const [yearText, monthText] = month.split("-");
  return Number(yearText) * 12 + Number(monthText) - 1;
}

function areMonthsContiguous(months) {
  if (months.length === 0) return false;
  const indexes = months.map(monthIndex);
  if (indexes.some((value) => value === null)) return false;
  for (let i = 1; i < indexes.length; i += 1) {
    if (indexes[i] !== indexes[i - 1] + 1) return false;
  }
  return true;
}

function isTimezoneAwareIso(value) {
  if (typeof value !== "string" || !TIMEZONE_SUFFIX_RE.test(value)) return false;
  const parsed = new Date(value);
  return !Number.isNaN(parsed.getTime());
}

function addIssue(issueSet, code, detail = null) {
  const key = detail === null ? code : `${code}:${detail}`;
  issueSet.add(key);
}

function assessDocument(documentId, data) {
  const issues = new Set();
  const identityIssues = new Set();
  const rootFields = Object.keys(data).sort();

  for (const field of CANONICAL_ROOT_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(data, field)) {
      addIssue(issues, "ROOT_MISSING_FIELD", field);
    }
  }

  for (const field of rootFields) {
    if (!CANONICAL_ROOT_FIELD_SET.has(field)) {
      addIssue(issues, "ROOT_UNEXPECTED_FIELD", field);
    }
  }

  if (!isCanonicalIdentity(documentId)) {
    addIssue(identityIssues, "DOCUMENT_ID_NOT_CANONICAL");
    addIssue(issues, "IDENTITY_DOCUMENT_ID_NOT_CANONICAL");
  }

  const master = data.master;
  if (!isPlainMap(master)) {
    addIssue(issues, "MASTER_NOT_MAP");
  } else {
    for (const key of Object.keys(master)) {
      if (!CANONICAL_MASTER_FIELD_SET.has(key)) {
        addIssue(issues, "MASTER_UNEXPECTED_FIELD", key);
      }
    }

    if (!Object.prototype.hasOwnProperty.call(master, "id")) {
      addIssue(issues, "MASTER_MISSING_ID");
      addIssue(identityIssues, "MASTER_ID_MISSING");
    } else if (typeof master.id !== "string") {
      addIssue(issues, "MASTER_ID_NOT_STRING");
      addIssue(identityIssues, "MASTER_ID_NOT_STRING");
    } else {
      if (!isCanonicalIdentity(master.id)) {
        addIssue(issues, "MASTER_ID_NOT_CANONICAL");
        addIssue(identityIssues, "MASTER_ID_NOT_CANONICAL");
      }
      if (master.id !== documentId) {
        addIssue(identityIssues, "DOCUMENT_ID_VS_MASTER_ID_MISMATCH");
        addIssue(issues, "IDENTITY_DOCUMENT_ID_VS_MASTER_ID_MISMATCH");
      }
    }

    if (!Object.prototype.hasOwnProperty.call(master, "visibility")) {
      addIssue(issues, "MASTER_VISIBILITY_MISSING");
    } else if (typeof master.visibility !== "string") {
      addIssue(issues, "MASTER_VISIBILITY_NOT_STRING");
    } else if (!CANONICAL_VISIBILITY_VALUES.has(master.visibility)) {
      addIssue(issues, "MASTER_VISIBILITY_INVALID_VALUE", master.visibility);
    }
  }

  if (typeof data.meterNo !== "string") {
    addIssue(issues, "METER_NO_NOT_STRING");
  } else if (data.meterNo.length === 0) {
    addIssue(issues, "METER_NO_BLANK");
  }

  if (typeof data.meterNoNormalized !== "string") {
    addIssue(issues, "METER_NO_NORMALIZED_NOT_STRING");
    addIssue(identityIssues, "METER_NO_NORMALIZED_NOT_STRING");
  } else {
    if (!isCanonicalIdentity(data.meterNoNormalized)) {
      addIssue(issues, "METER_NO_NORMALIZED_NOT_CANONICAL");
      addIssue(identityIssues, "METER_NO_NORMALIZED_NOT_CANONICAL");
    }
    if (data.meterNoNormalized !== documentId) {
      addIssue(identityIssues, "DOCUMENT_ID_VS_METER_NO_NORMALIZED_MISMATCH");
      addIssue(issues, "IDENTITY_DOCUMENT_ID_VS_METER_NO_NORMALIZED_MISMATCH");
    }
    if (typeof master?.id === "string" && master.id !== data.meterNoNormalized) {
      addIssue(identityIssues, "MASTER_ID_VS_METER_NO_NORMALIZED_MISMATCH");
      addIssue(issues, "IDENTITY_MASTER_ID_VS_METER_NO_NORMALIZED_MISMATCH");
    }
  }

  if (typeof data.meterNo === "string" && typeof data.meterNoNormalized === "string") {
    const derived = normalizeCanonicalMeterNo(data.meterNo);
    if (derived !== data.meterNoNormalized) {
      addIssue(identityIssues, "METER_NO_NORMALIZATION_MISMATCH");
      addIssue(issues, "IDENTITY_METER_NO_NORMALIZATION_MISMATCH");
    }
  }

  if (typeof data.provider !== "string") {
    addIssue(issues, "PROVIDER_NOT_STRING");
  } else if (data.provider !== "conlog") {
    addIssue(issues, "PROVIDER_NOT_CONLOG", data.provider);
  }

  if (typeof data.customerNo !== "string") {
    addIssue(issues, "CUSTOMER_NO_NOT_STRING");
  }

  if (typeof data.accountNo !== "string") {
    addIssue(issues, "ACCOUNT_NO_NOT_STRING");
  }

  const totalIsInteger = typeof data.totalAmountC === "bigint";
  if (!totalIsInteger) {
    addIssue(issues, "TOTAL_AMOUNT_C_NOT_FIRESTORE_INTEGER");
  } else if (data.totalAmountC < 0n) {
    addIssue(issues, "TOTAL_AMOUNT_C_NEGATIVE");
  }

  let monthlySum = 0n;
  let monthlyValuesAreIntegers = true;
  let latestPositiveMonth = null;
  let monthKeys = [];

  if (!isPlainMap(data.monthlyTotalsC)) {
    addIssue(issues, "MONTHLY_TOTALS_C_NOT_MAP");
  } else {
    monthKeys = Object.keys(data.monthlyTotalsC).sort();

    if (monthKeys.length === 0) {
      addIssue(issues, "MONTHLY_TOTALS_C_EMPTY");
    }

    for (const month of monthKeys) {
      if (!CANONICAL_MONTH_RE.test(month)) {
        addIssue(issues, "MONTH_KEY_INVALID", month);
      }

      const amount = data.monthlyTotalsC[month];
      if (typeof amount !== "bigint") {
        monthlyValuesAreIntegers = false;
        addIssue(issues, "MONTH_VALUE_NOT_FIRESTORE_INTEGER", month);
        continue;
      }

      if (amount < 0n) {
        addIssue(issues, "MONTH_VALUE_NEGATIVE", month);
      }

      monthlySum += amount;
      if (amount > 0n && CANONICAL_MONTH_RE.test(month)) {
        latestPositiveMonth = month;
      }
    }

    if (monthKeys.length > 0 && !areMonthsContiguous(monthKeys)) {
      addIssue(issues, "MONTH_RANGE_NOT_CONTIGUOUS");
    }
  }

  if (totalIsInteger && monthlyValuesAreIntegers && isPlainMap(data.monthlyTotalsC)) {
    if (data.totalAmountC !== monthlySum) {
      addIssue(issues, "TOTAL_AMOUNT_C_DOES_NOT_EQUAL_MONTHLY_SUM");
    }
  }

  const lastPurchase = data.lastPurchaseAtISO;
  const daysSince = data.daysSinceLastPurchase;

  if (!(lastPurchase === null || typeof lastPurchase === "string")) {
    addIssue(issues, "LAST_PURCHASE_AT_ISO_INVALID_TYPE");
  }

  if (!(daysSince === null || typeof daysSince === "bigint")) {
    addIssue(issues, "DAYS_SINCE_LAST_PURCHASE_INVALID_TYPE");
  } else if (typeof daysSince === "bigint" && daysSince < 0n) {
    addIssue(issues, "DAYS_SINCE_LAST_PURCHASE_NEGATIVE");
  }

  if (totalIsInteger && data.totalAmountC > 0n) {
    if (typeof lastPurchase !== "string" || lastPurchase.length === 0) {
      addIssue(issues, "POSITIVE_TOTAL_LAST_PURCHASE_MISSING");
    } else if (!isTimezoneAwareIso(lastPurchase)) {
      addIssue(issues, "LAST_PURCHASE_AT_ISO_INVALID_OR_TIMEZONE_FREE");
    } else if (latestPositiveMonth) {
      const utcPurchaseMonth = new Date(lastPurchase).toISOString().slice(0, 7);
      if (utcPurchaseMonth !== latestPositiveMonth) {
        addIssue(issues, "LAST_PURCHASE_MONTH_MISMATCH", `${utcPurchaseMonth}->${latestPositiveMonth}`);
      }
    }

    if (typeof daysSince !== "bigint") {
      addIssue(issues, "POSITIVE_TOTAL_DAYS_SINCE_MISSING");
    }
  }

  if (totalIsInteger && data.totalAmountC === 0n) {
    if (lastPurchase !== null) {
      addIssue(issues, "ZERO_TOTAL_LAST_PURCHASE_NOT_NULL");
    }
    if (daysSince !== null) {
      addIssue(issues, "ZERO_TOTAL_DAYS_SINCE_NOT_NULL");
    }
  }

  return {
    rootFields,
    rootShape: rootFields.join("|"),
    monthKeys,
    monthShape: monthKeys.join("|"),
    issues: Array.from(issues).sort(),
    identityIssues: Array.from(identityIssues).sort(),
  };
}

function issueCodeFromDetailedIssue(issue) {
  const colonIndex = issue.indexOf(":");
  return colonIndex === -1 ? issue : issue.slice(0, colonIndex);
}

function buildTypedShape(data) {
  const entries = [];

  function visit(parent, value) {
    const type = firestoreTypeOf(value);
    entries.push(`${parent}:${type}`);

    if (type === "map") {
      for (const key of Object.keys(value).sort()) {
        visit(appendPath(parent, key), value[key]);
      }
    } else if (type === "array") {
      for (const item of value) {
        visit(`${parent}[]`, item);
      }
    }
  }

  for (const key of Object.keys(data).sort()) {
    visit(appendPath("", key), data[key]);
  }

  return Array.from(new Set(entries)).sort().join("|");
}

function sortMapEntries(map, mapper) {
  return Array.from(map.entries())
    .map(([key, value]) => mapper(key, value))
    .sort((a, b) => b.count - a.count || JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

function serializeFieldInventory(inventory, documentCount) {
  const fields = Array.from(inventory.values())
    .map((entry) => ({
      path: entry.path,
      depth: entry.depth,
      occurrences: entry.occurrences,
      documentsWithField: entry.documentsWithField,
      documentsMissingField: documentCount - entry.documentsWithField,
      types: Array.from(entry.types.entries())
        .map(([type, count]) => ({ type, count }))
        .sort((a, b) => b.count - a.count || a.type.localeCompare(b.type)),
    }))
    .sort((a, b) => a.path.localeCompare(b.path));

  return {
    taskId: TASK_ID,
    schemaVersion: "sales_all_meters/1.0.0",
    collection: TARGET_COLLECTION,
    documentCount,
    rootFields: fields.filter((entry) => entry.depth === 1),
    nestedFieldPaths: fields.filter((entry) => entry.depth > 1),
    allFieldPaths: fields,
  };
}

async function writeJsonAtomic(filePath, value) {
  const tempPath = `${filePath}.tmp`;
  await fsp.writeFile(tempPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await fsp.rename(tempPath, filePath);
}

async function writeStreamLine(stream, line) {
  if (!stream.write(line)) {
    await once(stream, "drain");
  }
}

async function closeWriteStream(stream) {
  stream.end();
  await once(stream, "finish");
}

function safeError(error) {
  return {
    name: error?.name || "Error",
    message: error?.message || String(error),
    code: error?.code || null,
    stack: error?.stack || null,
  };
}

async function sha256File(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    const input = fs.createReadStream(filePath);
    input.on("error", reject);
    input.on("data", (chunk) => hash.update(chunk));
    input.on("end", () => resolve(hash.digest("hex")));
  });
}

async function main() {
  const startedAt = utcNowIso();
  const runId = `${TASK_ID}__${compactUtcTimestamp(new Date())}`;
  const scriptDir = __dirname;
  const reportsRoot = path.join(scriptDir, "reports");
  const runDir = path.join(reportsRoot, runId);

  let app = null;
  let rawStream = null;
  let rawTempPath = null;
  let runReportPath = null;
  let selectedCredential = null;

  const runState = {
    taskId: TASK_ID,
    runId,
    scriptVersion: SCRIPT_VERSION,
    mode: "READ_ONLY",
    firestoreWrites: 0,
    targetProjectId: TARGET_PROJECT_ID,
    targetCollection: TARGET_COLLECTION,
    startedAt,
    completedAt: null,
    status: "RUNNING",
  };

  try {
    const args = parseArgs(process.argv.slice(2));

    // Local report creation is allowed before Firebase initialization so even a
    // credential/project-lock failure can be governed by run_report.json.
    await fsp.mkdir(runDir, { recursive: true });
    runReportPath = path.join(runDir, "run_report.json");

    selectedCredential = await selectAndValidateServiceAccount(args.serviceAccountPath);

    const rawExportPath = path.join(runDir, "raw_export.jsonl");
    rawTempPath = `${rawExportPath}.tmp`;
    const fieldInventoryPath = path.join(runDir, "field_inventory.json");
    const collectionProfilePath = path.join(runDir, "collection_profile.json");

    console.log(`[LOCK] Project: ${TARGET_PROJECT_ID}`);
    console.log(`[LOCK] Collection: ${TARGET_COLLECTION}`);
    console.log(`[MODE] READ-ONLY — zero Firestore writes`);
    console.log(`[KEY] ${selectedCredential.basename} (project_id validated)`);
    console.log(`[OUT] ${runDir}`);

    app = initializeApp(
      {
        credential: cert(selectedCredential.serviceAccount),
        projectId: TARGET_PROJECT_ID,
      },
      runId,
    );

    const db = getFirestore(app);

    // Preserve exact Firestore integer values as BigInt and distinguish them
    // from Firestore doubles during profiling and JSONL encoding.
    db.settings({ useBigInt: true });

    const collectionRef = db.collection(TARGET_COLLECTION);
    const inventory = createFieldInventoryTracker();
    const rootShapes = new Map();
    const typedShapes = new Map();
    const monthShapes = new Map();
    const providerValues = new Map();
    const visibilityValues = new Map();
    const issueCounts = new Map();
    const identityIssueCounts = new Map();
    const identityValues = new Map();
    const nonCanonicalDocuments = [];
    const identityMismatchDocuments = [];

    let documentsRead = 0;
    let pagesRead = 0;
    let lastDocumentId = null;
    let firstDocumentId = null;

    rawStream = fs.createWriteStream(rawTempPath, {
      encoding: "utf8",
      flags: "wx",
    });

    rawStream.on("error", (error) => {
      console.error(`[RAW EXPORT ERROR] ${error.message}`);
    });

    while (true) {
      let query = collectionRef.orderBy(FieldPath.documentId()).limit(args.pageSize);
      if (lastDocumentId !== null) {
        query = query.startAfter(lastDocumentId);
      }

      const snapshot = await query.get();
      if (snapshot.empty) break;

      pagesRead += 1;

      for (const doc of snapshot.docs) {
        const data = doc.data();
        const documentId = doc.id;

        if (firstDocumentId === null) firstDocumentId = documentId;
        lastDocumentId = documentId;
        documentsRead += 1;

        const seenPaths = new Set();
        for (const key of Object.keys(data).sort()) {
          recordInventoryValue(inventory, appendPath("", key), data[key], seenPaths);
        }

        const assessment = assessDocument(documentId, data);
        const typedShape = buildTypedShape(data);

        mapIncrement(rootShapes, assessment.rootShape, documentId);
        mapIncrement(typedShapes, typedShape, documentId);
        mapIncrement(monthShapes, assessment.monthShape, documentId);
        mapIncrement(providerValues, summarizedValueKey(data.provider), documentId);
        mapIncrement(
          visibilityValues,
          summarizedValueKey(
            isPlainMap(data.master) && Object.prototype.hasOwnProperty.call(data.master, "visibility")
              ? data.master.visibility
              : null,
          ),
          documentId,
        );

        if (typeof data.meterNoNormalized === "string") {
          let ids = identityValues.get(data.meterNoNormalized);
          if (!ids) {
            ids = [];
            identityValues.set(data.meterNoNormalized, ids);
          }
          ids.push(documentId);
        }

        for (const issue of assessment.issues) {
          const code = issueCodeFromDetailedIssue(issue);
          issueCounts.set(code, (issueCounts.get(code) || 0) + 1);
        }

        for (const issue of assessment.identityIssues) {
          const code = issueCodeFromDetailedIssue(issue);
          identityIssueCounts.set(code, (identityIssueCounts.get(code) || 0) + 1);
        }

        if (assessment.issues.length > 0) {
          nonCanonicalDocuments.push({ documentId, issues: assessment.issues });
        }

        if (assessment.identityIssues.length > 0) {
          identityMismatchDocuments.push({
            documentId,
            issues: assessment.identityIssues,
            masterId: typeof data.master?.id === "string" ? data.master.id : null,
            meterNo: typeof data.meterNo === "string" ? data.meterNo : null,
            meterNoNormalized:
              typeof data.meterNoNormalized === "string" ? data.meterNoNormalized : null,
          });
        }

        const rawRecord = {
          taskId: TASK_ID,
          projectId: TARGET_PROJECT_ID,
          collection: TARGET_COLLECTION,
          documentId,
          documentPath: doc.ref.path,
          readTime:
            doc.readTime instanceof Timestamp ? doc.readTime.toDate().toISOString() : null,
          createTime:
            doc.createTime instanceof Timestamp ? doc.createTime.toDate().toISOString() : null,
          updateTime:
            doc.updateTime instanceof Timestamp ? doc.updateTime.toDate().toISOString() : null,
          data: encodeFirestoreValue(data),
        };

        await writeStreamLine(rawStream, `${JSON.stringify(rawRecord)}\n`);
      }

      console.log(
        `[READ] page=${pagesRead} pageDocs=${snapshot.size} totalDocs=${documentsRead} lastId=${lastDocumentId}`,
      );

      if (snapshot.size < args.pageSize) break;
    }

    await closeWriteStream(rawStream);
    rawStream = null;
    await fsp.rename(rawTempPath, rawExportPath);
    rawTempPath = null;

    const duplicateIdentityValues = Array.from(identityValues.entries())
      .filter(([, ids]) => ids.length > 1)
      .map(([meterNoNormalized, documentIds]) => ({
        meterNoNormalized,
        count: documentIds.length,
        documentIds: documentIds.slice().sort(),
      }))
      .sort((a, b) => b.count - a.count || a.meterNoNormalized.localeCompare(b.meterNoNormalized));

    if (duplicateIdentityValues.length > 0) {
      identityIssueCounts.set("DUPLICATE_METER_NO_NORMALIZED", duplicateIdentityValues.length);
    }

    const fieldInventory = serializeFieldInventory(inventory, documentsRead);

    const rootFieldsSummary = fieldInventory.rootFields.map((entry) => ({
      path: entry.path,
      documentsWithField: entry.documentsWithField,
      documentsMissingField: entry.documentsMissingField,
      types: entry.types,
    }));

    const nestedFieldPathsSummary = fieldInventory.nestedFieldPaths.map((entry) => ({
      path: entry.path,
      documentsWithField: entry.documentsWithField,
      types: entry.types,
    }));

    const providerSummary = sortMapEntries(providerValues, (key, entry) => ({
      ...parseSummarizedValueKey(key),
      count: entry.count,
      sampleDocumentIds: entry.sampleDocumentIds,
    }));

    const visibilitySummary = sortMapEntries(visibilityValues, (key, entry) => ({
      ...parseSummarizedValueKey(key),
      meaning:
        parseSummarizedValueKey(key).type === "null"
          ? "master.visibility absent"
          : "master.visibility present",
      count: entry.count,
      sampleDocumentIds: entry.sampleDocumentIds,
    }));

    const collectionProfile = {
      taskId: TASK_ID,
      runId,
      target: {
        projectId: TARGET_PROJECT_ID,
        collection: TARGET_COLLECTION,
      },
      mode: "READ_ONLY",
      firestoreWrites: 0,
      canonicalSchema: {
        name: "sales_all_meters",
        version: "1.1.0",
        status: "LOCKED",
        exactRootFields: CANONICAL_ROOT_FIELDS,
        masterFields: ["id", "visibility (required; VISIBLE | INVISIBLE)"],
        identityRule: "document ID = master.id = meterNoNormalized",
        providerRule: "provider must equal conlog",
      },
      scan: {
        pageSize: args.pageSize,
        pagesRead,
        documentsRead,
        firstDocumentId,
        lastDocumentId,
        ordering: "FieldPath.documentId() ascending",
        pagination: "startAfter(lastDocumentId)",
      },
      rootFields: rootFieldsSummary,
      nestedFieldPaths: nestedFieldPathsSummary,
      documentShapes: {
        distinctRootShapes: rootShapes.size,
        rootShapes: sortMapEntries(rootShapes, (signature, entry) => ({
          signature,
          fields: signature ? signature.split("|") : [],
          count: entry.count,
          sampleDocumentIds: entry.sampleDocumentIds,
        })),
        distinctTypedShapes: typedShapes.size,
        typedShapes: sortMapEntries(typedShapes, (signature, entry) => ({
          signature,
          count: entry.count,
          sampleDocumentIds: entry.sampleDocumentIds,
        })),
      },
      providerValues: providerSummary,
      masterVisibilityValues: visibilitySummary,
      monthlyTotalsProfiles: {
        distinctMonthKeySets: monthShapes.size,
        monthKeySets: sortMapEntries(monthShapes, (signature, entry) => ({
          signature,
          months: signature ? signature.split("|") : [],
          count: entry.count,
          sampleDocumentIds: entry.sampleDocumentIds,
        })),
      },
      identityMismatches: {
        documentsWithIdentityMismatch: identityMismatchDocuments.length,
        issueCounts: Array.from(identityIssueCounts.entries())
          .map(([code, count]) => ({ code, count }))
          .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code)),
        duplicateMeterNoNormalizedValues: duplicateIdentityValues,
        documents: identityMismatchDocuments,
      },
      canonicalAssessment: {
        canonicalDocuments: documentsRead - nonCanonicalDocuments.length,
        nonCanonicalDocuments: nonCanonicalDocuments.length,
        issueCounts: Array.from(issueCounts.entries())
          .map(([code, count]) => ({ code, count }))
          .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code)),
        documents: nonCanonicalDocuments,
      },
      validationScope: {
        validated: [
          "exact root field set",
          "master field set and required visibility enum value",
          "Firestore value types using useBigInt=true",
          "deterministic identity and meter normalization",
          "provider values",
          "customer/account string types",
          "non-negative integer monetary values",
          "monthly key syntax and per-document contiguity",
          "totalAmountC equals monthlyTotalsC sum",
          "recency type/null/timezone consistency",
          "last-purchase UTC month equals latest positive month",
        ],
        notValidatedWithoutFrozenStage06Contract: [
          "month key set against the approved Stage 06 manifest includedMonths",
          "daysSinceLastPurchase against the approved Stage 06 asOfDate",
          "field provenance, including which writer created master.visibility",
        ],
      },
    };

    await writeJsonAtomic(fieldInventoryPath, fieldInventory);
    await writeJsonAtomic(collectionProfilePath, collectionProfile);

    const rawStats = await fsp.stat(rawExportPath);
    const rawSha256 = await sha256File(rawExportPath);

    runState.status = "COMPLETED";
    runState.completedAt = utcNowIso();
    runState.durationMs = Date.parse(runState.completedAt) - Date.parse(startedAt);
    runState.serviceAccount = {
      file: selectedCredential.basename,
      projectId: selectedCredential.serviceAccount.project_id,
      clientEmail: selectedCredential.serviceAccount.client_email,
    };
    runState.reads = {
      collection: TARGET_COLLECTION,
      pagesRead,
      documentsRead,
      firstDocumentId,
      lastDocumentId,
      pageSize: args.pageSize,
    };
    runState.assessment = {
      canonicalDocuments: collectionProfile.canonicalAssessment.canonicalDocuments,
      nonCanonicalDocuments: collectionProfile.canonicalAssessment.nonCanonicalDocuments,
      documentsWithIdentityMismatch:
        collectionProfile.identityMismatches.documentsWithIdentityMismatch,
      distinctRootShapes: collectionProfile.documentShapes.distinctRootShapes,
      distinctTypedShapes: collectionProfile.documentShapes.distinctTypedShapes,
      distinctProviderValues: providerSummary.length,
      distinctMasterVisibilityValues: visibilitySummary.length,
    };
    runState.outputs = {
      runDirectory: runDir,
      rawExport: {
        file: "raw_export.jsonl",
        bytes: rawStats.size,
        sha256: rawSha256,
        encoding: "UTF-8 JSON Lines",
        firestoreTypeEncoding: "tagged envelopes; integers preserved as decimal strings",
      },
      fieldInventory: "field_inventory.json",
      collectionProfile: "collection_profile.json",
      runReport: "run_report.json",
    };
    runState.governance = {
      projectLockPassed: true,
      serviceAccountProjectIdValidated: true,
      collectionLockPassed: true,
      firestoreWritesAttempted: 0,
      firestoreWritesCompleted: 0,
      writerCodeChanged: false,
      newFirestoreCollectionCreated: false,
    };

    await writeJsonAtomic(runReportPath, runState);

    console.log(`[DONE] documents=${documentsRead} pages=${pagesRead}`);
    console.log(
      `[ASSESSMENT] canonical=${runState.assessment.canonicalDocuments} nonCanonical=${runState.assessment.nonCanonicalDocuments} identityMismatchDocs=${runState.assessment.documentsWithIdentityMismatch}`,
    );
    console.log(`[REPORT] ${runReportPath}`);
  } catch (error) {
    if (rawStream) {
      rawStream.destroy();
      rawStream = null;
    }

    runState.status = "FAILED";
    runState.completedAt = utcNowIso();
    runState.durationMs = Date.parse(runState.completedAt) - Date.parse(startedAt);
    runState.error = safeError(error);
    runState.governance = {
      projectLockPassed:
        selectedCredential?.serviceAccount?.project_id === TARGET_PROJECT_ID || false,
      serviceAccountProjectIdValidated:
        selectedCredential?.serviceAccount?.project_id === TARGET_PROJECT_ID || false,
      collectionLockPassed: true,
      firestoreWritesAttempted: 0,
      firestoreWritesCompleted: 0,
      writerCodeChanged: false,
      newFirestoreCollectionCreated: false,
    };

    if (rawTempPath && fs.existsSync(rawTempPath)) {
      const partialPath = rawTempPath.replace(/\.tmp$/, ".partial");
      try {
        await fsp.rename(rawTempPath, partialPath);
        runState.partialRawExport = partialPath;
      } catch {
        // Keep the original error as the primary failure.
      }
    }

    if (runReportPath) {
      try {
        await writeJsonAtomic(runReportPath, runState);
      } catch (reportError) {
        console.error(`[REPORT FAILURE] ${reportError.message}`);
      }
    }

    console.error(`[FAILED] ${error.message}`);
    process.exitCode = 1;
  } finally {
    if (app) {
      try {
        await deleteApp(app);
      } catch (error) {
        console.error(`[CLEANUP WARNING] ${error.message}`);
      }
    }
  }
}

main();
