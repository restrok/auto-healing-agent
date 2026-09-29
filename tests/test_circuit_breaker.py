import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from auto_healing.cognitive import (
    BOOTSTRAP_DEPENDENCY_ORDER,
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    analyze_log_anomaly,
    get_tier1_remediation,
)


class TestCircuitState(unittest.TestCase):
    """Verifies CircuitState enum definition."""

    def test_enum_values(self):
        self.assertEqual(CircuitState.CLOSED.value, "CLOSED")
        self.assertEqual(CircuitState.OPEN.value, "OPEN")
        self.assertEqual(CircuitState.HALF_OPEN.value, "HALF_OPEN")


class TestCircuitBreaker(unittest.TestCase):
    """Unit tests for the CircuitBreaker state machine, timeouts, and execution."""

    def setUp(self):
        self.breaker = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.1,  # Short recovery timeout for fast testing
            call_timeout=0.2,
        )

    def tearDown(self):
        self.breaker.close()

    def test_initial_state(self):
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)
        self.assertIsNone(self.breaker.last_failure_time)
        self.assertTrue(self.breaker.can_execute())

    def test_record_success_in_closed(self):
        self.breaker.failure_count = 1
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)

    def test_trip_to_open_on_threshold(self):
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 1)

        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.failure_count, 2)
        self.assertIsNotNone(self.breaker.last_failure_time)

    def test_fast_fail_when_open(self):
        self.breaker.state = CircuitState.OPEN
        self.breaker.last_failure_time = time.time()
        self.assertFalse(self.breaker.can_execute())

        with self.assertRaises(CircuitBreakerOpenError):
            self.breaker.execute(lambda: "should_not_run")

    def test_transition_open_to_half_open_after_cooldown(self):
        self.breaker.state = CircuitState.OPEN
        self.breaker.last_failure_time = time.time() - 0.2  # 0.2s > recovery_timeout (0.1s)
        self.assertTrue(self.breaker.can_execute())
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

    def test_half_open_success_resets_to_closed(self):
        self.breaker.state = CircuitState.HALF_OPEN
        self.breaker.failure_count = 2
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)
        self.assertIsNone(self.breaker.last_failure_time)

    def test_half_open_failure_trips_to_open(self):
        self.breaker.state = CircuitState.HALF_OPEN
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertIsNotNone(self.breaker.last_failure_time)

    def test_reset(self):
        self.breaker.state = CircuitState.OPEN
        self.breaker.failure_count = 5
        self.breaker.last_failure_time = time.time()
        self.breaker.reset()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)
        self.assertIsNone(self.breaker.last_failure_time)

    def test_execute_success(self):
        result = self.breaker.execute(lambda x, y: x + y, 3, 4)
        self.assertEqual(result, 7)
        self.assertEqual(self.breaker.failure_count, 0)

    def test_execute_failure_propagates_and_records(self):
        def faulty():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            self.breaker.execute(faulty)
        self.assertEqual(self.breaker.failure_count, 1)

    def test_execute_timeout_enforcement(self):
        def slow_function():
            time.sleep(0.5)
            return "done"

        with self.assertRaises(TimeoutError):
            self.breaker.execute(slow_function)

        # Timeout counts as a failure
        self.assertEqual(self.breaker.failure_count, 1)

    def test_thread_safety(self):
        def worker():
            for _ in range(50):
                self.breaker.can_execute()
                self.breaker.record_success()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(self.breaker.state, CircuitState.CLOSED)


class TestTier1Remediation(unittest.TestCase):
    """Verifies deterministic bootstrap dependency ordering and safe fallbacks."""

    def test_bootstrap_dependency_order_defined(self):
        self.assertIn("docker.service", BOOTSTRAP_DEPENDENCY_ORDER)
        self.assertIn("core-db-1", BOOTSTRAP_DEPENDENCY_ORDER)
        self.assertIn("core-llm-1", BOOTSTRAP_DEPENDENCY_ORDER)
        self.assertIn("core-brain-1", BOOTSTRAP_DEPENDENCY_ORDER)
        self.assertIn("core-scheduler-1", BOOTSTRAP_DEPENDENCY_ORDER)

    def test_docker_daemon_remediation(self):
        rem = get_tier1_remediation("docker.service", "Docker daemon unreachable")
        self.assertEqual(rem["worker_task"], "sudo systemctl restart docker.service")
        self.assertEqual(rem["target_project"], "homelab")
        self.assertEqual(rem["tier"], "tier1")
        self.assertIn("Docker", rem["title"])

    def test_neo4j_remediation(self):
        rem = get_tier1_remediation("core-db-1", "Healthcheck failed")
        self.assertEqual(rem["worker_task"], "docker restart core-db-1")
        self.assertEqual(rem["target_project"], "core-services")
        self.assertEqual(rem["tier"], "tier1")
        self.assertIn("Database", rem["title"])

    def test_ollama_remediation(self):
        rem = get_tier1_remediation("core-llm-1", "Port 11434 unreachable")
        self.assertEqual(rem["worker_task"], "docker restart core-llm-1")
        self.assertEqual(rem["target_project"], "core-services")
        self.assertEqual(rem["tier"], "tier1")
        self.assertIn("LLM", rem["title"])

    def test_brain_remediation(self):
        rem = get_tier1_remediation("core-brain-1", "Neo4j connection dropped")
        self.assertEqual(rem["worker_task"], "docker restart core-brain-1")
        self.assertEqual(rem["target_project"], "core-services")
        self.assertEqual(rem["tier"], "tier1")
        self.assertIn("Brain", rem["title"])

    def test_scheduler_remediation(self):
        rem = get_tier1_remediation("core-scheduler-1", "Cron execution stopped")
        self.assertEqual(rem["worker_task"], "docker restart core-scheduler-1")
        self.assertEqual(rem["target_project"], "core-services")
        self.assertEqual(rem["tier"], "tier1")
        self.assertIn("Scheduler", rem["title"])

    def test_generic_container_safe_fallback(self):
        rem = get_tier1_remediation("pihole", "DNS resolution slow")
        self.assertEqual(rem["worker_task"], "docker restart pihole")
        self.assertEqual(rem["target_project"], "homelab")
        self.assertEqual(rem["tier"], "tier1")
        self.assertIn("pihole", rem["title"])

    def test_status_inspect_fallback(self):
        rem = get_tier1_remediation("app-ingest", "Inspect network connectivity")
        self.assertEqual(rem["worker_task"], "docker inspect app-ingest")
        self.assertEqual(rem["target_project"], "app-platform")
        self.assertEqual(rem["tier"], "tier1")


