"""Comprehensive E2E Requirement-Driven Test Suite for RRSI-Inspired Resilient Auto-Healing.

Derived strictly from ORIGINAL_REQUEST.md, PROJECT.md, and TEST_INFRA.md.
Covers:
- Tier 1: Feature Coverage (CircuitBreaker timeout, Evidence Memory storage, Critic linting)
- Tier 2: Boundary & Corner Cases (Socket timeouts, 0ms fast-fail, 12/64-char hex strings, empty DB, 0-cycle vs 3-cycle windows)
- Tier 3: Cross-Feature Combinations (Circuit breaker tripped + Critic pre-flight; 3-cycle discarded hypothesis veto + Tier 1 bootstrap fallback)
- Tier 4: Real-World Homelab Acceptance Scenarios (S1-S6)

Hermetic execution: All external services (Docker, Ollama HTTP, Telegram, Orchestrator API) are isolated
via mocks to prevent side-effects on live homelab containers.
Compatible with standard library unittest and pytest via UV.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from auto_healing.audit import (
    compute_container_signature,
    compute_error_signature,
    get_db_connection,
    get_discarded_hypotheses,
    is_action_discarded,
    migrate_db,
    record_failed_action,
    record_rejected_hypothesis,
)
from auto_healing.cognitive import (
    CircuitBreaker,
    CircuitState,
    analyze_log_anomaly,
    get_tier1_remediation,
)
from auto_healing.critic import (
    VIOLATION_DESTRUCTIVE_COMMAND,
    VIOLATION_EPHEMERAL_ID,
    VIOLATION_NON_ATOMIC,
    VIOLATION_VOLATILE_PATH,
    lint_remediation_proposal,
)
from auto_healing import healer


# ==============================================================================
# Sample Production Database Fixture (9 Real Homelab Production Runs)
# ==============================================================================
SAMPLE_PRODUCTION_RUNS = [
    (1, "2026-09-17T07:00:01Z", "OK", 14, 14, 0, "[]", "<html>Report 1</html>"),
    (2, "2026-09-18T07:00:02Z", "OK", 14, 14, 0, "[]", "<html>Report 2</html>"),
    (
        3,
        "2026-09-19T07:00:01Z",
        "WARN",
        13,
        14,
        1,
        json.dumps(
            [
                {
                    "target": "app-api",
                    "action": "restart_container",
                    "verdict": "STILL_UNHEALTHY",
                    "details": "Container app-api restarted but remains in unhealthy state.",
                    "improved": False,
                }
            ]
        ),
        "<html>Report 3</html>",
    ),
    (4, "2026-09-20T07:00:02Z", "OK", 14, 14, 0, "[]", "<html>Report 4</html>"),
    (5, "2026-09-21T07:00:01Z", "OK", 14, 14, 0, "[]", "<html>Report 5</html>"),
    (
        6,
        "2026-09-22T07:00:03Z",
        "WARN",
        13,
        14,
        1,
        json.dumps(
            [
                {
                    "target": "gateway",
                    "action": "restart_container",
                    "verdict": "CRITICAL_REGRESSION",
                    "details": "Container gateway failed to stay up after restart (Exit: 137).",
                    "improved": False,
                }
            ]
        ),
        "<html>Report 6</html>",
    ),
    (7, "2026-09-23T07:00:01Z", "OK", 14, 14, 0, "[]", "<html>Report 7</html>"),
    (8, "2026-09-23T12:00:00Z", "OK", 14, 14, 0, "[]", "<html>Report 8</html>"),
    (9, "2026-09-24T00:40:18Z", "OK", 14, 14, 0, "[]", "<html>Report 9</html>"),
]


def populate_legacy_database(db_path: Path | str) -> None:
    """Populates an SQLite database with the legacy schema and the 9 production runs."""
    conn = sqlite3.connect(str(db_path))
    with conn:
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
        conn.executemany(
            """
            INSERT INTO healing_runs (
                id, timestamp, host_health, containers_running, containers_total,
                remediations_count, audits_summary, report_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            SAMPLE_PRODUCTION_RUNS,
        )
    conn.close()


