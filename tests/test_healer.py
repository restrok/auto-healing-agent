import unittest
from unittest.mock import patch

from auto_healing.audit import audit_remediation
from auto_healing.diagnostics import get_host_metrics, is_error_line, scan_container_logs
from auto_healing.reporter import format_report_html


class TestAutoHealing(unittest.TestCase):
    @patch("shutil.disk_usage")
    @patch("subprocess.run")
    def test_host_metrics(self, mock_subproc, mock_disk):
        mock_disk.return_value = (100 * 1024**3, 50 * 1024**3, 50 * 1024**3)
        mock_subproc.return_value.stdout = ""
        mock_subproc.return_value.returncode = 0

        metrics = get_host_metrics()
        self.assertEqual(metrics["disk"]["used_pct"], 50.0)
        self.assertEqual(metrics["disk"]["status"], "OK")

    @patch("auto_healing.audit.inspect_target_state")
    @patch("time.sleep", return_value=None)
    def test_audit_successful_restart(self, mock_sleep, mock_inspect):
        mock_inspect.return_value = {"running": True, "health": "healthy", "restart_count": 1}
        action = {"action": "restart_container", "target": "test-svc", "status": "APPLIED"}
        audit = audit_remediation(action)
        self.assertEqual(audit["verdict"], "POSITIVE")
        self.assertTrue(audit["improved"])

    @patch("auto_healing.audit.inspect_target_state")
    @patch("time.sleep", return_value=None)
    def test_audit_failed_restart(self, mock_sleep, mock_inspect):
        mock_inspect.return_value = {"running": False, "health": "unhealthy", "exit_code": 137}
        action = {"action": "restart_container", "target": "failing-svc", "status": "APPLIED"}
        audit = audit_remediation(action)
        self.assertEqual(audit["verdict"], "CRITICAL_REGRESSION")
        self.assertFalse(audit["improved"])

    def test_format_report_html(self):
        diag = {
            "timestamp": "2026-09-17T03:00:00Z",
            "host": {
                "disk": {"used_pct": 40.0, "free_gb": 100.0, "status": "OK"},
                "memory": {"total_mb": 16000, "available_mb": 8000, "swap_used_mb": 500, "status": "OK"},
                "issues": [],
            },
            "containers_running": 19,
            "containers_total": 19,
            "unhealthy_containers": [],
            "degraded_containers": [],
            "log_anomalies": [],
        }
        remediations = [{"action": "restart_container", "target": "dummy", "status": "APPLIED"}]
        audits = [{"verdict": "POSITIVE", "improved": True, "details": "Container healthy."}]
        html = format_report_html(diag, remediations, audits)
        self.assertIn("[Homelab Auto-Healing Report]", html)
        self.assertIn("POSITIVE", html)

    def test_severity_filter_is_error_line(self):
        # Line [INFO] with "critical" must NOT count as error
        info_line = "12:03:24 [INFO] src.agent.graph: EXCLUSION RULES (CRITICAL): Do not extract rules"
        self.assertFalse(is_error_line(info_line))

        # Line [ERROR] SI must count as error
        error_line = "12:03:24 [ERROR] src.agent.graph: Database connection timed out"
        self.assertTrue(is_error_line(error_line))

        # Other severe markers
        self.assertTrue(is_error_line("12:03:24 [CRITICAL] System out of memory"))
        self.assertTrue(is_error_line("12:03:24 [EXCEPTION] Unhandled traceback"))
        self.assertTrue(is_error_line("ERROR: Connection refused"))

    @patch("subprocess.run")
    def test_scan_container_logs_severity_filtering(self, mock_subproc):
        mock_subproc.return_value.stdout = (
            "12:03:24 [INFO] src.agent.graph: EXCLUSION RULES (CRITICAL): prompt\n"
            "12:03:25 [ERROR] src.agent.graph: ❌ DataScientist failed to generate report\n"
        )
        mock_subproc.return_value.stderr = ""
        mock_subproc.return_value.returncode = 0

        res = scan_container_logs("test-container", hours=1)
        self.assertEqual(res["error_count"], 1)
        self.assertEqual(len(res["sample_errors"]), 1)
        self.assertIn("[ERROR]", res["sample_errors"][0])
        self.assertNotIn("[INFO]", res["sample_errors"][0])


if __name__ == "__main__":
    unittest.main()
