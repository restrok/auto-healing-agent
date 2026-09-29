"""
Unit Tests for Pre-Flight Critic and Leakage Screening (critic.py).
RRSI Requirement R3 Verification Suite.
"""

from __future__ import annotations

import unittest

from auto_healing.critic import (
    VIOLATION_DESTRUCTIVE_COMMAND,
    VIOLATION_EPHEMERAL_ID,
    VIOLATION_NON_ATOMIC,
    VIOLATION_VOLATILE_PATH,
    CriticResult,
    CriticVerdict,
    lint_remediation_proposal,
)


class TestCritic(unittest.TestCase):
    """Test suite for Critic pre-flight screening and leakage detection."""

    # 1. Spec Requirement Test Cases (Suite 3 from spec_requirements.md §5.4.3)

    def test_critic_rejects_12_char_container_hex_id(self) -> None:
        """Rule 1: Verify 12-char container hex ID is rejected with diagnostic."""
        result = lint_remediation_proposal("docker restart 7a2b9f3e12c4")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_EPHEMERAL_ID)
        self.assertIn("EPHEMERAL_ID", result.violations)
        self.assertIn("EPHEMERAL_CONTAINER_ID_LEAKAGE", result.violations)
        self.assertIn("ephemeral container hex id", result.rejection_reason.lower())
        self.assertIn("7a2b9f3e12c4", result.rejection_reason)

    def test_critic_rejects_64_char_container_hex_id(self) -> None:
        """Rule 1: Verify 64-char full SHA256 container ID is rejected."""
        hex64 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        result = lint_remediation_proposal(f"docker logs {hex64}")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_EPHEMERAL_ID)
        self.assertIn(VIOLATION_EPHEMERAL_ID, result.violations)
        self.assertIn(hex64, result.rejection_reason)

    def test_critic_rejects_volatile_tmp_path(self) -> None:
        """Rule 2: Verify volatile /tmp/ path is rejected with diagnostic."""
        result = lint_remediation_proposal("cat /tmp/debug.log && rm /tmp/debug.log")
        self.assertFalse(result.is_valid)
        self.assertIn("volatile", result.rejection_reason.lower())
        self.assertIn("path", result.rejection_reason.lower())
        self.assertIn(VIOLATION_VOLATILE_PATH, result.violations)

    def test_critic_rejects_destructive_rm_rf(self) -> None:
        """Rule 3: Verify destructive rm -rf command is rejected."""
        result = lint_remediation_proposal("rm -rf /var/lib/docker/overlay2")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)
        self.assertIn(VIOLATION_DESTRUCTIVE_COMMAND, result.violations)
        self.assertIn("destructive", result.rejection_reason.lower())

    def test_critic_rejects_docker_system_prune_volumes(self) -> None:
        """Rule 3: Verify uncontained docker system prune is rejected."""
        result = lint_remediation_proposal("docker system prune -a --volumes")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)
        self.assertIn(VIOLATION_DESTRUCTIVE_COMMAND, result.violations)

    def test_critic_rejects_non_atomic_chained_commands(self) -> None:
        """Rule 4: Verify chained compound command is rejected per update sparsity."""
        result = lint_remediation_proposal("docker restart a && docker restart b")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_NON_ATOMIC)
        self.assertIn(VIOLATION_NON_ATOMIC, result.violations)
        self.assertTrue("atomic" in result.rejection_reason.lower() or "sparsity" in result.rejection_reason.lower())

    def test_critic_accepts_valid_atomic_task(self) -> None:
        """Verify valid atomic remediation command passes pre-flight screening."""
        result = lint_remediation_proposal("docker restart app-api")
        self.assertTrue(result.is_valid)
        self.assertIsNone(result.violation_type)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.rejection_reason, "")
        self.assertEqual(result.sanitized_task, "docker restart app-api")

    # 2. Comprehensive Boundary & Variation Tests

    def test_critic_rejects_target_container_ephemeral_id(self) -> None:
        """Rule 1: Target container parameter with hex ID is rejected."""
        result = lint_remediation_proposal(
            task_description="Restart container",
            command="docker restart",
            target_container="7a2b9f3e12c4",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_EPHEMERAL_ID)

    def test_critic_rejects_command_ephemeral_id(self) -> None:
        """Rule 1: Command parameter with hex ID is rejected."""
        result = lint_remediation_proposal(
            task_description="Restart API",
            command="docker restart 1234567890ab",
            target_container="app-api",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_EPHEMERAL_ID)

    def test_critic_rejects_var_tmp_path(self) -> None:
        """Rule 2: /var/tmp/ path is screened and rejected."""
        result = lint_remediation_proposal("rm -f /var/tmp/stale.sock")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_VOLATILE_PATH)

    def test_critic_rejects_dev_shm_path(self) -> None:
        """Rule 2: /dev/shm/ path is screened and rejected."""
        result = lint_remediation_proposal("cat /dev/shm/shared_memory.bin")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_VOLATILE_PATH)

    def test_critic_rejects_semicolon_compound_command(self) -> None:
        """Rule 4: Semicolon command separator is rejected."""
        result = lint_remediation_proposal("docker stop foo; docker rm foo")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_NON_ATOMIC)

    def test_critic_rejects_pipe_compound_command(self) -> None:
        """Rule 4: Shell pipe separator is rejected."""
        result = lint_remediation_proposal("docker ps | grep dead")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_NON_ATOMIC)

    def test_critic_rejects_or_compound_command(self) -> None:
        """Rule 4: Shell || operator is rejected."""
        result = lint_remediation_proposal("docker restart a || docker restart b")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_NON_ATOMIC)

    def test_critic_rejects_multi_action_natural_language(self) -> None:
        """Rule 4: Multi-action natural language phrases are rejected."""
        result = lint_remediation_proposal("Reiniciar contenedor app-api y luego eliminar logs")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_NON_ATOMIC)

        result_en = lint_remediation_proposal("Restart container app-api and then delete logs")
        self.assertFalse(result_en.is_valid)
        self.assertEqual(result_en.violation_type, VIOLATION_NON_ATOMIC)

    def test_critic_rejects_destructive_docker_volume_prune(self) -> None:
        """Rule 3: docker volume prune is rejected."""
        result = lint_remediation_proposal("docker volume prune -f")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

    def test_critic_rejects_destructive_docker_volume_rm(self) -> None:
        """Rule 3: docker volume rm is rejected."""
        result = lint_remediation_proposal("docker volume rm db_data")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

    def test_critic_rejects_destructive_formatting(self) -> None:
        """Rule 3: Raw formatting or block device manipulation is rejected."""
        result_mkfs = lint_remediation_proposal("mkfs.ext4 /dev/sdb1")
        self.assertFalse(result_mkfs.is_valid)
        self.assertEqual(result_mkfs.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

        result_dd = lint_remediation_proposal("dd if=/dev/zero of=/dev/sda bs=1M")
        self.assertFalse(result_dd.is_valid)
        self.assertEqual(result_dd.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

    def test_critic_rejects_host_reboot(self) -> None:
        """Rule 3: Host shutdown/reboot is rejected."""
        result = lint_remediation_proposal("sudo reboot")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, VIOLATION_DESTRUCTIVE_COMMAND)

    def test_critic_accepts_safe_remediations(self) -> None:
        """Verify various legitimate auto-healing remediations pass."""
        # Safe image prune (allowed in remediations.py)
        res1 = lint_remediation_proposal("docker image prune -f")
        self.assertTrue(res1.is_valid)

        # Safe systemctl restart
        res2 = lint_remediation_proposal("sudo systemctl restart docker")
        self.assertTrue(res2.is_valid)

        # Safe container logs query
        res3 = lint_remediation_proposal(
            "docker logs --tail 100 app-api",
            target_container="app-api",
        )
        self.assertTrue(res3.is_valid)

    def test_critic_rejects_empty_proposal(self) -> None:
        """Verify empty input is rejected cleanly without error."""
        result = lint_remediation_proposal("")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.violation_type, "EMPTY_PROPOSAL")

    def test_critic_result_properties_and_aliases(self) -> None:
        """Verify CriticResult dataclass properties, reason alias, and CriticVerdict."""
        res = CriticResult(
            is_valid=False,
            rejection_reason="Test reason",
            violation_type=VIOLATION_EPHEMERAL_ID,
        )
        self.assertEqual(res.reason, "Test reason")
        self.assertEqual(res.violations, [VIOLATION_EPHEMERAL_ID])
        self.assertIs(CriticVerdict, CriticResult)


if __name__ == "__main__":
    unittest.main()