# ==============================================================================
# Base Test Class with Hermetic Temp Environment
# ==============================================================================
class HermeticTestBase(unittest.TestCase):
    """Base test fixture providing a temporary hermetic database environment."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_resilience_")
        self.db_path = Path(self.temp_dir) / "test_healing_history.sqlite"
        populate_legacy_database(self.db_path)
        migrate_db(self.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


# ==============================================================================
# Tier 1: Feature Coverage (Equivalence Partitioning)
# ==============================================================================
class TestTier1FeatureCoverage(HermeticTestBase):
    """Tier 1: Feature coverage for CircuitBreaker, Evidence Memory, and Critic."""

    def test_tier1_circuit_breaker_timeout_and_fallback(self) -> None:
        """F1: Circuit breaker enforces <= 2.0s strict timeout on hanging function."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0, call_timeout=0.5)

        def hanging_call() -> dict[str, str]:
            time.sleep(1.0)
            return {"status": "ok"}

        start = time.time()
        with self.assertRaises(Exception):
            cb.execute(hanging_call)
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.8, "CircuitBreaker must interrupt hanging call at strict timeout")

    def test_tier1_circuit_breaker_state_transitions(self) -> None:
        """F2: Circuit breaker transitions from CLOSED to OPEN after failure threshold."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, call_timeout=0.5)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.can_execute())

        def failing_call() -> None:
            raise ConnectionRefusedError("Simulated connection refused")

        # 1st failure
        with self.assertRaises(ConnectionRefusedError):
            cb.execute(failing_call)
        self.assertEqual(cb.state, CircuitState.CLOSED)

        # 2nd failure -> trips breaker
        with self.assertRaises(ConnectionRefusedError):
            cb.execute(failing_call)
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.can_execute())

    def test_tier1_safe_mode_bootstrap_dependency_order(self) -> None:
        """F3: Tier 1 safe mode adheres strictly to deterministic bootstrap order."""
        # Docker service on host
        rem_docker = get_tier1_remediation("docker.service")
        self.assertIn(rem_docker["target_project"], ("host", "homelab"))
        self.assertIn("systemctl restart docker", rem_docker["worker_task"])

        # Level 1: Neo4j
        rem_neo4j = get_tier1_remediation("core-db-1")
        self.assertIn("core-db-1", rem_neo4j["worker_task"])

        # Level 2: Ollama
        rem_ollama = get_tier1_remediation("core-llm-1")
        self.assertIn("core-llm-1", rem_ollama["worker_task"])

        # Level 3: Brain
        rem_brain = get_tier1_remediation("core-brain-1")
        self.assertIn("core-brain-1", rem_brain["worker_task"])

    def test_tier1_signature_computation(self) -> None:
        """F5: Container and error signature hashing produce deterministic 16-char hex."""
        c_sig1 = compute_container_signature("app-api")
        c_sig2 = compute_container_signature("app-api")
        self.assertEqual(c_sig1, c_sig2)
        self.assertEqual(len(c_sig1), 16)

        # Error signature normalizes timestamps
        err_a = "2026-09-24T03:00:00Z [ERROR] Failed to bind port 8000: address already in use"
        err_b = "2026-09-25T11:22:33Z [ERROR] Failed to bind port 8000: address already in use"
        sig_a = compute_error_signature(err_a)
        sig_b = compute_error_signature(err_b)
        self.assertEqual(sig_a, sig_b, "Error signatures must be invariant to timestamps")

    def test_tier1_evidence_memory_storage_and_query(self) -> None:
        """F6 & F7: Failed actions are stored and retrieved via get_discarded_hypotheses."""
        record_failed_action(
            run_id=9,
            container_name="app-api",
            action_type="restart_container",
            verdict="STILL_UNHEALTHY",
            details="Container restarted but healthcheck failed",
            db_path=self.db_path,
        )

        discarded = get_discarded_hypotheses("app-api", window_cycles=3, db_path=self.db_path)
        self.assertGreaterEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["action_type"], "restart_container")
        self.assertTrue(is_action_discarded("app-api", "restart_container", window_cycles=3, db_path=self.db_path))

    def test_tier1_critic_pre_flight_linting(self) -> None:
        """F10, F11, F12, F13: Critic screens ephemeral IDs, volatile paths, destructive commands, atomicity."""
        # Clean atomic proposal
        clean = lint_remediation_proposal("docker restart app-api")
        self.assertTrue(clean.is_valid)

        # Ephemeral ID
        eph = lint_remediation_proposal("docker restart 7a2b9f3e12c4")
        self.assertFalse(eph.is_valid)
        self.assertEqual(eph.violation_type, VIOLATION_EPHEMERAL_ID)

        # Volatile path
        vol = lint_remediation_proposal("cat /tmp/server.sock")
        self.assertFalse(vol.is_valid)
        self.assertEqual(vol.violation_type, VIOLATION_VOLATILE_PATH)

        # Destructive command
        dest = lint_remediation_proposal("rm -rf /var/lib/docker")
        self.assertFalse(dest.is_valid)
        self.assertEqual(dest.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

        # Compound non-atomic command
        comp = lint_remediation_proposal("docker stop a && docker rm a")
        self.assertFalse(comp.is_valid)
        self.assertEqual(comp.violation_type, VIOLATION_NON_ATOMIC)


# ==============================================================================
# Tier 2: Boundary & Corner Cases (Boundary Value Analysis)
# ==============================================================================
class TestTier2BoundaryAndCornerCases(HermeticTestBase):
    """Tier 2: Boundary value analysis and edge condition stress."""

    def test_tier2_circuit_breaker_0ms_fast_fail(self) -> None:
        """Boundary: When circuit breaker is OPEN, analyze_log_anomaly fast-fails in < 0.05s."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0, call_timeout=0.5)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

        start = time.time()
        res = analyze_log_anomaly(
            container_name="generic-service",
            log_snippet=["Critical database connection error"],
            circuit_breaker=cb,
        )
        elapsed = time.time() - start

        self.assertLess(elapsed, 0.05, "OPEN circuit breaker must fast-fail in < 50ms")
        self.assertEqual(res["target_project"], "homelab")

    def test_tier2_exact_12_and_64_char_hex_detection(self) -> None:
        """Boundary: Exact 12-char and 64-char hex strings are flagged, but not 11-char or non-hex."""
        # Exact 12-char hex boundary
        res12 = lint_remediation_proposal("docker inspect a1b2c3d4e5f6")
        self.assertFalse(res12.is_valid)
        self.assertEqual(res12.violation_type, VIOLATION_EPHEMERAL_ID)

        # 11-char hex (below 12) -> not flagged as ephemeral hex ID
        res11 = lint_remediation_proposal("docker restart a1b2c3d4e5a")
        self.assertTrue(res11.is_valid)

        # Exact 64-char hex boundary
        hex64 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        res64 = lint_remediation_proposal(f"docker inspect {hex64}")
        self.assertFalse(res64.is_valid)
        self.assertEqual(res64.violation_type, VIOLATION_EPHEMERAL_ID)

        # Valid container name with hyphens and digits
        res_valid = lint_remediation_proposal("docker restart core-llm-1")
        self.assertTrue(res_valid.is_valid)

    def test_tier2_empty_database_and_null_queries(self) -> None:
        """Boundary: Querying on empty or fresh database returns empty list without error."""
        empty_db = Path(self.temp_dir) / "empty.sqlite"
        migrate_db(empty_db)

        discarded = get_discarded_hypotheses("non-existent-svc", window_cycles=3, db_path=empty_db)
        self.assertEqual(discarded, [])
        self.assertFalse(
            is_action_discarded("non-existent-svc", "restart_container", window_cycles=3, db_path=empty_db)
        )

    def test_tier2_window_cycles_boundaries(self) -> None:
        """Boundary: Sliding window cycles (0-cycle vs 3-cycle vs 7-cycle) filtering."""
        # Run 3 failed: app-api
        # Current runs total: 9 (runs 1..9)
        # Window 3 considers runs 7, 8, 9 -> Run 3 failure should NOT be returned
        discarded_w3 = get_discarded_hypotheses("app-api", window_cycles=3, db_path=self.db_path)
        self.assertEqual(len(discarded_w3), 0, "Run 3 failure must be outside the 3-cycle window")

        # Window 7 considers runs 3..9 -> Run 3 failure SHOULD be returned
        discarded_w7 = get_discarded_hypotheses("app-api", window_cycles=7, db_path=self.db_path)
        self.assertGreaterEqual(len(discarded_w7), 1, "Run 3 failure must be included inside a 7-cycle window")

    def test_tier2_corrupt_and_empty_log_snippets(self) -> None:
        """Boundary: Empty or corrupt log snippets do not cause unhandled crashes."""
        # Empty string
        res_empty_str = get_tier1_remediation("test-svc", "")
        self.assertIn("target_project", res_empty_str)

        # Normalized signature on empty text
        sig_empty = compute_error_signature("")
        self.assertEqual(len(sig_empty), 16)


