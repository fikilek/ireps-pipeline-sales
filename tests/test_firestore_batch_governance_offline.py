from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class FirestoreBatchGovernanceTests(unittest.TestCase):
    def test_10216_partitions_into_25x400_plus_216(self) -> None:
        sizes = []
        remaining = 10216
        while remaining:
            wave = min(400, remaining)
            sizes.append(wave)
            remaining -= wave
        self.assertEqual(sizes, [400] * 25 + [216])
        self.assertEqual(len(sizes), 26)

    def test_rules_lock_400_and_cross_collection_transaction_exception(self) -> None:
        rules = source("rules/SALES_PIPELINE_RULES.md")
        self.assertIn("**Version:** 1.10.0", rules)
        self.assertIn("FIRESTORE_BATCH_SIZE = 400", rules)
        self.assertIn("maximum logical meters per transaction = 200", rules)
        self.assertIn("no more than 400 document references", rules)
        self.assertIn("no more than 400 write operations", rules)

    def test_sales_all_refresh_has_explicit_writebatch_and_no_transaction_loop(self) -> None:
        text = source("scripts/sales_pipeline_sales_all_refresh.py")
        self.assertIn("FIRESTORE_BATCH_SIZE = 400", text)
        self.assertIn("batch = db.batch()", text)
        self.assertIn("batch.create(", text)
        self.assertIn("batch.update(", text)
        self.assertIn("LastUpdateOption", text)
        self.assertNotIn("db.transaction(", text)
        self.assertNotIn("firestore.transactional", text)
        self.assertIn('"perDocumentFallback": False', text)

    def test_meter_master_refresh_has_explicit_writebatch_and_no_transaction_loop(self) -> None:
        text = source("scripts/07_upload_meter_master_v3.py")
        self.assertIn("FIRESTORE_BATCH_SIZE = BATCH_SIZE", text)
        self.assertIn("batch = db.batch()", text)
        self.assertIn("option=LastUpdateOption(update_time)", text)
        self.assertNotIn("db.transaction()", text)
        self.assertNotIn("@firestore.transactional", text)
        self.assertIn("preflight_refresh_documents", text)

    def test_visibility_reconciliation_uses_200_meter_batched_transaction_exception(self) -> None:
        text = source("scripts/sales_pipeline_visibility_reconciliation_dev.py")
        self.assertIn("FIRESTORE_READ_LIMIT = 400", text)
        self.assertIn("LOGICAL_METERS_PER_TRANSACTION = 200", text)
        self.assertIn("db.transaction(max_attempts=TRANSACTION_MAX_ATTEMPTS)", text)
        self.assertIn("transaction.get_all(refs)", text)
        self.assertIn('transaction.update(sales_ref, {"master.visibility": expected})', text)
        self.assertNotRegex(text, r"\.document\([^\n]+\)\.get\(")
        self.assertIn('"perDocumentFallback": False', text)

    def test_psd_deploy_clis_accept_only_governed_400_sizes(self) -> None:
        for relative in (
            "scripts/monthly_only/08_deploy_corrected_psd_dev.py",
            "scripts/monthly_only/08_deploy_corrected_psd_test.py",
        ):
            with self.subTest(relative=relative):
                text = source(relative)
                self.assertIn("DEFAULT_READ_BATCH_SIZE = 400", text)
                self.assertIn("DEFAULT_WRITE_BATCH_SIZE = 400", text)
                self.assertIn("args.read_batch_size != DEFAULT_READ_BATCH_SIZE", text)
                self.assertIn("args.write_batch_size != DEFAULT_WRITE_BATCH_SIZE", text)
                self.assertNotIn("1 <= args.write_batch_size <= 500", text)

    def test_gps_pilot_bulk_reads_preflight_and_verification(self) -> None:
        text = source("scripts/monthly_only/07_upload_exact_gps_pilot_dev.py")
        self.assertGreaterEqual(text.count("db.get_all(refs)"), 2)
        self.assertIn("batch = db.batch()", text)
        self.assertIn("batch.set(", text)

    def test_provider_seed_is_atomic_create_batch(self) -> None:
        text = source("scripts/tools/vending-provider/seed_vending_providers.py")
        self.assertIn("batch = db.batch()", text)
        self.assertIn("batch.create(ref, document)", text)
        self.assertIn("batch.commit()", text)
        self.assertNotRegex(text, r"\bref\.create\(")

    def test_meter_master_migration_uses_writebatch_not_bulkwriter(self) -> None:
        text = source("scripts/tools/meter-master/migrate_meter_master_to_canonical_v1.js")
        self.assertIn("const FIRESTORE_BATCH_SIZE = 400", text)
        self.assertIn("const batch = db.batch()", text)
        self.assertIn("batch.update(ref, operation.updateData, operation.precondition)", text)
        self.assertIn("lastUpdateTime", text)
        self.assertNotIn("bulkWriter(", text)
        self.assertIn("BOUNDED_BATCH_RECOVERY_FAILED", text)
        self.assertIn("perDocumentFallback: false", text)

    def test_metadata_cleanup_bulk_reads_and_one_batch(self) -> None:
        text = source("scripts/tools/sales-all/remove_sales_all_metadata_dev_v1.js")
        self.assertIn("const FIRESTORE_BATCH_SIZE = 400", text)
        self.assertIn("db.getAll(", text)
        self.assertIn("db.batch()", text)
        self.assertIn("lastUpdateTime", text)
        self.assertIn("perDocumentFallback: false", text)
        self.assertNotRegex(text, r"await\s+[^\n]*Ref\.update\(")

    def test_single_doc_visibility_write_uses_one_operation_writebatch(self) -> None:
        text = source("scripts/tools/sales-all/update_sales_all_visibility_dev_v1.js")
        self.assertIn("const batch = db.batch()", text)
        self.assertIn("batch.update(", text)
        self.assertIn("lastUpdateTime", text)
        self.assertNotIn("await targetRef.update(", text)

    def test_stage02_stage04_stage08_keep_400_create_batches_and_bulk_samples(self) -> None:
        checks = (
            ("scripts/02_upload_conlog_atomic_v2.py", "BATCH_SIZE = 400"),
            ("scripts/04_upload_conlog_monthly_v3.py", "BATCH_SIZE = 400"),
            ("scripts/08_upload_sales_all_meters.py", "BATCH_SIZE = 400"),
        )
        for relative, batch_constant in checks:
            with self.subTest(relative=relative):
                text = source(relative)
                self.assertIn(batch_constant, text)
                self.assertIn("batch.create(", text)
                self.assertIn("db.get_all(refs)", text)

    def test_governed_bulk_sources_do_not_reintroduce_known_per_record_patterns(self) -> None:
        governed = {
            "scripts/07_upload_meter_master_v3.py": ("db.transaction()", ".get(transaction="),
            "scripts/sales_pipeline_sales_all_refresh.py": ("db.transaction(", ".get(transaction="),
            "scripts/tools/meter-master/migrate_meter_master_to_canonical_v1.js": ("bulkWriter(",),
        }
        for relative, forbidden in governed.items():
            text = source(relative)
            with self.subTest(relative=relative):
                for pattern in forbidden:
                    self.assertNotIn(pattern, text)


if __name__ == "__main__":
    unittest.main()
