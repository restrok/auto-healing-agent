"""Unit tests for Evidence Memory Engine and SQLite schema migration.

Tests verify:
- Non-destructive, backward-compatible migration of healing_history.sqlite
- Deterministic 16-char hex container and error signature hashing
- Failed action and rejected hypothesis persistence
- RRSI L0 3-cycle sliding window exclusion
- HITL Telegram rejection synchronization from orchestrator.db
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auto_healing.audit import (
    compute_container_signature,
    compute_error_signature,
    get_db_connection,
    get_discarded_hypotheses,
    is_action_discarded,
    migrate_db,
    record_failed_action,
    record_rejected_hypothesis,
    sync_hitl_rejections,
)


class TestDatabaseMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_healing_history.sqlite"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migrate_db_creates_tables_and_indexes(self):
        """Verifies tables and B-tree indexes are created properly."""
        migrate_db(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("healing_runs", tables)
        self.assertIn("failed_actions", tables)
        self.assertIn("rejected_hypotheses", tables)
        self.assertIn("schema_migrations", tables)

        indexes = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("idx_failed_actions_sig_run", indexes)
        self.assertIn("idx_failed_actions_name_run", indexes)
        self.assertIn("idx_rejected_hypo_sig_run", indexes)
        self.assertIn("idx_rejected_hypo_name_run", indexes)
        self.assertIn("idx_rejected_hypo_plan_id", indexes)

        # Check migration recorded
        mig = cursor.execute("SELECT version, description FROM schema_migrations WHERE version = 1").fetchone()
        self.assertIsNotNone(mig)
        self.assertEqual(mig[0], 1)
        conn.close()

    def test_migrate_db_is_idempotent(self):
        """Verifies running migrate_db repeatedly causes no errors or duplicate schema state."""
        migrate_db(self.db_path)
        # Run second and third times
        migrate_db(self.db_path)
        migrate_db(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        count = cursor.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(count, 1)
        conn.close()

    def test_migrate_db_preserves_existing_data(self):
        """Verifies migration is non-destructive and preserves existing healing_runs."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE healing_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                host_health TEXT NOT NULL,
                containers_running INTEGER NOT NULL,
                containers_total INTEGER NOT NULL,
                remediations_count INTEGER NOT NULL,
                audits_summary TEXT NOT NULL,
                report_html TEXT NOT NULL
            )
            """
        )
        for i in range(1, 10):
            conn.execute(
                """
                INSERT INTO healing_runs (
                    timestamp, host_health, containers_running, containers_total,
                    remediations_count, audits_summary, report_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"2026-09-{i:02d}T10:00:00Z", "OK", 19, 19, 0, "[]", "<p>Report</p>"),
            )
        conn.commit()
        conn.close()

        # Run migration
        migrate_db(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        count = cursor.execute("SELECT count(*) FROM healing_runs").fetchone()[0]
        self.assertEqual(count, 9)
        conn.close()

    def test_historical_backfill(self):
        """Verifies that historical failed actions in healing_runs are backfilled to failed_actions."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE healing_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                host_health TEXT NOT NULL,
                containers_running INTEGER NOT NULL,
                containers_total INTEGER NOT NULL,
                remediations_count INTEGER NOT NULL,
                audits_summary TEXT NOT NULL,
                report_html TEXT NOT NULL
            )
            """
        )
        # Insert a run with a failure resembling production run #2
        audits_json = json.dumps(
            [
                {
                    "verdict": "CRITICAL_REGRESSION",
                    "details": "Container mock-failing-service failed to stay up after restart (Exit: 0).",
                    "improved": False,
                    "post_state": {"running": True, "health": "starting", "restart_count": 0, "exit_code": 0},
                }
            ]
        )
        conn.execute(
            """
            INSERT INTO healing_runs (
                id, timestamp, host_health, containers_running, containers_total,
                remediations_count, audits_summary, report_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (2, "2026-09-18T02:29:42+00:00", "OK", 19, 19, 1, audits_json, "<p>Report</p>"),
        )
        conn.commit()
        conn.close()

        migrate_db(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT run_id, container_name, verdict, action_type, improved FROM failed_actions"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 2)
        self.assertEqual(row[1], "mock-failing-service")
        self.assertEqual(row[2], "CRITICAL_REGRESSION")
        self.assertEqual(row[3], "restart_container")
        self.assertEqual(row[4], 0)
        conn.close()

    def test_wal_pragma_and_busy_timeout(self):
        """Verifies WAL journal mode and busy_timeout settings."""
        migrate_db(self.db_path)
        conn = get_db_connection(self.db_path)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.close()
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, 10000)


class TestSignatureHashing(unittest.TestCase):
    def test_container_signature_length_and_hex(self):
        sig = compute_container_signature("mock-service")
        self.assertEqual(len(sig), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in sig))

    def test_container_signature_normalization(self):
        base_sig = compute_container_signature("mock-service")
        # Leading slash
        self.assertEqual(compute_container_signature("/mock-service"), base_sig)
        # Mixed casing
        self.assertEqual(compute_container_signature("Mock-Service"), base_sig)
        # Leading/trailing whitespace
        self.assertEqual(compute_container_signature("   mock-service   "), base_sig)
        # Combination
        self.assertEqual(compute_container_signature("  /MOCK-SERVICE  "), base_sig)

    def test_container_signature_with_image(self):
        sig_no_img = compute_container_signature("service-a")
        sig_with_img = compute_container_signature("service-a", image="alpine:3.18")
        self.assertEqual(len(sig_with_img), 16)
        self.assertNotEqual(sig_no_img, sig_with_img)
        # Image normalization
        self.assertEqual(
            compute_container_signature("service-a", image="alpine:3.18"),
            compute_container_signature("/service-a", image="  ALPINE:3.18  "),
        )

    def test_error_signature_length_and_hex(self):
        sig = compute_error_signature("Connection refused on port 8080")
        self.assertEqual(len(sig), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in sig))

    def test_error_signature_timestamp_normalization(self):
        err1 = "2026-09-24T03:00:00.123456+00:00 [ERROR] Database connection timed out"
        err2 = "2026-09-24T03:05:00Z [ERROR] Database connection timed out"
        err3 = "12:03:24 [ERROR] Database connection timed out"
        sig1 = compute_error_signature(err1)
        sig2 = compute_error_signature(err2)
        sig3 = compute_error_signature(err3)
        self.assertEqual(sig1, sig2)
        self.assertEqual(sig1, sig3)

    def test_error_signature_ephemeral_id_normalization(self):
        err1 = "Container 7a5a1d3d4e5f failed healthcheck"
        err2 = "Container 9b8c7d6e5f4a failed healthcheck"
        self.assertEqual(compute_error_signature(err1), compute_error_signature(err2))

    def test_error_signature_exit_code(self):
        err = "Container terminated with error"
        sig_none = compute_error_signature(err)
        sig_137 = compute_error_signature(err, exit_code=137)
        sig_1 = compute_error_signature(err, exit_code=1)
        self.assertNotEqual(sig_none, sig_137)
        self.assertNotEqual(sig_137, sig_1)

    def test_error_signature_list_input(self):
        lines = [
            "2026-09-24 03:00:00 [ERROR] Line 1",
            "2026-09-24 03:00:01 [ERROR] Line 2",
        ]
        sig = compute_error_signature(lines)
        self.assertEqual(len(sig), 16)


class TestEvidenceMemoryOperations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_healing_history.sqlite"
        migrate_db(self.db_path)
        conn = get_db_connection(self.db_path)
        conn.execute(
            """
            INSERT INTO healing_runs (id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html)
            VALUES (1, '2026-09-24T00:00:00Z', 'OK', 19, 19, 0, '[]', '')
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_record_and_query_failed_action(self):
        record_failed_action(
            run_id=1,
            container_name="test-service",
            action_type="restart_container",
            verdict="STILL_UNHEALTHY",
            details="Service failed to recover",
            error_msg="Connection refused",
            db_path=self.db_path,
        )

        discarded = get_discarded_hypotheses("test-service", window_cycles=3, db_path=self.db_path)
        self.assertEqual(len(discarded), 1)
        item = discarded[0]
        self.assertEqual(item["item_type"], "failed_action")
        self.assertEqual(item["container_name"], "test-service")
        self.assertEqual(item["action_type"], "restart_container")
        self.assertEqual(item["verdict"], "STILL_UNHEALTHY")

        # Test query by 16-char container signature
        sig = compute_container_signature("test-service")
        by_sig = get_discarded_hypotheses(sig, window_cycles=3, db_path=self.db_path)
        self.assertEqual(len(by_sig), 1)
        self.assertEqual(by_sig[0]["container_name"], "test-service")

        # Test is_action_discarded
        self.assertTrue(is_action_discarded("test-service", "restart_container", db_path=self.db_path))
        self.assertFalse(is_action_discarded("test-service", "prune_docker_dangling_images", db_path=self.db_path))
        self.assertFalse(is_action_discarded("other-service", "restart_container", db_path=self.db_path))

    def test_record_and_query_failed_action_with_null_and_unknown_run_id(self):
        # run_id=None
        record_failed_action(
            run_id=None,
            container_name="null-run-svc",
            action_type="restart_container",
            verdict="FAILED_TO_APPLY",
            details="Immediate failure",
            db_path=self.db_path,
        )
        # Nonexistent run_id (gracefully set to None)
        record_failed_action(
            run_id=999,
            container_name="unknown-run-svc",
            action_type="restart_container",
            verdict="STILL_UNHEALTHY",
            details="Run not created yet",
            db_path=self.db_path,
        )
        self.assertTrue(is_action_discarded("null-run-svc", "restart_container", db_path=self.db_path))
        self.assertTrue(is_action_discarded("unknown-run-svc", "restart_container", db_path=self.db_path))

    def test_record_and_query_rejected_hypothesis(self):
        record_rejected_hypothesis(
            container_name="coach-api",
            hypothesis_title="Restart Coach Service",
            recommended_fix="Execute restart_container",
            worker_task="docker restart coach-api",
            rejection_source="HUMAN_HITL",
            rejection_reason="Operator rejected via Telegram",
            plan_id="heal_12345678",
            db_path=self.db_path,
        )

        discarded = get_discarded_hypotheses("coach-api", window_cycles=3, db_path=self.db_path)
        self.assertEqual(len(discarded), 1)
        item = discarded[0]
        self.assertEqual(item["item_type"], "rejected_hypothesis")
        self.assertEqual(item["container_name"], "coach-api")
        self.assertEqual(item["plan_id"], "heal_12345678")
        self.assertEqual(item["rejection_source"], "HUMAN_HITL")

        self.assertTrue(is_action_discarded("coach-api", "restart_container", db_path=self.db_path))


class TestSlidingWindowExclusion(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_sliding_window.sqlite"
        migrate_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sliding_window_filtering(self):
        """
        RRSI sliding window test:
        An action failing in run 1 must be discarded in runs 1, 2, 3 (window=3).
        Once run 4 is recorded, run 1 falls out of the 3-cycle window (runs 4, 3, 2).
        """
        conn = get_db_connection(self.db_path)
        # Insert run 1
        conn.execute(
            """
            INSERT INTO healing_runs (id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html)
            VALUES (1, '2026-09-20T10:00:00Z', 'OK', 19, 19, 1, '[]', '')
            """
        )
        conn.commit()
        conn.close()

        record_failed_action(
            run_id=1,
            container_name="db-cache",
            action_type="restart_container",
            verdict="CRITICAL_REGRESSION",
            details="Crashloop backoff",
            db_path=self.db_path,
        )

        # In run 1: must be discarded
        self.assertTrue(is_action_discarded("db-cache", "restart_container", window_cycles=3, db_path=self.db_path))

        # Insert run 2 and run 3
        conn = get_db_connection(self.db_path)
        conn.execute(
            "INSERT INTO healing_runs (id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html) VALUES (2, '2026-09-21T10:00:00Z', 'OK', 19, 19, 0, '[]', '')"
        )
        conn.execute(
            "INSERT INTO healing_runs (id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html) VALUES (3, '2026-09-22T10:00:00Z', 'OK', 19, 19, 0, '[]', '')"
        )
        conn.commit()
        conn.close()

        # At run 3: window covers runs 3, 2, 1 -> still discarded
        self.assertTrue(is_action_discarded("db-cache", "restart_container", window_cycles=3, db_path=self.db_path))
        self.assertEqual(len(get_discarded_hypotheses("db-cache", window_cycles=3, db_path=self.db_path)), 1)

        # Insert run 4: window covers runs 4, 3, 2 -> run 1 expires!
        conn = get_db_connection(self.db_path)
        conn.execute(
            "INSERT INTO healing_runs (id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html) VALUES (4, '2026-09-23T10:00:00Z', 'OK', 19, 19, 0, '[]', '')"
        )
        conn.commit()
        conn.close()

        # At run 4: run 1 is out of window
        self.assertFalse(is_action_discarded("db-cache", "restart_container", window_cycles=3, db_path=self.db_path))
        self.assertEqual(len(get_discarded_hypotheses("db-cache", window_cycles=3, db_path=self.db_path)), 0)


class TestHITLRejectionSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_healing_history.sqlite"
        self.orch_db_path = Path(self.temp_dir.name) / "test_orchestrator.db"
        migrate_db(self.db_path)

        # Create mock orchestrator.db
        conn = sqlite3.connect(str(self.orch_db_path))
        conn.execute(
            """
            CREATE TABLE approval_plans (
                id TEXT PRIMARY KEY,
                requester_id TEXT,
                requester_telegram_id TEXT,
                title TEXT,
                plan_details TEXT,
                task TEXT,
                target_project TEXT,
                status TEXT,
                created_at TIMESTAMP,
                reviewed_at TIMESTAMP
            )
            """
        )
        # Plan 1: Rejected plan for app-api
        conn.execute(
            """
            INSERT INTO approval_plans (
                id, requester_id, requester_telegram_id, title, plan_details, task, target_project, status, created_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "heal_plan_001",
                "test-user",
                "123456789",
                "Auto-Healing: Fix timeouts in app-api",
                "Diagnóstico automático de Auto-Healing para app-api:\n• Fix Sugerido: restart container",
                "docker restart app-api",
                "biometric-ai-platform",
                "rejected",
                "2026-09-23T12:00:00Z",
                "2026-09-23T12:05:00Z",
            ),
        )
        # Plan 2: Pending plan (should NOT be ingested)
        conn.execute(
            """
            INSERT INTO approval_plans (
                id, requester_id, requester_telegram_id, title, plan_details, task, target_project, status, created_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "heal_plan_002",
                "test-user",
                "123456789",
                "Auto-Healing: Fix disk space",
                "Prune unused images",
                "docker system prune",
                "homelab",
                "pending",
                "2026-09-23T12:10:00Z",
                None,
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_hitl_rejections_and_idempotency(self):
        # Initial sync
        synced = sync_hitl_rejections(orchestrator_db_path=self.orch_db_path, db_path=self.db_path)
        self.assertEqual(synced, 1)

        # Check rejected_hypotheses
        discarded = get_discarded_hypotheses("app-api", db_path=self.db_path)
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["plan_id"], "heal_plan_001")
        self.assertEqual(discarded[0]["rejection_source"], "HUMAN_HITL")
        self.assertTrue(is_action_discarded("app-api", "restart_container", db_path=self.db_path))

        # Second sync should be idempotent (0 new synced)
        synced_again = sync_hitl_rejections(orchestrator_db_path=self.orch_db_path, db_path=self.db_path)
        self.assertEqual(synced_again, 0)

    def test_sync_hitl_rejections_missing_db_handled_gracefully(self):
        missing_path = Path(self.temp_dir.name) / "non_existent.db"
        synced = sync_hitl_rejections(orchestrator_db_path=missing_path, db_path=self.db_path)
        self.assertEqual(synced, 0)


if __name__ == "__main__":
    unittest.main()