# ==============================================================================
# Tier 3: Cross-Feature Combinations (Pairwise Combinations)
# ==============================================================================
class TestTier3CrossFeatureCombinations(HermeticTestBase):
    """Tier 3: Pairwise integration across circuit breaker, critic, and evidence memory."""

    def test_tier3_circuit_breaker_tripped_and_critic_pre_flight(self) -> None:
        """Cross: When circuit breaker trips, Tier 1 fallback task passes pre-flight Critic audit."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0, call_timeout=0.5)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Trigger fallback
        diagnosis = analyze_log_anomaly(
            container_name="app-api",
            log_snippet=["Worker process exited with code 1"],
            circuit_breaker=cb,
        )

        task = diagnosis.get("worker_task", "")
        self.assertTrue(task, "Tier 1 must generate a non-empty worker task")

        # Pre-flight audit Tier 1 generated task
        critic_verdict = lint_remediation_proposal(task, target_container="app-api")
        self.assertTrue(
            critic_verdict.is_valid,
            f"Tier 1 fallback task must pass pre-flight Critic audit without violations: {critic_verdict.rejection_reason}",
        )

    def test_tier3_discarded_hypothesis_veto_with_tier1_fallback(self) -> None:
        """Cross: 3-cycle discarded hypothesis vetoes candidate and triggers Tier 1 fallback."""
        container = "app-api"
        # Record a discarded restart
        record_rejected_hypothesis(
            container_name=container,
            hypothesis_title="Restart Container",
            recommended_fix="docker restart app-api",
            worker_task="docker restart app-api",
            rejection_source="OPERATOR_REJECTED",
            rejection_reason="Recurring OOM loop, restart futile",
            run_id=9,
            db_path=self.db_path,
        )

        discarded = get_discarded_hypotheses(container, window_cycles=3, db_path=self.db_path)
        self.assertGreaterEqual(len(discarded), 1)

        # Simulate LLM returning the discarded restart task
        mock_llm_response = {
            "title": "Reiniciar servicio app-api",
            "root_cause": "Falla transitoria de proceso",
            "recommended_fix": "docker restart app-api",
            "worker_task": "docker restart app-api",
            "target_project": "app-platform",
        }

        with patch("auto_healing.cognitive.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {"choices": [{"message": {"content": json.dumps(mock_llm_response)}}]}
            ).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            diag = analyze_log_anomaly(
                container_name=container,
                log_snippet=["OOMKilled process 1234"],
                discarded_hypotheses=discarded,
            )

            # The cognitive module must VETO the LLM output and fall back to Tier 1
            self.assertEqual(diag.get("tier"), "tier1")
            self.assertIn("Vetoed discarded hypothesis", diag.get("root_cause", ""))

    def test_tier3_audit_failure_persists_to_evidence_and_updates_pruning(self) -> None:
        """Cross: Audit failure verdict directly persists to evidence and marks action discarded."""
        target = "redis-cluster"
        action = "restart_container"

        # Record action failure
        record_failed_action(
            run_id=9,
            container_name=target,
            action_type=action,
            verdict="CRITICAL_REGRESSION",
            details="Container exited with code 1 immediately after restart",
            db_path=self.db_path,
        )

        # Immediately verify it is marked as discarded
        self.assertTrue(is_action_discarded(target, action, window_cycles=3, db_path=self.db_path))

    def test_tier3_critic_rejection_recorded_as_discarded_hypothesis(self) -> None:
        """Cross: Critic rejection can be recorded into evidence memory and queried."""
        target = "gateway"
        bad_task = "docker restart 8f4a1c9e2b10"

        critic_result = lint_remediation_proposal(bad_task, target_container=target)
        self.assertFalse(critic_result.is_valid)

        # Persist critic rejection
        record_rejected_hypothesis(
            container_name=target,
            hypothesis_title="Critic Rejected Remediation",
            recommended_fix="Use container name instead of hash",
            worker_task=bad_task,
            rejection_source="CRITIC_REJECTED",
            rejection_reason=critic_result.rejection_reason,
            run_id=9,
            db_path=self.db_path,
        )

        discarded = get_discarded_hypotheses(target, window_cycles=3, db_path=self.db_path)
        self.assertTrue(any(d["rejection_source"] == "CRITIC_REJECTED" for d in discarded))


# ==============================================================================
# Tier 4: Real-World Homelab Acceptance Scenarios
# ==============================================================================
class TestTier4RealWorldHomelabAcceptance(HermeticTestBase):
    """Tier 4: Acceptance criteria validation under real-world homelab operating conditions."""

    @patch("auto_healing.healer.send_telegram_digest")
    @patch("auto_healing.healer.submit_hitl_approval_plan")
    @patch("auto_healing.healer.run_full_diagnostics")
    def test_scenario_1_unreachable_ollama_executes_in_under_5s(
        self,
        mock_diag: MagicMock,
        mock_hitl: MagicMock,
        mock_telegram: MagicMock,
    ) -> None:
        """Scenario 1 (Acceptance R1): Unreachable/slow Ollama -> full cycle <= 5.0s in Tier 1 safe mode."""
        mock_hitl.return_value = "plan_test_s1"
        mock_telegram.return_value = True

        mock_diag.return_value = {
            "timestamp": "2026-09-24T03:00:00Z",
            "host": {"disk": {"status": "OK"}},
            "containers_running": 14,
            "containers_total": 14,
            "unhealthy_containers": [],
            "degraded_containers": [],
            "log_anomalies": [
                {
                    "container": "app-api",
                    "sample_errors": ["Error connecting to database at 127.0.0.1:5432"],
                }
            ],
        }

        # Simulate hanging / unreachable Ollama endpoint
        with (
            patch("auto_healing.cognitive.urllib.request.urlopen") as mock_urlopen,
            patch.object(healer, "DB_PATH", str(self.db_path)),
        ):

            def slow_hanging_network(*args: Any, **kwargs: Any) -> None:
                time.sleep(1.0)
                raise TimeoutError("Connection to Ollama timed out (127.0.0.1:11434)")

            mock_urlopen.side_effect = slow_hanging_network

            start_time = time.time()
            with patch("sys.argv", ["healer.py"]):
                try:
                    healer.main()
                    execution_success = True
                except Exception as e:
                    execution_success = False
                    self.fail(f"Healer raised unhandled exception during Ollama outage: {e}")

            elapsed_time = time.time() - start_time

            self.assertTrue(execution_success)
            self.assertLessEqual(
                elapsed_time,
                5.0,
                f"Auto-healing cycle exceeded 5.0s during Ollama outage (took {elapsed_time:.2f}s)",
            )
            mock_hitl.assert_called_once()
            plan_arg = mock_hitl.call_args[0][0]
            self.assertEqual(plan_arg["target_project"], "app-platform")

    def test_scenario_2_container_crash_loop_3_cycle_veto(self) -> None:
        """Scenario 2 (Acceptance R2): Container crash loop fails audit, excluded from next 3 cycles."""
        container = "app-api"
        action = "restart_container"

        # Cycle 9: Action fails
        record_failed_action(
            run_id=9,
            container_name=container,
            action_type=action,
            verdict="CRITICAL_REGRESSION",
            details="Container exited immediately with code 137 (OOM)",
            db_path=self.db_path,
        )

        # Cycles 10, 11, 12: Action must be vetoed
        for future_run_id in [10, 11, 12]:
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "INSERT INTO healing_runs (id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html) VALUES (?, '2026-09-24T12:00:00Z', 'OK', 14, 14, 0, '[]', 'html')",
                    (future_run_id,),
                )
            conn.close()

            is_vetoed = is_action_discarded(container, action, window_cycles=3, db_path=self.db_path)
            if future_run_id <= 11:
                # Runs in window: 9, 10, 11 (run 9 is in top 3)
                self.assertTrue(is_vetoed, f"Action must remain vetoed at run {future_run_id}")
            elif future_run_id == 12:
                # Top 3 runs: 12, 11, 10 -> run 9 is expired!
                self.assertFalse(is_vetoed, f"Veto must expire after 3 cycles (at run {future_run_id})")

    def test_scenario_3_critic_blocks_ephemeral_container_hashes(self) -> None:
        """Scenario 3 (Acceptance R3): Critic blocks leakage of ephemeral container hashes."""
        # Simulated LLM hallucination with short hash
        bad_task_short = "docker restart 4f8a1c9e2b10"
        res_short = lint_remediation_proposal(bad_task_short)
        self.assertFalse(res_short.is_valid)
        self.assertIn("Ephemeral container hex ID detected", res_short.rejection_reason)

        # Simulated LLM hallucination with full 64-char hash
        bad_task_long = "docker logs 3a2c5b7e9f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c"
        res_long = lint_remediation_proposal(bad_task_long)
        self.assertFalse(res_long.is_valid)
        self.assertIn("Ephemeral container hex ID detected", res_long.rejection_reason)

        # Correct proposal targeting stable container name
        good_task = "docker restart app-api"
        res_good = lint_remediation_proposal(good_task)
        self.assertTrue(res_good.is_valid)

    def test_scenario_4_critic_blocks_destructive_commands(self) -> None:
        """Scenario 4 (Acceptance R3): Critic blocks destructive commands (rm -rf, uncontained prune)."""
        # rm -rf
        rm_task = "rm -rf /var/lib/docker/overlay2"
        res_rm = lint_remediation_proposal(rm_task)
        self.assertFalse(res_rm.is_valid)
        self.assertEqual(res_rm.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

        # docker system prune -a --volumes
        prune_task = "docker system prune -a --volumes"
        res_prune = lint_remediation_proposal(prune_task)
        self.assertFalse(res_prune.is_valid)
        self.assertEqual(res_prune.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

    def test_scenario_5_telegram_operator_rejection_synced_and_excluded(self) -> None:
        """Scenario 5 (Acceptance R2): Operator rejection in Telegram synced and excluded from cognitive search."""
        container = "telegram-agent-orchestrator"
        rejected_action = "docker restart telegram-agent-orchestrator"

        # Federico clicks Reject on Telegram
        record_rejected_hypothesis(
            container_name=container,
            hypothesis_title="Reiniciar Orquestador",
            recommended_fix="docker restart telegram-agent-orchestrator",
            worker_task=rejected_action,
            rejection_source="OPERATOR_REJECTED",
            rejection_reason="Federico rejected: service is waiting for network DNS sync",
            run_id=9,
            db_path=self.db_path,
        )

        discarded = get_discarded_hypotheses(container, window_cycles=3, db_path=self.db_path)
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["rejection_source"], "OPERATOR_REJECTED")

        # When cognitive analyzes the log anomaly, verify rejected hypothesis is passed to prompt
        with patch("auto_healing.cognitive.urllib.request.urlopen") as mock_urlopen:
            captured_prompts: list[str] = []

            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "title": "Inspeccionar DNS",
                                        "root_cause": "Falla de resolución DNS",
                                        "recommended_fix": "Verificar resolv.conf en host",
                                        "worker_task": "cat /etc/resolv.conf",
                                        "target_project": "telegram-agent-orchestrator",
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp

            def fake_urlopen(req: Any, timeout: float = 2.0) -> Any:
                body = json.loads(req.data.decode("utf-8"))
                user_msg = body["messages"][1]["content"]
                captured_prompts.append(user_msg)
                return mock_resp

            mock_urlopen.side_effect = fake_urlopen

            diag = analyze_log_anomaly(
                container_name=container,
                log_snippet=["DNS resolution timeout for api.telegram.org"],
                discarded_hypotheses=discarded,
            )

            self.assertGreaterEqual(len(captured_prompts), 1)
            self.assertIn("NEGATIVE CONSTRAINTS", captured_prompts[0])
            self.assertIn(rejected_action, captured_prompts[0])
            self.assertEqual(diag["worker_task"], "cat /etc/resolv.conf")

    def test_scenario_6_non_destructive_migration_preserves_9_production_runs(self) -> None:
        """Scenario 6 (Acceptance Integridad): Non-destructive migration preserving 9 production runs."""
        # Check that after migrate_db, all 9 historical rows are identical
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        runs = cursor.execute(
            "SELECT id, timestamp, host_health, containers_running, containers_total, remediations_count, audits_summary, report_html FROM healing_runs ORDER BY id ASC"
        ).fetchall()

        self.assertEqual(len(runs), 9, "All 9 production runs must be preserved exactly")
        for orig, migrated in zip(SAMPLE_PRODUCTION_RUNS, runs):
            self.assertEqual(orig[0], migrated[0], "Run IDs must match")
            self.assertEqual(orig[1], migrated[1], "Timestamps must match")
            self.assertEqual(orig[2], migrated[2], "Host health must match")
            self.assertEqual(orig[3], migrated[3], "Running containers must match")
            self.assertEqual(orig[4], migrated[4], "Total containers must match")
            self.assertEqual(orig[5], migrated[5], "Remediations count must match")
            self.assertEqual(orig[6], migrated[6], "Audits summary must match")
            self.assertEqual(orig[7], migrated[7], "Report HTML must match")

        # Verify new tables exist
        tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("failed_actions", tables)
        self.assertIn("rejected_hypotheses", tables)
        self.assertIn("schema_migrations", tables)

        # Verify historical backfill captured past failures from runs 3 and 6
        failed_actions = cursor.execute("SELECT container_name, verdict, run_id FROM failed_actions").fetchall()
        self.assertGreaterEqual(len(failed_actions), 2, "Backfill must have extracted past failures")
        containers_backfilled = {r[0] for r in failed_actions}
        self.assertIn("app-api", containers_backfilled)
        self.assertIn("gateway", containers_backfilled)

        conn.close()


if __name__ == "__main__":
    unittest.main()
