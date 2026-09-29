"""
Pre-Flight Critic & Leakage Screening Module for RRSI-Inspired Resilient Auto-Healing.

Enforces pre-flight regularization on proposed remediation actions and tasks before
execution or Human-In-The-Loop (HITL) dispatch:
- Rule 1 (EPHEMERAL_ID): Screen for ephemeral container hex IDs (12-char and 64-char hashes).
- Rule 2 (VOLATILE_PATH): Screen for volatile filesystem paths (/tmp/*, /var/tmp/*, /dev/shm/*).
- Rule 3 (DESTRUCTIVE_COMMAND): Screen for forbidden destructive commands (rm -rf, docker system prune, etc.).
- Rule 4 (NON_ATOMIC): Screen for compound commands (&&, ;, ||, |) enforcing update sparsity (max 1 mutation/cycle).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("auto_healing.critic")

# Primary violation codes
VIOLATION_EPHEMERAL_ID = "EPHEMERAL_ID"
VIOLATION_VOLATILE_PATH = "VOLATILE_PATH"
VIOLATION_DESTRUCTIVE_COMMAND = "DESTRUCTIVE_COMMAND"
VIOLATION_NON_ATOMIC = "NON_ATOMIC"

# Secondary / specification aliases
ALIAS_EPHEMERAL_ID = "EPHEMERAL_CONTAINER_ID_LEAKAGE"
ALIAS_VOLATILE_PATH = "VOLATILE_PATH_LEAKAGE"
ALIAS_DESTRUCTIVE_COMMAND = "FORBIDDEN_DESTRUCTIVE_COMMAND"
ALIAS_NON_ATOMIC = "NON_ATOMIC_ACTION_UPDATE_SPARSITY_VIOLATION"


@dataclass
class CriticResult:
    """Pre-flight validation result for a proposed remediation."""

    is_valid: bool
    rejection_reason: str = ""
    violation_type: str | None = None
    violations: list[str] = field(default_factory=list)
    sanitized_task: str | None = None

    def __post_init__(self) -> None:
        if self.violation_type and not self.violations:
            self.violations = [self.violation_type]
        elif self.violations and not self.violation_type:
            self.violation_type = self.violations[0]

    @property
    def reason(self) -> str:
        """Alias for rejection_reason to support varying caller conventions."""
        return self.rejection_reason

    @reason.setter
    def reason(self, value: str) -> None:
        self.rejection_reason = value


# Spec alias
CriticVerdict = CriticResult


# Compiled Regex Rules

# Rule 1: Ephemeral container hex IDs (12-char short and 64-char full SHA256 hashes)
EPHEMERAL_ID_12_PATTERN = re.compile(r"\b[0-9a-fA-F]{12}\b")
EPHEMERAL_ID_64_PATTERN = re.compile(r"\b[0-9a-fA-F]{64}\b")

# Rule 2: Volatile filesystem paths (/tmp/, /var/tmp/, /dev/shm/)
VOLATILE_PATH_PATTERN = re.compile(
    r"/(?:tmp|var/tmp|dev/shm)(?:/[^\s\"'`;]+|/|\b)",
    re.IGNORECASE,
)

# Rule 3: Destructive commands
DESTRUCTIVE_RM_PATTERN = re.compile(
    r"\brm\s+(?:"
    r"-[a-zA-Z]*r[a-zA-Z]*f\b|"
    r"-[a-zA-Z]*f[a-zA-Z]*r\b|"
    r"-[a-zA-Z]*r\b\s+-[a-zA-Z]*f\b|"
    r"-[a-zA-Z]*f\b\s+-[a-zA-Z]*r\b|"
    r"--recursive\s+--force\b|"
    r"--force\s+--recursive\b|"
    r"-[a-zA-Z]*r[a-zA-Z]*\s+(?:/|/\*|\*|~)"
    r")",
    re.IGNORECASE,
)

DESTRUCTIVE_DOCKER_PATTERN = re.compile(
    r"\bdocker\s+(?:system\s+prune|volume\s+(?:prune|rm))\b",
    re.IGNORECASE,
)

RAW_DISK_PATTERN = re.compile(
    r"\b(?:mkfs(?:\.[a-zA-Z0-9]+|\b)|dd\s+if=.*of=/dev/|\bfdisk\b|\bparted\b)",
    re.IGNORECASE,
)

HOST_POWER_PATTERN = re.compile(
    r"\b(?:shutdown|reboot|poweroff|init\s+0|halt)\b",
    re.IGNORECASE,
)

DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (DESTRUCTIVE_RM_PATTERN, "Destructive recursive removal (rm -rf / rm -r)"),
    (DESTRUCTIVE_DOCKER_PATTERN, "Uncontained Docker prune or volume deletion"),
    (RAW_DISK_PATTERN, "Raw disk manipulation or filesystem formatting"),
    (HOST_POWER_PATTERN, "Host shutdown/reboot command"),
]

# Rule 4: Action atomicity & update sparsity (max 1 mutation per cycle)
NON_ATOMIC_PATTERN = re.compile(
    r"(&&|\|\||;|\||\by luego\b|\band then\b|\bdespu[eé]s\b|\bafter that\b)",
    re.IGNORECASE,
)


def lint_remediation_proposal(
    task_description: str = "",
    command: str = "",
    target_container: str = "",
    **kwargs: Any,
) -> CriticResult:
    """Pre-flight validator screening remediation proposals against RRSI regularizing rules.

    Args:
        task_description: Human-readable task description or command string.
        command: Exact shell command if provided separately.
        target_container: Target container name or ID.
        **kwargs: Additional parameters (e.g. 'task' alias).

    Returns:
        CriticResult indicating whether proposal is valid, with diagnostics if rejected.
    """
    if not task_description and "task" in kwargs:
        task_description = str(kwargs["task"])

    items_to_check: list[tuple[str, str]] = []
    if task_description:
        items_to_check.append(("task_description", task_description.strip()))
    if command:
        items_to_check.append(("command", command.strip()))
    if target_container:
        items_to_check.append(("target_container", target_container.strip()))

    if not items_to_check:
        logger.warning("Empty remediation proposal submitted to critic")
        return CriticResult(
            is_valid=False,
            rejection_reason="Empty remediation proposal: no task, command, or target specified.",
            violation_type="EMPTY_PROPOSAL",
            violations=["EMPTY_PROPOSAL"],
        )

    full_text = " ".join(val for _, val in items_to_check)

    violations: list[str] = []
    reasons: list[str] = []
    primary_violation: str | None = None

    # Rule 1: Ephemeral container hex IDs (screen 64-char then 12-char)
    match_64 = EPHEMERAL_ID_64_PATTERN.search(full_text)
    match_12 = EPHEMERAL_ID_12_PATTERN.search(full_text)
    if match_64 or match_12:
        matched_id = (match_64 or match_12).group(0)
        violations.extend([VIOLATION_EPHEMERAL_ID, ALIAS_EPHEMERAL_ID])
        reasons.append(
            f"Ephemeral container hex ID detected: '{matched_id}'. "
            "Remediations must target stable container or service names, not ephemeral container IDs."
        )
        if primary_violation is None:
            primary_violation = VIOLATION_EPHEMERAL_ID

    # Rule 2: Volatile filesystem paths
    match_vol = VOLATILE_PATH_PATTERN.search(full_text)
    if match_vol:
        matched_path = match_vol.group(0).strip()
        violations.extend([VIOLATION_VOLATILE_PATH, ALIAS_VOLATILE_PATH])
        reasons.append(
            f"Volatile filesystem path detected: '{matched_path}'. "
            "Actions must not depend on transient paths (/tmp, /var/tmp, /dev/shm)."
        )
        if primary_violation is None:
            primary_violation = VIOLATION_VOLATILE_PATH

    # Rule 3: Forbidden destructive commands
    for pat, desc in DESTRUCTIVE_PATTERNS:
        match_dest = pat.search(full_text)
        if match_dest:
            matched_cmd = match_dest.group(0)
            violations.extend([VIOLATION_DESTRUCTIVE_COMMAND, ALIAS_DESTRUCTIVE_COMMAND])
            reasons.append(
                f"Forbidden destructive command detected ({desc}): '{matched_cmd}'. "
                "Command risks unrecoverable data loss or system failure."
            )
            if primary_violation is None:
                primary_violation = VIOLATION_DESTRUCTIVE_COMMAND
            break

    # Rule 4: Action atomicity & update sparsity
    match_atomic = NON_ATOMIC_PATTERN.search(full_text)
    if match_atomic:
        matched_op = match_atomic.group(0)
        violations.extend([VIOLATION_NON_ATOMIC, ALIAS_NON_ATOMIC, "UPDATE_SPARSITY"])
        reasons.append(
            f"Non-atomic compound command/task detected: '{matched_op}'. "
            "Enforcing max 1 mutation per cycle per RRSI update sparsity."
        )
        if primary_violation is None:
            primary_violation = VIOLATION_NON_ATOMIC

    if primary_violation is not None:
        rejection_str = "; ".join(reasons)
        logger.info(f"Critic rejected remediation proposal: {rejection_str}")
        return CriticResult(
            is_valid=False,
            rejection_reason=rejection_str,
            violation_type=primary_violation,
            violations=violations,
            sanitized_task=None,
        )

    # Valid proposal passed all pre-flight screening
    clean_task = command if command else task_description
    logger.debug(f"Critic approved remediation proposal: {clean_task}")
    return CriticResult(
        is_valid=True,
        rejection_reason="",
        violation_type=None,
        violations=[],
        sanitized_task=clean_task,
    )
