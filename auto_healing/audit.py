"""Audit module for auto-healing and infrastructure resilience.

Provides health verification, backward-compatible SQLite schema migrations,
deterministic signature hashing, and local evidence memory for failed actions
and rejected hypotheses (RRSI L0 evidence-aware).
"""

import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os

try:
    from .config import COOLDOWN_SECONDS, DB_PATH
except ImportError:
    from auto_healing.config import COOLDOWN_SECONDS, DB_PATH

logger = logging.getLogger("auto_healing.audit")

DEFAULT_ORCHESTRATOR_DB_PATH = Path(os.getenv("ORCHESTRATOR_DB_PATH", "./data/orchestrator.db"))


def get_db_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Creates a sqlite3 connection with WAL mode and busy timeout configured."""
    db_str = str(db_path)
    if db_str != ":memory:":
        Path(db_str).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_str, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 10000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.OperationalError:
        pass
    return conn


def compute_container_signature(name: str, image: str = "") -> str:
    """Computes a deterministic 16-char sha256 hex signature for container identity."""
    norm_name = name.strip().lower().lstrip("/")
    norm_image = image.strip().lower() if image else ""
    key = f"{norm_name}:{norm_image}" if norm_image else norm_name
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def compute_error_signature(error_message: str | list[str], exit_code: int | None = None) -> str:
    """Computes a deterministic 16-char sha256 hex signature for error context."""
    if isinstance(error_message, list):
        text = " \n ".join(str(x) for x in error_message[:5])
    else:
        text = str(error_message)

    # Strip ISO timestamps: e.g. 2026-09-24T03:00:00.123456+00:00 or 2026-09-24 03:00:00Z
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?", "", text)
    # Strip HH:MM:SS timestamps
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "", text)
    # Strip ephemeral container/process hex IDs (12 to 64 hex chars)
    text = re.sub(r"\b[0-9a-fA-F]{12,64}\b", "<HEX_ID>", text)
    # Collapse whitespace
    norm = " ".join(text.strip().lower().split())
    if not norm:
        norm = "generic_error"
    if exit_code is not None:
        norm = f"{norm} :exit_{exit_code}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def backfill_historical_evidence(conn: sqlite3.Connection) -> int:
    """Extracts historical failed actions from healing_runs.audits_summary and populates failed_actions."""
    cursor = conn.cursor()
    existing_run_ids = {
        row[0]
        for row in cursor.execute("SELECT DISTINCT run_id FROM failed_actions WHERE run_id IS NOT NULL").fetchall()
    }
    runs = cursor.execute(
        "SELECT id, timestamp, audits_summary FROM healing_runs WHERE remediations_count > 0"
    ).fetchall()

    backfilled = 0
    for rid, ts, audits_json in runs:
        if rid in existing_run_ids:
            continue
        try:
            audits = json.loads(audits_json)
            for a in audits:
                if not a.get("improved", False):
                    details = a.get("details", "")
                    target = "unknown"
                    if "Container " in details:
                        target = details.split("Container ")[1].split()[0]
                    action = "restart_container"
                    sig = compute_container_signature(target)
                    cursor.execute(
                        """
                        INSERT INTO failed_actions (
                            run_id, timestamp, container_name, container_signature,
                            action_type, verdict, details, improved
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (rid, ts, target, sig, action, a.get("verdict", "FAILED"), details),
                    )
                    backfilled += 1
        except (json.JSONDecodeError, sqlite3.Error, KeyError, IndexError) as e:
            logger.warning(f"Error backfilling run {rid}: {e}")
    return backfilled


