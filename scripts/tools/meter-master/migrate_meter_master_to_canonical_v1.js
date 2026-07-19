const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const {
  applicationDefault,
  cert,
  getApp,
  getApps,
  initializeApp,
} = require("firebase-admin/app");
const {
  DocumentReference,
  FieldPath,
  FieldValue,
  GeoPoint,
  Timestamp,
  getFirestore,
} = require("firebase-admin/firestore");

const SCRIPT_PATH = __filename;
const SCRIPT_DIR = __dirname;
const SCRIPT_NAME = "migrate_meter_master_to_canonical_v1.js";
const SCRIPT_VERSION = "1.0.3";

const TARGET_PROJECT_ID = "ireps2";
const COLLECTION_NAME = "meter_master";
const DEFAULT_SERVICE_ACCOUNT_PATH = "C:\\dev\\secrets\\ireps2-e72fd9dc94de.json";
const DEFAULT_REPORT_DIR = path.join(SCRIPT_DIR, "reports");
const CONFIRM_TEXT = "MIGRATE_IREPS2_METER_MASTER_CANONICAL_V1";
const MIGRATION_UID = "SYSTEM";
const MIGRATION_USER = "METER MASTER CANONICAL MIGRATION";

const CANONICAL_ROOT_FIELDS = Object.freeze([
  "lmPcode",
  "meterNo",
  "meterType",
  "customerNo",
  "accountNo",
  "refs",
  "metadata",
]);

const CONFLICT_CODES = Object.freeze({
  DOCUMENT_ID_NONCANONICAL: "MM_DOCUMENT_ID_NONCANONICAL",
  NORMALIZED_IDENTITY_CONFLICT: "MM_NORMALIZED_IDENTITY_CONFLICT",
  LM_CONFLICT: "MM_LM_CONFLICT",
  METER_TYPE_CONFLICT: "MM_METER_TYPE_CONFLICT",
  SALES_REFERENCE_CONFLICT: "MM_SALES_REFERENCE_CONFLICT",
  SALES_PROVIDER_CONFLICT: "MM_SALES_PROVIDER_CONFLICT",
  CREATED_METADATA_INVALID: "MM_CREATED_METADATA_INVALID",
  GOVERNED_FIELD_TYPE_INVALID: "MM_GOVERNED_FIELD_TYPE_INVALID",
  DOCUMENT_SHAPE_UNSAFE: "MM_DOCUMENT_SHAPE_UNSAFE",
  CANONICAL_FIELD_MISSING: "MM_CANONICAL_FIELD_MISSING",
  TRANSACTION_PRECONDITION_CHANGED: "MM_TRANSACTION_PRECONDITION_CHANGED",
  RECORD_WRITE_FAILED: "MM_RECORD_WRITE_FAILED",
});

