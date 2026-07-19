const test = require("node:test");
const assert = require("node:assert/strict");
const { Timestamp } = require("firebase-admin/firestore");
const {
  CANONICAL_ROOT_FIELDS,
  buildCanonicalCandidate,
  deserializeFirestore,
  flushBulkWriter,
  normalizeMeterNo,
  serializeFirestore,
  timestampsEqual,
  toTimestampOrNull,
  validateCanonicalDocument,
} = require("./migrate_meter_master_to_canonical_v1.js");

function makeSnapshot(id, data, createIso = "2026-03-19T09:27:01.000Z", updateIso = "2026-04-30T02:10:22.000Z") {
  return {
    id,
    data: () => data,
    createTime: Timestamp.fromDate(new Date(createIso)),
    updateTime: Timestamp.fromDate(new Date(updateIso)),
  };
}

test("normalization removes whitespace, uppercases and preserves leading zeroes", () => {
  assert.equal(normalizeMeterNo(" 04 0ab 123 "), "040AB123");
});

test("legacy SALES_ONLY document missing metadata plans a canonical same-ID update", () => {
  const snapshot = makeSnapshot("04085348573", {
    accountNo: "100448184",
    customerNo: "100448184",
    lmPcode: "ZA7423",
    meterNo: { normalized: "04085348573", raw: "04085348573" },
    meterType: "electricity",
    refs: {
      asts: { id: "" },
      sales: { id: "04085348573", provider: "conlog" },
    },
  });

  const record = buildCanonicalCandidate(snapshot);
  assert.equal(record.id, "04085348573");
  assert.equal(record.classification, "UPDATE");
  assert.equal(record.conflicts.length, 0);
  assert.deepEqual(Object.keys(record.canonicalBase).sort(), [...CANONICAL_ROOT_FIELDS].sort());
});

test("legacy flat meterNo is converted without changing the document ID", () => {
  const snapshot = makeSnapshot("W35678", {
    lmPcode: "ZA2157",
    meterNo: "w35678",
    meterType: "water",
    customerNo: null,
    accountNo: null,
    refs: { asts: { id: "AST_1" }, sales: { id: null, provider: null } },
  });
  const record = buildCanonicalCandidate(snapshot);
  assert.equal(record.classification, "UPDATE");
  assert.equal(record.conflicts.length, 0);
  assert.equal(record.canonicalBase.meterNo.normalized, "W35678");
});

test("noncanonical document ID is a conflict because IDs are preserved", () => {
  const snapshot = makeSnapshot(" 04085348573 ", {
    lmPcode: "ZA7423",
    meterNo: { raw: "04085348573", normalized: "04085348573" },
    meterType: "electricity",
    customerNo: "",
    accountNo: "",
    refs: { asts: { id: "" }, sales: { id: "04085348573", provider: "conlog" } },
  });
  const record = buildCanonicalCandidate(snapshot);
  assert.equal(record.classification, "CONFLICT");
  assert.ok(record.conflicts.some((item) => item.code === "MM_DOCUMENT_ID_NONCANONICAL"));
});

test("sales ID different from document ID is a conflict", () => {
  const snapshot = makeSnapshot("04085348573", {
    lmPcode: "ZA7423",
    meterNo: { raw: "04085348573", normalized: "04085348573" },
    meterType: "electricity",
    customerNo: "",
    accountNo: "",
    refs: { asts: { id: "" }, sales: { id: "DIFFERENT", provider: "conlog" } },
  });
  const record = buildCanonicalCandidate(snapshot);
  assert.equal(record.classification, "CONFLICT");
  assert.ok(record.conflicts.some((item) => item.code === "MM_SALES_REFERENCE_CONFLICT"));
});

test("already canonical document validates without schema errors", () => {
  const timestamp = Timestamp.fromDate(new Date("2026-07-19T00:17:19.711Z"));
  const data = {
    lmPcode: "ZA2157",
    meterNo: { raw: "04014321456", normalized: "04014321456" },
    meterType: "electricity",
    customerNo: "",
    accountNo: "",
    refs: { asts: { id: "AST_1" }, sales: { id: "", provider: "" } },
    metadata: {
      createdAt: timestamp,
      createdByUid: "UID",
      createdByUser: "User",
      updatedAt: timestamp,
      updatedByUid: "UID",
      updatedByUser: "User",
    },
  };
  assert.deepEqual(validateCanonicalDocument("04014321456", data), []);
});


test("timestamp serialization preserves Firestore nanoseconds exactly", () => {
  const original = new Timestamp(1777515022, 455759000);
  const serialized = serializeFirestore(original);
  const restored = deserializeFirestore(serialized);

  assert.equal(restored.seconds, 1777515022);
  assert.equal(restored.nanoseconds, 455759000);
  assert.equal(timestampsEqual(original, restored), true);
});

test("timestamp comparison rejects a one-nanosecond change", () => {
  const planned = new Timestamp(1777515022, 455759000);
  const changed = new Timestamp(1777515022, 455759001);

  assert.equal(timestampsEqual(planned, changed), false);
});


test("BulkWriter is closed before waiting for queued write promises", async () => {
  let closeCalled = false;
  let resolveWrite;

  const writePromise = new Promise((resolve) => {
    resolveWrite = resolve;
  });

  const fakeWriter = {
    async close() {
      closeCalled = true;
      resolveWrite({ status: "UPDATED" });
    },
  };

  const results = await flushBulkWriter(fakeWriter, [writePromise]);

  assert.equal(closeCalled, true);
  assert.deepEqual(results, [{ status: "UPDATED" }]);
});


test("toTimestampOrNull preserves Firestore timestamp nanoseconds exactly", () => {
  const original = new Timestamp(1773912421, 362345000);
  const restored = toTimestampOrNull(original);

  assert.equal(restored.seconds, 1773912421);
  assert.equal(restored.nanoseconds, 362345000);
});

test("already canonical document with sub-millisecond metadata is UNCHANGED", () => {
  const createdAt = new Timestamp(1773912421, 362345000);
  const updatedAt = new Timestamp(1784428987, 762000000);

  const data = {
    accountNo: "100448184",
    customerNo: "100448184",
    lmPcode: "ZA7423",
    metadata: {
      createdAt,
      createdByUid: "SYSTEM",
      createdByUser: "METER MASTER CANONICAL MIGRATION",
      updatedAt,
      updatedByUid: "SYSTEM",
      updatedByUser: "METER MASTER CANONICAL MIGRATION",
    },
    meterNo: {
      normalized: "04085348573",
      raw: "04085348573",
    },
    meterType: "electricity",
    refs: {
      asts: { id: "" },
      sales: { id: "04085348573", provider: "conlog" },
    },
  };

  const snapshot = {
    id: "04085348573",
    data: () => data,
    createTime: new Timestamp(1773912421, 362345000),
    updateTime: new Timestamp(1784428990, 649375000),
  };

  const record = buildCanonicalCandidate(snapshot);

  assert.equal(record.classification, "UNCHANGED");
  assert.equal(record.conflicts.length, 0);
  assert.equal(
    record.canonicalBase.metadata.createdAt.nanoseconds,
    362345000,
  );
});