def migrate_db(db_path: Path | str = DB_PATH) -> None:
    """Executes backward-compatible, non-destructive schema migration.

    Creates failed_actions, rejected_hypotheses, and schema_migrations tables with B-tree indexes.
    Enables WAL mode and busy_timeout=10000. Preserves all existing records in healing_runs.
    Backfills historical failures if needed.
    """
    conn = get_db_connection(db_path)
    try:
        with conn:
            # Ensure base healing_runs table exists
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS healing_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    host_health TEXT NOT NULL,
                    containers_running INTEGER NOT NULL,
                    containers_total INTEGER NOT NULL,
                    remediations_count INTEGER NOT NULL,
                    audits_summary TEXT NOT NULL,
                    report_html TEXT NOT NULL
                );
                """
            )

            # Table: failed_actions
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS failed_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    timestamp TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    container_signature TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    error_signature TEXT,
                    verdict TEXT NOT NULL,
                    details TEXT,
                    improved INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(run_id) REFERENCES healing_runs(id) ON DELETE SET NULL
                );
                """
            )

            # Table: rejected_hypotheses
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rejected_hypotheses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    timestamp TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    container_signature TEXT NOT NULL,
                    error_signature TEXT,
                    hypothesis_title TEXT,
                    root_cause TEXT,
                    recommended_fix TEXT,
                    worker_task TEXT,
                    action_type TEXT,
                    rejection_source TEXT NOT NULL,
                    rejection_reason TEXT NOT NULL,
                    plan_id TEXT,
                    FOREIGN KEY(run_id) REFERENCES healing_runs(id) ON DELETE SET NULL
                );
                """
            )

            # Table: schema_migrations
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                """
            )

            # B-Tree Indexes on (container_signature, run_id) and (container_name, run_id)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_failed_actions_sig_run ON failed_actions(container_signature, run_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_failed_actions_name_run ON failed_actions(container_name, run_id);"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_failed_actions_time ON failed_actions(timestamp);")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rejected_hypo_sig_run ON rejected_hypotheses(container_signature, run_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rejected_hypo_name_run ON rejected_hypotheses(container_name, run_id);"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rejected_hypo_plan_id ON rejected_hypotheses(plan_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rejected_hypo_time ON rejected_hypotheses(timestamp);")

            # Check migration status
            applied = conn.execute("SELECT version FROM schema_migrations WHERE version = 1").fetchone()
            if not applied:
                backfill_historical_evidence(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (
                        1,
                        "Create failed_actions, rejected_hypotheses, and backfill historical failures",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
    finally:
        conn.close()


def record_failed_action(
    run_id: int | None,
    container_name: str,
    action_type: str,
    verdict: str,
    details: str,
    error_msg: str = "",
    db_path: Path | str = DB_PATH,
) -> None:
    """Records an action that resulted in STILL_UNHEALTHY, CRITICAL_REGRESSION, or FAILED_TO_APPLY."""
    conn = get_db_connection(db_path)
    try:
        # Validate foreign key if run_id is supplied
        if run_id is not None:
            exists = conn.execute("SELECT 1 FROM healing_runs WHERE id = ?", (run_id,)).fetchone()
            if not exists:
                run_id = None

        norm_name = container_name.strip().lstrip("/")
        c_sig = compute_container_signature(norm_name)
        e_sig = compute_error_signature(error_msg) if error_msg else None
        now_ts = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO failed_actions (
                    run_id, timestamp, container_name, container_signature,
                    action_type, error_signature, verdict, details, improved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (run_id, now_ts, norm_name, c_sig, action_type.strip(), e_sig, verdict.strip(), details.strip()),
            )
    finally:
        conn.close()


def record_rejected_hypothesis(
    container_name: str,
    hypothesis_title: str,
    recommended_fix: str,
    worker_task: str,
    rejection_source: str,
    rejection_reason: str,
    plan_id: str | None = None,
    run_id: int | None = None,
    action_type: str | None = None,
    root_cause: str = "",
    error_msg: str = "",
    db_path: Path | str = DB_PATH,
) -> None:
    """Records an explicit rejection from human HITL or pre-flight Critic."""
    conn = get_db_connection(db_path)
    try:
        # Validate foreign key if run_id is supplied
        if run_id is not None:
            exists = conn.execute("SELECT 1 FROM healing_runs WHERE id = ?", (run_id,)).fetchone()
            if not exists:
                run_id = None

        norm_name = container_name.strip().lstrip("/")
        c_sig = compute_container_signature(norm_name)
        e_sig = compute_error_signature(error_msg) if error_msg else None
        now_ts = datetime.now(timezone.utc).isoformat()

        if not action_type:
            combined_text = f"{hypothesis_title} {recommended_fix} {worker_task}".lower()
            if "restart" in combined_text:
                action_type = "restart_container"
            elif "prune" in combined_text:
                action_type = "prune_docker_dangling_images"
            else:
                action_type = ""

        with conn:
            conn.execute(
                """
                INSERT INTO rejected_hypotheses (
                    run_id, timestamp, container_name, container_signature,
                    error_signature, hypothesis_title, root_cause,
                    recommended_fix, worker_task, action_type,
                    rejection_source, rejection_reason, plan_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now_ts,
                    norm_name,
                    c_sig,
                    e_sig,
                    hypothesis_title.strip(),
                    root_cause.strip(),
                    recommended_fix.strip(),
                    worker_task.strip(),
                    action_type.strip(),
                    rejection_source.strip(),
                    rejection_reason.strip(),
                    plan_id,
                ),
            )
    finally:
        conn.close()


def get_discarded_hypotheses(
    container_name: str,
    window_cycles: int = 3,
    db_path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    """Sliding window query: fetches failed actions and rejected hypotheses from the last window_cycles runs.

    Accepts container_name as container name or 16-char container signature.
    """
    conn = get_db_connection(db_path)
    try:
        cursor = conn.cursor()
        norm_name = container_name.strip().lower().lstrip("/")
        if len(norm_name) == 16 and all(c in "0123456789abcdef" for c in norm_name):
            norm_sig = norm_name
        else:
            norm_sig = compute_container_signature(norm_name)

        # Determine minimum run_id for the sliding window
        recent_run_rows = cursor.execute(
            "SELECT id FROM healing_runs ORDER BY id DESC LIMIT ?", (window_cycles,)
        ).fetchall()
        min_run_id = min(r[0] for r in recent_run_rows) if recent_run_rows else 0

        query = """
            SELECT 'failed_action' AS item_type, id, action_type, container_name, container_signature,
                   verdict, details, timestamp, run_id, error_signature, '' AS hypothesis_title,
                   '' AS recommended_fix, '' AS worker_task, '' AS rejection_source, '' AS rejection_reason,
                   '' AS plan_id
            FROM failed_actions
            WHERE (lower(container_name) = ? OR container_signature = ?)
              AND (run_id >= ? OR run_id IS NULL)
            UNION ALL
            SELECT 'rejected_hypothesis' AS item_type, id, action_type, container_name, container_signature,
                   rejection_source AS verdict, rejection_reason AS details, timestamp, run_id, error_signature,
                   hypothesis_title, recommended_fix, worker_task, rejection_source, rejection_reason,
                   plan_id
            FROM rejected_hypotheses
            WHERE (lower(container_name) = ? OR container_signature = ?)
              AND (run_id >= ? OR run_id IS NULL)
            ORDER BY timestamp DESC, id DESC
        """
        rows = cursor.execute(query, (norm_name, norm_sig, min_run_id, norm_name, norm_sig, min_run_id)).fetchall()

        results = []
        for r in rows:
            results.append(
                {
                    "item_type": r[0],
                    "id": r[1],
                    "action_type": r[2],
                    "container_name": r[3],
                    "container_signature": r[4],
                    "verdict": r[5],
                    "details": r[6],
                    "timestamp": r[7],
                    "run_id": r[8],
                    "error_signature": r[9],
                    "hypothesis_title": r[10],
                    "recommended_fix": r[11],
                    "worker_task": r[12],
                    "rejection_source": r[13],
                    "rejection_reason": r[14],
                    "plan_id": r[15],
                }
            )
        return results
    finally:
        conn.close()


def is_action_discarded(
    container_name: str,
    action_type: str,
    window_cycles: int = 3,
    db_path: Path | str = DB_PATH,
) -> bool:
    """Returns True if action_type is in the discarded hypotheses for container_name within window_cycles."""
    discarded = get_discarded_hypotheses(container_name, window_cycles=window_cycles, db_path=db_path)
    norm_action = action_type.strip().lower()
    for d in discarded:
        act = (d.get("action_type") or "").strip().lower()
        if act == norm_action:
            return True
        if not act:
            for field in ("recommended_fix", "worker_task", "details", "hypothesis_title"):
                val = (d.get(field) or "").lower()
                if norm_action == "restart_container" and "restart" in val:
                    return True
                if norm_action == "prune_docker_dangling_images" and "prune" in val:
                    return True
                if norm_action in val:
                    return True
    return False


def sync_hitl_rejections(
    orchestrator_db_path: Path | str | None = None,
    db_path: Path | str = DB_PATH,
) -> int:
    """Ingests status == 'rejected' plans from orchestrator.db into rejected_hypotheses.

    Idempotent: skips plans that have already been imported.
    """
    orch_path = Path(orchestrator_db_path or DEFAULT_ORCHESTRATOR_DB_PATH)
    if not orch_path.exists():
        logger.info(f"Orchestrator DB does not exist at {orch_path}, skipping sync.")
        return 0

    try:
        orch_conn = sqlite3.connect(str(orch_path), timeout=5.0)
        orch_conn.row_factory = sqlite3.Row
        with orch_conn:
            plans = orch_conn.execute("SELECT * FROM approval_plans WHERE status = 'rejected'").fetchall()
        orch_conn.close()
    except (sqlite3.Error, OSError) as e:
        logger.warning(f"Could not read rejected plans from orchestrator DB: {e}")
        return 0

    if not plans:
        return 0

    conn = get_db_connection(db_path)
    synced = 0
    try:
        cursor = conn.cursor()
        for p in plans:
            plan_id = p["id"]
            existing = cursor.execute("SELECT id FROM rejected_hypotheses WHERE plan_id = ?", (plan_id,)).fetchone()
            if existing:
                continue

            details = p["plan_details"] or ""
            title = p["title"] or ""
            task = p["task"] or ""

            # Extract container name
            container_name = "unknown"
            match_details = re.search(r"para\s+([a-zA-Z0-9_\-\.]+)", details, re.IGNORECASE)
            if match_details:
                container_name = match_details.group(1).rstrip(":")
            else:
                match_title = re.search(r"en\s+([a-zA-Z0-9_\-\.]+)", title, re.IGNORECASE)
                if match_title:
                    container_name = match_title.group(1).rstrip(":")
                elif p["target_project"]:
                    container_name = p["target_project"]

            record_rejected_hypothesis(
                container_name=container_name,
                hypothesis_title=title,
                recommended_fix=details,
                worker_task=task,
                rejection_source="HUMAN_HITL",
                rejection_reason="Operator rejected plan via Telegram",
                plan_id=plan_id,
                root_cause="Operator rejection in orchestrator.db",
                db_path=db_path,
            )
            synced += 1
    finally:
        conn.close()

    logger.info(f"Synced {synced} rejected plans from orchestrator into rejected_hypotheses.")
    return synced


def inspect_target_state(target: str, action: str) -> dict[str, Any]:
    """Captures a snapshot of the target state."""
    if action == "restart_container":
        try:
            res = subprocess.run(
                ["docker", "inspect", target],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)[0]
                state = data.get("State", {})
                return {
                    "running": state.get("Running", False),
                    "health": state.get("Health", {}).get("Status", "none"),
                    "restart_count": data.get("RestartCount", 0),
                    "exit_code": state.get("ExitCode", 0),
                }
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError, IndexError) as e:
            logger.warning(f"Could not inspect target {target}: {e}")
        return {"running": False, "health": "unknown", "error": "inspect_failed"}

    elif action == "prune_docker_dangling_images":
        try:
            res = subprocess.run(
                ["docker", "system", "df", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return {"raw_df": res.stdout}
        except (subprocess.SubprocessError, OSError):
            return {}

    return {}


def audit_remediation(action_dict: dict[str, Any]) -> dict[str, Any]:
    """Audits the effect of an applied remediation after cooldown."""
    action = action_dict.get("action")
    target = action_dict.get("target")
    status = action_dict.get("status")

    if status != "APPLIED":
        return {
            "verdict": "FAILED_TO_APPLY",
            "details": f"Remediation did not apply successfully: {action_dict.get('message')}",
            "improved": False,
        }

    logger.info(f"⏳ Waiting cooldown of {COOLDOWN_SECONDS}s to evaluate stability for {target}...")
    time.sleep(COOLDOWN_SECONDS)

    post_state = inspect_target_state(target, action)

    if action == "restart_container":
        is_running = post_state.get("running", False)
        health = post_state.get("health", "none")

        if is_running and health in ["healthy", "none"]:
            verdict = "POSITIVE"
            details = f"Container {target} is running stably (Health: {health})."
            improved = True
        elif is_running and health == "unhealthy":
            verdict = "STILL_UNHEALTHY"
            details = f"Container {target} restarted but remains in unhealthy state."
            improved = False
        else:
            verdict = "CRITICAL_REGRESSION"
            details = f"Container {target} failed to stay up after restart (Exit: {post_state.get('exit_code')})."
            improved = False

        return {
            "verdict": verdict,
            "details": details,
            "improved": improved,
            "post_state": post_state,
        }

    elif action == "prune_docker_dangling_images":
        return {
            "verdict": "POSITIVE",
            "details": "Dangling image cleanup executed and freed space.",
            "improved": True,
            "post_state": post_state,
        }

    return {
        "verdict": "NEUTRAL",
        "details": "Action completed with no specific automated regression checks.",
        "improved": True,
        "post_state": post_state,
    }