function parseArgs(argv) {
  const args = {
    projectId: TARGET_PROJECT_ID,
    serviceAccountPath: DEFAULT_SERVICE_ACCOUNT_PATH,
    useAdc: false,
    execute: false,
    confirm: "",
    planPath: "",
    reportDir: DEFAULT_REPORT_DIR,
    onlyIds: [],
    help: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--project") {
      args.projectId = argv[++i] || "";
    } else if (arg === "--service-account") {
      args.serviceAccountPath = argv[++i] || "";
    } else if (arg === "--adc") {
      args.useAdc = true;
    } else if (arg === "--execute") {
      args.execute = true;
    } else if (arg === "--confirm") {
      args.confirm = argv[++i] || "";
    } else if (arg === "--plan") {
      args.planPath = argv[++i] || "";
    } else if (arg === "--report-dir") {
      args.reportDir = argv[++i] || args.reportDir;
    } else if (arg === "--only-id") {
      const value = argv[++i] || "";
      if (value) args.onlyIds.push(value);
    } else if (arg === "--help" || arg === "-h") {
      args.help = true;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  return args;
}

function printHelp() {
  console.log(`
${SCRIPT_NAME} v${SCRIPT_VERSION}

Purpose:
  Export every selected ireps2 meter_master document, build a canonical migration plan,
  and optionally update the same Firestore document IDs in place.

Default mode is DRY RUN. Dry run performs no Firestore writes.

Run from C:\\dev\\ireps-pipeline-sales:

  node .\\scripts\\tools\\meter-master\\${SCRIPT_NAME} --project ireps2

Optional single-document pilot plan:

  node .\\scripts\\tools\\meter-master\\${SCRIPT_NAME} --project ireps2 --only-id 04085348573

Execute an approved frozen plan:

  node .\\scripts\\tools\\meter-master\\${SCRIPT_NAME} \\
    --project ireps2 \\
    --execute \\
    --plan ".\\scripts\\meter-master\\reports\\<run-folder>\\migration_plan.json" \\
    --confirm ${CONFIRM_TEXT}

Options:
  --project <id>             Must be exactly ${TARGET_PROJECT_ID}.
  --service-account <path>   Defaults to ${DEFAULT_SERVICE_ACCOUNT_PATH}.
  --adc                      Use Application Default Credentials instead of a JSON key.
  --only-id <docId>          Restrict planning to one ID. Repeatable. Stored in the plan.
  --report-dir <path>        Defaults to ${DEFAULT_REPORT_DIR}.
  --execute                  Apply the approved plan.
  --plan <path>              Required with --execute.
  --confirm <text>           Must equal ${CONFIRM_TEXT} with --execute.
  --help                     Show this help.

Safety rules:
  - Hard locked to ireps2.
  - Never creates, deletes, or renames Meter Master document IDs.
  - Dry run first; execution requires an approved plan and confirmation text.
  - Every update uses the source document updateTime as a Firestore precondition.
  - Conflicting records are skipped, reported, and do not stop other records.
  - A complete raw export is written before any planned write.
`);
}

function ensureDirectory(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function timestampToken(date = new Date()) {
  return date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function normalizeMeterNo(value) {
  const normalized = String(value ?? "").replace(/\s+/g, "").toUpperCase();
  return normalized;
}

function normalizeText(value) {
  if (value === null || value === undefined) return "";
  if (["string", "number", "bigint"].includes(typeof value)) {
    return String(value).trim();
  }
  return null;
}

function normalizeUpper(value) {
  const text = normalizeText(value);
  return text === null ? null : text.toUpperCase();
}

function normalizeLower(value) {
  const text = normalizeText(value);
  return text === null ? null : text.toLowerCase();
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

function isTimestamp(value) {
  return value instanceof Timestamp
    || Boolean(value)
      && typeof value.toDate === "function"
      && typeof value.toMillis === "function";
}

function toTimestampOrNull(value) {
  if (!value) return null;
  if (isTimestamp(value)) {
    if (Number.isInteger(value.seconds) && Number.isInteger(value.nanoseconds)) {
      return new Timestamp(value.seconds, value.nanoseconds);
    }
    return Timestamp.fromMillis(value.toMillis());
  }
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    return Timestamp.fromDate(value);
  }
  if (typeof value === "string") {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : Timestamp.fromDate(parsed);
  }
  if (isPlainObject(value) && value.__type === "timestamp" && typeof value.iso === "string") {
    const parsed = new Date(value.iso);
    return Number.isNaN(parsed.getTime()) ? null : Timestamp.fromDate(parsed);
  }
  if (isPlainObject(value) && typeof value.__time__ === "string") {
    const parsed = new Date(value.__time__);
    return Number.isNaN(parsed.getTime()) ? null : Timestamp.fromDate(parsed);
  }
  if (isPlainObject(value) && Number.isFinite(value.seconds)) {
    return new Timestamp(value.seconds, Number(value.nanoseconds || 0));
  }
  if (isPlainObject(value) && Number.isFinite(value._seconds)) {
    return new Timestamp(value._seconds, Number(value._nanoseconds || 0));
  }
  return null;
}

function serializeFirestore(value) {
  if (isTimestamp(value)) {
    return {
      __type: "timestamp",
      iso: value.toDate().toISOString(),
      seconds: value.seconds,
      nanoseconds: value.nanoseconds,
    };
  }
  if (value instanceof Date) {
    return { __type: "date", iso: value.toISOString() };
  }
  if (value instanceof GeoPoint) {
    return { __type: "geopoint", latitude: value.latitude, longitude: value.longitude };
  }
  if (value instanceof DocumentReference) {
    return { __type: "reference", path: value.path };
  }
  if (Buffer.isBuffer(value)) {
    return { __type: "bytes", base64: value.toString("base64") };
  }
  if (Array.isArray(value)) return value.map(serializeFirestore);
  if (value && typeof value === "object") {
    const output = {};
    for (const key of Object.keys(value).sort()) {
      output[key] = serializeFirestore(value[key]);
    }
    return output;
  }
  return value;
}

function deserializeFirestore(value) {
  if (Array.isArray(value)) return value.map(deserializeFirestore);
  if (value && typeof value === "object") {
    if (value.__type === "timestamp") {
      if (Number.isInteger(value.seconds) && Number.isInteger(value.nanoseconds)) {
        return new Timestamp(value.seconds, value.nanoseconds);
      }
      return Timestamp.fromDate(new Date(value.iso));
    }
    if (value.__type === "date") return new Date(value.iso);
    if (value.__type === "geopoint") {
      return new GeoPoint(value.latitude, value.longitude);
    }
    if (value.__type === "bytes") return Buffer.from(value.base64, "base64");
    if (value.__type === "reference") {
      throw new Error(`Plan contains unsupported reference value: ${value.path}`);
    }
    const output = {};
    for (const [key, nested] of Object.entries(value)) {
      output[key] = deserializeFirestore(nested);
    }
    return output;
  }
  return value;
}

function timestampsEqual(left, right) {
  return Boolean(left)
    && Boolean(right)
    && left.seconds === right.seconds
    && left.nanoseconds === right.nanoseconds;
}

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function hashFirestore(value) {
  return sha256(stableStringify(serializeFirestore(value)));
}

function uniqueNonBlank(values, normalizer = normalizeText) {
  const normalized = [];
  for (const value of values) {
    const item = normalizer(value);
    if (item === null || item === "") continue;
    if (!normalized.includes(item)) normalized.push(item);
  }
  return normalized;
}

function addConflict(conflicts, code, message, paths = [], existingValues = {}, incomingValues = {}) {
  conflicts.push({ code, message, paths, existingValues, incomingValues });
}

function readNestedId(container, key) {
  if (container === null || container === undefined) return { value: "", invalid: false };
  if (typeof container === "string" || typeof container === "number") {
    const text = normalizeText(container);
    return { value: text ?? "", invalid: text === null };
  }
  if (!isPlainObject(container)) return { value: "", invalid: true };
  const text = normalizeText(container[key]);
  return { value: text ?? "", invalid: text === null };
}

function buildCanonicalCandidate(snapshot) {
  const data = snapshot.data() || {};
  const docId = snapshot.id;
  const conflicts = [];
  const warnings = [];

  const canonicalDocId = normalizeMeterNo(docId);
  if (!canonicalDocId || canonicalDocId !== docId) {
    addConflict(
      conflicts,
      CONFLICT_CODES.DOCUMENT_ID_NONCANONICAL,
      "Document ID is not already the canonical normalized meter number. The migration preserves IDs and cannot rename it.",
      ["__name__"],
      { documentId: docId },
      { canonicalDocumentId: canonicalDocId },
    );
  }

  let rawCandidate = docId;
  let normalizedCandidate = docId;
  if (typeof data.meterNo === "string" || typeof data.meterNo === "number") {
    rawCandidate = normalizeText(data.meterNo) ?? "";
    normalizedCandidate = normalizeMeterNo(rawCandidate);
  } else if (isPlainObject(data.meterNo)) {
    const raw = normalizeText(data.meterNo.raw);
    const normalized = normalizeText(data.meterNo.normalized);
    if (raw === null || normalized === null) {
      addConflict(
        conflicts,
        CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID,
        "meterNo.raw and meterNo.normalized must be strings.",
        ["meterNo.raw", "meterNo.normalized"],
        { meterNo: serializeFirestore(data.meterNo) },
      );
    } else {
      rawCandidate = raw || normalized || docId;
      normalizedCandidate = normalizeMeterNo(normalized || rawCandidate || docId);
    }
  } else if (data.meterNo !== undefined && data.meterNo !== null) {
    addConflict(
      conflicts,
      CONFLICT_CODES.DOCUMENT_SHAPE_UNSAFE,
      "meterNo must be a canonical map or a migratable legacy string.",
      ["meterNo"],
      { meterNo: serializeFirestore(data.meterNo) },
    );
  }

  const normalizedEvidence = uniqueNonBlank([
    docId,
    normalizedCandidate,
    normalizeMeterNo(rawCandidate),
    isPlainObject(data.meterNo) ? normalizeMeterNo(data.meterNo.normalized) : "",
  ], normalizeMeterNo);
  if (normalizedEvidence.some((value) => value !== docId)) {
    addConflict(
      conflicts,
      CONFLICT_CODES.NORMALIZED_IDENTITY_CONFLICT,
      "Existing meter-number evidence does not reconcile to the preserved document ID.",
      ["__name__", "meterNo.raw", "meterNo.normalized"],
      { evidence: normalizedEvidence },
      { requiredDocumentId: docId },
    );
  }

  const lmCandidates = uniqueNonBlank([
    data.lmPcode,
    data.parents?.lmPcode,
    data.accessData?.parents?.lmPcode,
    /^ZA\d+$/i.test(String(data.status?.detail || "")) ? data.status.detail : "",
    /^ZA\d+$/i.test(String(data.status?.id || "")) ? data.status.id : "",
  ], normalizeUpper);
  if (lmCandidates.length > 1) {
    addConflict(
      conflicts,
      CONFLICT_CODES.LM_CONFLICT,
      "Multiple legacy LM values disagree.",
      ["lmPcode", "parents.lmPcode", "status"],
      { candidates: lmCandidates },
    );
  }
  const lmPcode = lmCandidates[0] || "";
  if (!lmPcode) {
    addConflict(
      conflicts,
      CONFLICT_CODES.CANONICAL_FIELD_MISSING,
      "No reliable lmPcode could be derived.",
      ["lmPcode"],
    );
  }

  const meterTypeCandidates = uniqueNonBlank([
    data.meterType,
    data.utilityType,
    data.type,
  ], normalizeLower).filter((value) => ["electricity", "water"].includes(value));
  if (meterTypeCandidates.length > 1) {
    addConflict(
      conflicts,
      CONFLICT_CODES.METER_TYPE_CONFLICT,
      "Multiple legacy meterType values disagree.",
      ["meterType"],
      { candidates: meterTypeCandidates },
    );
  }
  const meterType = meterTypeCandidates[0] || "";
  if (!meterType) {
    addConflict(
      conflicts,
      CONFLICT_CODES.CANONICAL_FIELD_MISSING,
      "No reliable electricity/water meterType could be derived.",
      ["meterType"],
    );
  }

  const customerNo = normalizeText(data.customerNo);
  const accountNo = normalizeText(data.accountNo);
  if (customerNo === null) {
    addConflict(
      conflicts,
      CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID,
      "customerNo cannot be converted safely to a canonical string.",
      ["customerNo"],
      { customerNo: serializeFirestore(data.customerNo) },
    );
  }
  if (accountNo === null) {
    addConflict(
      conflicts,
      CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID,
      "accountNo cannot be converted safely to a canonical string.",
      ["accountNo"],
      { accountNo: serializeFirestore(data.accountNo) },
    );
  }

  if (data.refs !== undefined && data.refs !== null && !isPlainObject(data.refs)) {
    addConflict(
      conflicts,
      CONFLICT_CODES.DOCUMENT_SHAPE_UNSAFE,
      "refs must be a map.",
      ["refs"],
      { refs: serializeFirestore(data.refs) },
    );
  }

  const astResult = readNestedId(data.refs?.asts, "id");
  const legacyAst = normalizeText(data.astId);
  if (astResult.invalid || legacyAst === null) {
    addConflict(
      conflicts,
      CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID,
      "AST reference cannot be converted safely to a string.",
      ["refs.asts.id"],
    );
  }
  const astCandidates = uniqueNonBlank([astResult.value, legacyAst]);
  if (astCandidates.length > 1) {
    addConflict(
      conflicts,
      "MM_AST_REFERENCE_CONFLICT",
      "Multiple legacy AST references disagree.",
      ["refs.asts.id", "astId"],
      { candidates: astCandidates },
    );
  }
  const astId = astCandidates[0] || "";

  const salesResult = readNestedId(data.refs?.sales, "id");
  const providerResult = readNestedId(data.refs?.sales, "provider");
  const legacySalesId = normalizeText(data.salesId);
  const legacySalesProvider = normalizeLower(data.salesProvider);
  if (salesResult.invalid || providerResult.invalid || legacySalesId === null || legacySalesProvider === null) {
    addConflict(
      conflicts,
      CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID,
      "Sales reference cannot be converted safely to canonical strings.",
      ["refs.sales.id", "refs.sales.provider"],
    );
  }

  const salesCandidates = uniqueNonBlank([salesResult.value, legacySalesId], normalizeMeterNo);
  if (salesCandidates.length > 1 || salesCandidates.some((value) => value !== docId)) {
    addConflict(
      conflicts,
      CONFLICT_CODES.SALES_REFERENCE_CONFLICT,
      "Sales reference must be blank or equal to the preserved canonical document ID.",
      ["refs.sales.id"],
      { candidates: salesCandidates },
      { requiredValue: docId },
    );
  }
  const salesId = salesCandidates[0] || "";

  const providerCandidates = uniqueNonBlank([
    providerResult.value,
    legacySalesProvider,
  ], normalizeLower);
  if (providerCandidates.length > 1 || providerCandidates.some((value) => value !== "conlog")) {
    addConflict(
      conflicts,
      CONFLICT_CODES.SALES_PROVIDER_CONFLICT,
      "Existing nonblank sales provider must reconcile to conlog.",
      ["refs.sales.provider"],
      { candidates: providerCandidates },
      { governedProvider: "conlog" },
    );
  }
  let salesProvider = providerCandidates[0] || "";
  if (salesId && !salesProvider) {
    salesProvider = "conlog";
    warnings.push("Filled missing Conlog provider because a canonical sales ID exists.");
  }
  if (!salesId && salesProvider) {
    addConflict(
      conflicts,
      CONFLICT_CODES.SALES_PROVIDER_CONFLICT,
      "A populated provider without a sales ID cannot be cleared silently.",
      ["refs.sales.id", "refs.sales.provider"],
      { salesId, salesProvider },
    );
  }

  const metadata = isPlainObject(data.metadata) ? data.metadata : {};
  if (data.metadata !== undefined && data.metadata !== null && !isPlainObject(data.metadata)) {
    addConflict(
      conflicts,
      CONFLICT_CODES.DOCUMENT_SHAPE_UNSAFE,
      "metadata must be a map.",
      ["metadata"],
      { metadata: serializeFirestore(data.metadata) },
    );
  }

  const createdAt = toTimestampOrNull(metadata.createdAt) || snapshot.createTime || null;
  const preservedUpdatedAt = toTimestampOrNull(metadata.updatedAt) || snapshot.updateTime || createdAt;
  if (!createdAt || !preservedUpdatedAt) {
    addConflict(
      conflicts,
      CONFLICT_CODES.CREATED_METADATA_INVALID,
      "Creation/update timestamps could not be reconstructed.",
      ["metadata.createdAt", "metadata.updatedAt"],
    );
  }

  const createdByUid = normalizeText(metadata.createdByUid);
  const createdByUser = normalizeText(metadata.createdByUser);
  const updatedByUid = normalizeText(metadata.updatedByUid);
  const updatedByUser = normalizeText(metadata.updatedByUser);
  if ([createdByUid, createdByUser, updatedByUid, updatedByUser].some((value) => value === null)) {
    addConflict(
      conflicts,
      CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID,
      "Metadata actor fields cannot be converted safely to strings.",
      [
        "metadata.createdByUid",
        "metadata.createdByUser",
        "metadata.updatedByUid",
        "metadata.updatedByUser",
      ],
    );
  }

  const canonicalBase = {
    lmPcode,
    meterNo: {
      raw: rawCandidate || docId,
      normalized: docId,
    },
    meterType,
    customerNo: customerNo ?? "",
    accountNo: accountNo ?? "",
    refs: {
      asts: { id: astId },
      sales: { id: salesId, provider: salesProvider },
    },
    metadata: {
      createdAt,
      createdByUid: createdByUid || MIGRATION_UID,
      createdByUser: createdByUser || MIGRATION_USER,
      updatedAt: preservedUpdatedAt,
      updatedByUid: updatedByUid || createdByUid || MIGRATION_UID,
      updatedByUser: updatedByUser || createdByUser || MIGRATION_USER,
    },
  };

  if (!metadata.createdAt || !createdByUid || !createdByUser) {
    warnings.push("Reconstructed missing creation metadata from Firestore document times and migration actor fallbacks.");
  }

  const validationErrors = validateCanonicalDocument(docId, canonicalBase);
  for (const error of validationErrors) {
    addConflict(conflicts, error.code, error.message, error.paths);
  }

  const existingHash = hashFirestore(data);
  const canonicalBaseHash = hashFirestore(canonicalBase);
  const classification = conflicts.length > 0
    ? "CONFLICT"
    : existingHash === canonicalBaseHash
      ? "UNCHANGED"
      : "UPDATE";

  const removedRootFields = Object.keys(data)
    .filter((field) => !CANONICAL_ROOT_FIELDS.includes(field))
    .sort();

  return {
    id: docId,
    classification,
    sourceUpdateTime: serializeFirestore(snapshot.updateTime),
    sourceCreateTime: serializeFirestore(snapshot.createTime),
    sourceHash: existingHash,
    canonicalBase: serializeFirestore(canonicalBase),
    canonicalBaseHash,
    removedRootFields,
    conflicts,
    warnings,
  };
}

function validateCanonicalDocument(docId, data) {
  const errors = [];
  const rootKeys = Object.keys(data || {}).sort();
  const expectedKeys = [...CANONICAL_ROOT_FIELDS].sort();
  if (stableStringify(rootKeys) !== stableStringify(expectedKeys)) {
    errors.push({
      code: CONFLICT_CODES.DOCUMENT_SHAPE_UNSAFE,
      message: "Canonical root fields do not match the locked seven-field shape.",
      paths: rootKeys,
    });
  }

  if (normalizeMeterNo(docId) !== docId) {
    errors.push({
      code: CONFLICT_CODES.DOCUMENT_ID_NONCANONICAL,
      message: "Document ID is noncanonical.",
      paths: ["__name__"],
    });
  }

  if (typeof data?.lmPcode !== "string" || !data.lmPcode || data.lmPcode !== data.lmPcode.toUpperCase()) {
    errors.push({ code: CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID, message: "lmPcode must be a nonblank uppercase string.", paths: ["lmPcode"] });
  }
  if (!isPlainObject(data?.meterNo)
      || typeof data.meterNo.raw !== "string"
      || typeof data.meterNo.normalized !== "string"
      || data.meterNo.normalized !== docId
      || normalizeMeterNo(data.meterNo.raw) !== docId) {
    errors.push({ code: CONFLICT_CODES.NORMALIZED_IDENTITY_CONFLICT, message: "meterNo identity does not match the document ID.", paths: ["meterNo.raw", "meterNo.normalized"] });
  }
  if (!["electricity", "water"].includes(data?.meterType)) {
    errors.push({ code: CONFLICT_CODES.METER_TYPE_CONFLICT, message: "meterType must be electricity or water.", paths: ["meterType"] });
  }
  for (const field of ["customerNo", "accountNo"]) {
    if (typeof data?.[field] !== "string") {
      errors.push({ code: CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID, message: `${field} must be a string.`, paths: [field] });
    }
  }
  if (!isPlainObject(data?.refs)
      || !isPlainObject(data.refs.asts)
      || !isPlainObject(data.refs.sales)
      || typeof data.refs.asts.id !== "string"
      || typeof data.refs.sales.id !== "string"
      || typeof data.refs.sales.provider !== "string") {
    errors.push({ code: CONFLICT_CODES.DOCUMENT_SHAPE_UNSAFE, message: "refs must contain canonical string reference fields.", paths: ["refs"] });
  } else {
    if (data.refs.sales.id && data.refs.sales.id !== docId) {
      errors.push({ code: CONFLICT_CODES.SALES_REFERENCE_CONFLICT, message: "Sales ID must equal the canonical document ID.", paths: ["refs.sales.id"] });
    }
    if (data.refs.sales.id && data.refs.sales.provider !== "conlog") {
      errors.push({ code: CONFLICT_CODES.SALES_PROVIDER_CONFLICT, message: "A populated sales ID requires provider conlog.", paths: ["refs.sales.provider"] });
    }
    if (!data.refs.sales.id && data.refs.sales.provider) {
      errors.push({ code: CONFLICT_CODES.SALES_PROVIDER_CONFLICT, message: "A blank sales ID requires a blank provider.", paths: ["refs.sales.provider"] });
    }
  }

  if (!isPlainObject(data?.metadata)
      || !isTimestamp(data.metadata.createdAt)
      || !isTimestamp(data.metadata.updatedAt)) {
    errors.push({ code: CONFLICT_CODES.CREATED_METADATA_INVALID, message: "metadata timestamps must be Firestore Timestamp values.", paths: ["metadata.createdAt", "metadata.updatedAt"] });
  } else {
    for (const field of ["createdByUid", "createdByUser", "updatedByUid", "updatedByUser"]) {
      if (typeof data.metadata[field] !== "string") {
        errors.push({ code: CONFLICT_CODES.GOVERNED_FIELD_TYPE_INVALID, message: `metadata.${field} must be a string.`, paths: [`metadata.${field}`] });
      }
    }
  }

  return errors;
}

function buildExecutionCanonical(planRecord, executionTimestamp) {
  const canonical = deserializeFirestore(planRecord.canonicalBase);
  canonical.metadata.updatedAt = executionTimestamp;
  canonical.metadata.updatedByUid = MIGRATION_UID;
  canonical.metadata.updatedByUser = MIGRATION_USER;
  return canonical;
}

function buildUpdateRequest(existingData, canonical, sourceUpdateTime) {
  const updateData = {};
  for (const field of CANONICAL_ROOT_FIELDS) {
    updateData[field] = canonical[field];
  }
  for (const field of Object.keys(existingData)) {
    if (!CANONICAL_ROOT_FIELDS.includes(field)) {
      updateData[field] = FieldValue.delete();
    }
  }
  return {
    updateData,
    precondition: { lastUpdateTime: sourceUpdateTime },
  };
}

async function flushBulkWriter(writer, writePromises) {
  // BulkWriter dispatches and drains queued writes when close() is called.
  // Do not await unresolved individual write promises before closing the writer:
  // Node can otherwise exit with an unresolved top-level promise and no active handles.
  await writer.close();
  return Promise.all(writePromises);
}

function validateProjectArgs(args) {
  if (args.projectId !== TARGET_PROJECT_ID) {
    throw new Error(`This migration is hard locked to ${TARGET_PROJECT_ID}; received ${args.projectId}.`);
  }
  if (args.execute) {
    if (!args.planPath) throw new Error("--plan is required with --execute.");
    if (args.confirm !== CONFIRM_TEXT) {
      throw new Error(`--confirm must equal ${CONFIRM_TEXT}.`);
    }
  } else if (args.planPath || args.confirm) {
    throw new Error("--plan and --confirm are execution-only options.");
  }
}

function initializeAdmin(args) {
  if (getApps().length) {
    const existingApp = getApp();
    const existingProjectId = String(existingApp.options.projectId || "");
    if (existingProjectId && existingProjectId !== args.projectId) {
      throw new Error(
        `Existing Firebase app project ${existingProjectId} does not match ${args.projectId}.`,
      );
    }
    return getFirestore(existingApp);
  }

  let credential;
  let credentialProjectId = "";
  if (args.useAdc) {
    credential = applicationDefault();
  } else {
    if (!args.serviceAccountPath || !fs.existsSync(args.serviceAccountPath)) {
      throw new Error(`Service account file not found: ${args.serviceAccountPath}`);
    }
    const serviceAccount = JSON.parse(fs.readFileSync(args.serviceAccountPath, "utf8"));
    credentialProjectId = String(serviceAccount.project_id || "");
    if (credentialProjectId !== args.projectId) {
      throw new Error(
        `Service account project_id ${credentialProjectId || "<missing>"} does not match ${args.projectId}.`,
      );
    }
    credential = cert(serviceAccount);
  }

  const app = initializeApp({ credential, projectId: args.projectId });
  const actualProjectId = String(app.options.projectId || credentialProjectId || "");
  if (actualProjectId && actualProjectId !== args.projectId) {
    throw new Error(
      `Initialized Firebase project ${actualProjectId} does not match ${args.projectId}.`,
    );
  }
  return getFirestore(app);
}

async function readSelectedDocuments(db, onlyIds) {
  if (onlyIds.length > 0) {
    const uniqueIds = [...new Set(onlyIds)];
    const refs = uniqueIds.map((id) => db.collection(COLLECTION_NAME).doc(id));
    const snapshots = [];
    for (let i = 0; i < refs.length; i += 250) {
      snapshots.push(...await db.getAll(...refs.slice(i, i + 250)));
    }
    return snapshots.filter((snapshot) => snapshot.exists).sort((a, b) => a.id.localeCompare(b.id));
  }

  const snapshot = await db.collection(COLLECTION_NAME)
    .orderBy(FieldPath.documentId())
    .get();
  return snapshot.docs;
}

function makeRawExport(snapshots, projectId) {
  return {
    schemaVersion: 1,
    projectId,
    collection: COLLECTION_NAME,
    exportedAt: new Date().toISOString(),
    count: snapshots.length,
    documents: snapshots.map((snapshot) => ({
      id: snapshot.id,
      path: snapshot.ref.path,
      createTime: serializeFirestore(snapshot.createTime),
      updateTime: serializeFirestore(snapshot.updateTime),
      readTime: serializeFirestore(snapshot.readTime),
      data: serializeFirestore(snapshot.data()),
    })),
  };
}

function makePlan(snapshots, args, rawExportHash) {
  const records = snapshots.map(buildCanonicalCandidate);
  const counts = records.reduce((acc, record) => {
    acc[record.classification] = (acc[record.classification] || 0) + 1;
    return acc;
  }, { UPDATE: 0, UNCHANGED: 0, CONFLICT: 0 });

  const planCore = {
    schemaVersion: 1,
    script: SCRIPT_NAME,
    scriptVersion: SCRIPT_VERSION,
    projectId: args.projectId,
    collection: COLLECTION_NAME,
    onlyIds: [...new Set(args.onlyIds)].sort(),
    rawExportHash,
    counts,
    records,
  };
  return {
    ...planCore,
    generatedAt: new Date().toISOString(),
    planFingerprint: sha256(stableStringify(planCore)),
  };
}

function validatePlan(plan, args) {
  if (plan.script !== SCRIPT_NAME || plan.scriptVersion !== SCRIPT_VERSION) {
    throw new Error("Plan script identity/version does not match this migration script.");
  }
  if (plan.projectId !== args.projectId || plan.projectId !== TARGET_PROJECT_ID) {
    throw new Error("Plan project does not match the hard-locked target project.");
  }
  if (plan.collection !== COLLECTION_NAME) throw new Error("Plan collection mismatch.");
  const { generatedAt, planFingerprint, ...planCore } = plan;
  const expected = sha256(stableStringify(planCore));
  if (expected !== planFingerprint) throw new Error("Plan fingerprint validation failed.");
  return true;
}

function writeJson(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const tempPath = `${filePath}.tmp`;
  fs.writeFileSync(tempPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  fs.renameSync(tempPath, filePath);
}

async function createDryRun(db, args) {
  const runDir = path.join(args.reportDir, `meter_master_canonical_plan_${timestampToken()}`);
  ensureDirectory(runDir);

  console.log(`Mode: DRY RUN`);
  console.log(`Project: ${args.projectId}`);
  console.log(`Collection: ${COLLECTION_NAME}`);
  console.log(`Output: ${runDir}`);
  if (args.onlyIds.length) console.log(`Only IDs: ${args.onlyIds.join(", ")}`);

  const snapshots = await readSelectedDocuments(db, args.onlyIds);
  const rawExport = makeRawExport(snapshots, args.projectId);
  const rawExportPath = path.join(runDir, "raw_export.json");
  writeJson(rawExportPath, rawExport);
  const rawExportHash = sha256(fs.readFileSync(rawExportPath));

  const plan = makePlan(snapshots, args, rawExportHash);
  const planPath = path.join(runDir, "migration_plan.json");
  writeJson(planPath, plan);

  const conflicts = plan.records
    .filter((record) => record.classification === "CONFLICT")
    .map((record) => ({ id: record.id, conflicts: record.conflicts, warnings: record.warnings }));
  writeJson(path.join(runDir, "conflicts.json"), conflicts);

  const report = {
    schemaVersion: 1,
    script: SCRIPT_NAME,
    scriptVersion: SCRIPT_VERSION,
    mode: "DRY_RUN",
    projectId: args.projectId,
    collection: COLLECTION_NAME,
    result: conflicts.length ? "PLANNED_WITH_CONFLICTS" : "PLANNED",
    counts: plan.counts,
    rawExportPath,
    rawExportHash,
    planPath,
    planFingerprint: plan.planFingerprint,
    conflictReportPath: path.join(runDir, "conflicts.json"),
    firestoreWrites: 0,
    generatedAt: new Date().toISOString(),
  };
  writeJson(path.join(runDir, "run_report.json"), report);

  console.log(`Documents read/exported: ${snapshots.length}`);
  console.log(`UPDATE: ${plan.counts.UPDATE}`);
  console.log(`UNCHANGED: ${plan.counts.UNCHANGED}`);
  console.log(`CONFLICT: ${plan.counts.CONFLICT}`);
  console.log(`Firestore writes: 0`);
  console.log(`Plan: ${planPath}`);
  return report;
}

async function executePlan(db, args) {
  const absolutePlanPath = path.resolve(args.planPath);
  if (!fs.existsSync(absolutePlanPath)) throw new Error(`Plan not found: ${absolutePlanPath}`);
  const plan = JSON.parse(fs.readFileSync(absolutePlanPath, "utf8"));
  validatePlan(plan, args);

  const runDir = path.join(args.reportDir, `meter_master_canonical_execute_${timestampToken()}`);
  ensureDirectory(runDir);
  const executionTimestamp = Timestamp.now();

  console.log(`Mode: EXECUTE`);
  console.log(`Project: ${args.projectId}`);
  console.log(`Collection: ${COLLECTION_NAME}`);
  console.log(`Plan: ${absolutePlanPath}`);
  console.log(`Output: ${runDir}`);

  const conflictRecords = plan.records.filter((record) => record.classification === "CONFLICT");
  const unchangedRecords = plan.records.filter((record) => record.classification === "UNCHANGED");
  const updateRecords = plan.records.filter((record) => record.classification === "UPDATE");

  const writer = db.bulkWriter();
  writer.onWriteError((error) => {
    const retryableCodes = new Set([4, 8, 10, 13, 14]);
    return retryableCodes.has(error.code) && error.failedAttempts < 3;
  });

  const writePromises = [];
  for (const record of updateRecords) {
    const ref = db.collection(COLLECTION_NAME).doc(record.id);
    const sourceUpdateTime = deserializeFirestore(record.sourceUpdateTime);
    const currentSnapshot = await ref.get();
    if (!currentSnapshot.exists) {
      conflictRecords.push({
        ...record,
        classification: "CONFLICT",
        conflicts: [{
          code: CONFLICT_CODES.TRANSACTION_PRECONDITION_CHANGED,
          message: "Document no longer exists at execution time.",
          paths: ["__name__"],
        }],
      });
      continue;
    }
    const currentHash = hashFirestore(currentSnapshot.data());
    if (currentHash !== record.sourceHash || !timestampsEqual(currentSnapshot.updateTime, sourceUpdateTime)) {
      conflictRecords.push({
        ...record,
        classification: "CONFLICT",
        conflicts: [{
          code: CONFLICT_CODES.TRANSACTION_PRECONDITION_CHANGED,
          message: "Document changed after the dry-run plan was created.",
          paths: ["__updateTime__"],
          existingValues: {
            plannedHash: record.sourceHash,
            currentHash,
            plannedUpdateTime: serializeFirestore(sourceUpdateTime),
            currentUpdateTime: serializeFirestore(currentSnapshot.updateTime),
          },
        }],
      });
      continue;
    }

    const canonical = buildExecutionCanonical(record, executionTimestamp);
    const validationErrors = validateCanonicalDocument(record.id, canonical);
    if (validationErrors.length) {
      conflictRecords.push({
        ...record,
        classification: "CONFLICT",
        conflicts: validationErrors,
      });
      continue;
    }

    const { updateData, precondition } = buildUpdateRequest(
      currentSnapshot.data(),
      canonical,
      sourceUpdateTime,
    );
    const promise = writer.update(ref, updateData, precondition)
      .then((writeResult) => ({
        id: record.id,
        status: "UPDATED",
        writeTime: serializeFirestore(writeResult.writeTime),
        expectedHash: hashFirestore(canonical),
      }))
      .catch((error) => ({
        id: record.id,
        status: "FAILED",
        conflictCode: error.code === 9
          ? CONFLICT_CODES.TRANSACTION_PRECONDITION_CHANGED
          : CONFLICT_CODES.RECORD_WRITE_FAILED,
        error: String(error?.message || error),
      }));
    writePromises.push(promise);
  }

  console.log(`Queued writes: ${writePromises.length}`);
  const writeResults = await flushBulkWriter(writer, writePromises);
  console.log(`BulkWriter completed: ${writeResults.length}`);

  const updatedResults = writeResults.filter((result) => result.status === "UPDATED");
  const failedResults = writeResults.filter((result) => result.status === "FAILED");

  const verification = [];
  for (let i = 0; i < updatedResults.length; i += 250) {
    const chunk = updatedResults.slice(i, i + 250);
    const snapshots = await db.getAll(...chunk.map((result) => db.collection(COLLECTION_NAME).doc(result.id)));
    for (const snapshot of snapshots) {
      const result = updatedResults.find((item) => item.id === snapshot.id);
      const errors = snapshot.exists ? validateCanonicalDocument(snapshot.id, snapshot.data()) : [{
        code: CONFLICT_CODES.RECORD_WRITE_FAILED,
        message: "Document missing during verification.",
        paths: ["__name__"],
      }];
      const actualHash = snapshot.exists ? hashFirestore(snapshot.data()) : "";
      verification.push({
        id: snapshot.id,
        exists: snapshot.exists,
        expectedHash: result?.expectedHash || "",
        actualHash,
        hashMatch: Boolean(result) && result.expectedHash === actualHash,
        validationErrors: errors,
        passed: snapshot.exists && errors.length === 0 && result?.expectedHash === actualHash,
      });
    }
  }

  const verificationFailures = verification.filter((item) => !item.passed);
  const result = verificationFailures.length || failedResults.length
    ? "FAILED"
    : conflictRecords.length
      ? "COMPLETED_WITH_CONFLICTS"
      : "COMPLETED";

  const conflictOutput = [
    ...conflictRecords.map((record) => ({
      id: record.id,
      sourceHash: record.sourceHash,
      conflicts: record.conflicts,
      warnings: record.warnings || [],
      writeAttempted: false,
    })),
    ...failedResults.map((record) => ({
      id: record.id,
      conflicts: [{
        code: record.conflictCode,
        message: record.error,
        paths: [],
      }],
      writeAttempted: true,
    })),
  ];

  writeJson(path.join(runDir, "conflicts.json"), conflictOutput);
  writeJson(path.join(runDir, "write_results.json"), writeResults);
  writeJson(path.join(runDir, "verification.json"), verification);

  const report = {
    schemaVersion: 1,
    script: SCRIPT_NAME,
    scriptVersion: SCRIPT_VERSION,
    mode: "EXECUTE",
    projectId: args.projectId,
    collection: COLLECTION_NAME,
    planPath: absolutePlanPath,
    planFingerprint: plan.planFingerprint,
    startedAt: executionTimestamp.toDate().toISOString(),
    finishedAt: new Date().toISOString(),
    result,
    counts: {
      rowsRead: plan.records.length,
      plannedUpdates: updateRecords.length,
      updated: updatedResults.length,
      unchanged: unchangedRecords.length,
      conflicts: conflictOutput.length,
      failed: failedResults.length,
      verificationFailed: verificationFailures.length,
    },
    accountingValid:
      updatedResults.length
      + unchangedRecords.length
      + conflictOutput.length
      === plan.records.length,
    conflictReportPath: path.join(runDir, "conflicts.json"),
    writeResultsPath: path.join(runDir, "write_results.json"),
    verificationPath: path.join(runDir, "verification.json"),
  };
  writeJson(path.join(runDir, "run_report.json"), report);

  console.log(`UPDATED: ${report.counts.updated}`);
  console.log(`UNCHANGED: ${report.counts.unchanged}`);
  console.log(`CONFLICTS: ${report.counts.conflicts}`);
  console.log(`FAILED WRITES: ${report.counts.failed}`);
  console.log(`VERIFICATION FAILED: ${report.counts.verificationFailed}`);
  console.log(`RESULT: ${result}`);
  console.log(`Report: ${path.join(runDir, "run_report.json")}`);

  if (result === "FAILED") process.exitCode = 1;
  return report;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  validateProjectArgs(args);
  const db = initializeAdmin(args);
  if (args.execute) await executePlan(db, args);
  else await createDryRun(db, args);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`[FAILED] ${error?.stack || error}`);
    process.exitCode = 1;
  });
}

module.exports = {
  CANONICAL_ROOT_FIELDS,
  CONFLICT_CODES,
  buildCanonicalCandidate,
  buildExecutionCanonical,
  buildUpdateRequest,
  deserializeFirestore,
  flushBulkWriter,
  hashFirestore,
  normalizeMeterNo,
  serializeFirestore,
  stableStringify,
  timestampsEqual,
  toTimestampOrNull,
  validateCanonicalDocument,
};