class TestAnalyzeLogAnomaly(unittest.TestCase):
    """Verifies analyze_log_anomaly with CircuitBreaker, anti-circular check, and negative constraints."""

    def setUp(self):
        self.breaker = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=60.0,
            call_timeout=2.0,
        )

    def tearDown(self):
        self.breaker.close()

    @patch("urllib.request.urlopen")
    def test_successful_llm_diagnosis(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Fix DB Pool",
                                    "root_cause": "Conexiones agotadas",
                                    "recommended_fix": "Reiniciar servicio de base de datos",
                                    "worker_task": "docker restart postgres-db",
                                    "target_project": "homelab",
                                }
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = analyze_log_anomaly(
            "postgres-db",
            ["FATAL: remaining connection slots are reserved", "error connecting to db"],
            circuit_breaker=self.breaker,
        )

        self.assertEqual(res["title"], "Fix DB Pool")
        self.assertEqual(res["worker_task"], "docker restart postgres-db")
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

    @patch("urllib.request.urlopen")
    def test_anti_circular_dependency_bypasses_llm(self, mock_urlopen):
        # Core service should NEVER call the LLM
        res = analyze_log_anomaly(
            "core-llm-1",
            ["LLM out of memory", "Killed"],
            circuit_breaker=self.breaker,
        )

        mock_urlopen.assert_not_called()
        self.assertEqual(res["tier"], "tier1")
        self.assertEqual(res["worker_task"], "docker restart core-llm-1")

    @patch("urllib.request.urlopen")
    def test_fast_fail_when_circuit_open(self, mock_urlopen):
        self.breaker.state = CircuitState.OPEN
        self.breaker.last_failure_time = time.time()

        res = analyze_log_anomaly(
            "generic-app",
            "Error: connection refused",
            circuit_breaker=self.breaker,
        )

        mock_urlopen.assert_not_called()
        self.assertEqual(res["tier"], "tier1")
        self.assertIn("Circuit breaker is OPEN", res["root_cause"])

    @patch("urllib.request.urlopen")
    def test_fallback_on_network_error_trips_breaker(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        res1 = analyze_log_anomaly(
            "web-app",
            "502 Bad Gateway",
            circuit_breaker=self.breaker,
        )
        self.assertEqual(res1["tier"], "tier1")
        self.assertEqual(self.breaker.failure_count, 1)

        res2 = analyze_log_anomaly(
            "web-app",
            "502 Bad Gateway",
            circuit_breaker=self.breaker,
        )
        self.assertEqual(res2["tier"], "tier1")
        self.assertEqual(self.breaker.failure_count, 2)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

    @patch("urllib.request.urlopen")
    def test_discarded_hypotheses_injected_in_prompt(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Config Fix",
                                    "root_cause": "Falta parametro de memoria",
                                    "recommended_fix": "Aumentar buffer",
                                    "worker_task": "update config.json",
                                    "target_project": "homelab",
                                }
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        analyze_log_anomaly(
            "worker-svc",
            "High memory usage",
            discarded_hypotheses=["restart_container", "docker restart worker-svc"],
            circuit_breaker=self.breaker,
        )

        # Inspect request payload sent to urlopen
        called_req = mock_urlopen.call_args[0][0]
        body = json.loads(called_req.data.decode("utf-8"))
        prompt_content = body["messages"][1]["content"]

        self.assertIn("NEGATIVE CONSTRAINTS", prompt_content)
        self.assertIn("restart_container", prompt_content)
        self.assertIn("docker restart worker-svc", prompt_content)

    @patch("urllib.request.urlopen")
    def test_discarded_hypotheses_post_filter_veto(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Restart proposal",
                                    "root_cause": "Process deadlocked",
                                    "recommended_fix": "Reiniciar servicio",
                                    "worker_task": "docker restart worker-svc",
                                    "target_project": "homelab",
                                }
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Operator already rejected "docker restart worker-svc"
        res = analyze_log_anomaly(
            "worker-svc",
            "Deadlock detected",
            discarded_hypotheses=["docker restart worker-svc"],
            circuit_breaker=self.breaker,
        )

        # Because LLM proposed the discarded hypothesis, post-filter vetoes it and returns Tier 1
        self.assertEqual(res["tier"], "tier1")
        self.assertIn("Vetoed discarded hypothesis", res["root_cause"])


if __name__ == "__main__":
    unittest.main()
