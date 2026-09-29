import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Allow execution directly or as module
if __name__ == "__main__" and __package__ is None:
    file_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(file_dir.parent))
    from auto_healing.audit import (
        audit_remediation,
        get_discarded_hypotheses,
        is_action_discarded,
        migrate_db,
        record_failed_action,
        record_rejected_hypothesis,
    )
    from auto_healing.cognitive import analyze_log_anomaly
    from auto_healing.config import DB_PATH, MAX_REMEDIATIONS_PER_RUN
    from auto_healing.critic import lint_remediation_proposal
    from auto_healing.diagnostics import run_full_diagnostics
    from auto_healing.hitl import submit_hitl_approval_plan
    from auto_healing.remediations import prune_docker_dangling_images, restart_container
    from auto_healing.reporter import format_report_html, send_telegram_digest
else:
    from .audit import (
        audit_remediation,
        get_discarded_hypotheses,
        is_action_discarded,
        migrate_db,
        record_failed_action,
        record_rejected_hypothesis,
    )
    from .cognitive import analyze_log_anomaly
    from .config import DB_PATH, MAX_REMEDIATIONS_PER_RUN
    from .critic import lint_remediation_proposal
    from .diagnostics import run_full_diagnostics
    from .hitl import submit_hitl_approval_plan
    from .remediations import prune_docker_dangling_images, restart_container
    from .reporter import format_report_html, send_telegram_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_healing")


def init_db():
    """Initializes and migrates sqlite database to support evidence memory and regularized audit history."""
    migrate_db(DB_PATH)


def save_run(diag, remediations, audits, report_html) -> int:
    """Saves run details to database and returns the generated run_id."""
    conn = sqlite3.connect(DB_PATH)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO healing_runs (
                timestamp, host_health, containers_running, containers_total,
                remediations_count, audits_summary, report_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                diag["timestamp"],
                diag["host"]["disk"]["status"],
                diag["containers_running"],
                diag["containers_total"],
                len(remediations),
                json.dumps(audits),
                report_html,
            ),
        )
        run_id = cursor.lastrowid
    conn.close()
    return run_id


def main():
    parser = argparse.ArgumentParser(description="Homelab Auto-Healing and Infrastructure Audit Agent")
    parser.add_argument(
        "--dry-run", action="store_true", help="Run diagnostics and audit check without executing changes"
    )
    parser.add_argument("--notify", action="store_true", help="Send report to Telegram")
    parser.add_argument("--force-prune", action="store_true", help="Force prune dangling images")
    args = parser.parse_args()

    init_db()
    logger.info("🔍 Starting Homelab Diagnostics...")
    diag = run_full_diagnostics()

    remediations = []
    audits = []

    # Check candidates for remediation
    remediation_candidates = []

    for name in diag.get("unhealthy_containers", []):
        remediation_candidates.append({"action": "restart_container", "target": name})

    for name in diag.get("degraded_containers", []):
        remediation_candidates.append({"action": "restart_container", "target": name})

    if args.force_prune or diag["host"]["disk"]["status"] in ["WARN", "CRIT"]:
        remediation_candidates.append({"action": "prune_docker_dangling_images", "target": "docker_images"})

    logger.info(f"Identified {len(remediation_candidates)} potential remediation actions.")

    # Execute remediations up to safety limit with Evidence-Aware and Critic Pre-Flight filtering
    executed_count = 0
    for cand in remediation_candidates:
        if executed_count >= MAX_REMEDIATIONS_PER_RUN:
            logger.warning(
                f"⚠️ Reached max limit of {MAX_REMEDIATIONS_PER_RUN} remediations per run. Skipping further actions."
            )
            break

        action = cand["action"]
        target = cand["target"]

        # 1. Evidence-Aware Filtering: Is this action vetoed by local evidence memory?
        if is_action_discarded(target, action, window_cycles=3, db_path=DB_PATH):
            logger.warning(
                f"🚫 VETOED by Evidence Memory: Action '{action}' on '{target}' failed recently within 3 cycles. Skipping."
            )
            continue

        # 2. Critic Pre-Flight Screening
        critic_res = lint_remediation_proposal(
            task_description=f"{action} on {target}", command=f"docker {action} {target}", target_container=target
        )
        if not critic_res.is_valid:
            logger.warning(f"🛡️ CRITIC PRE-FLIGHT REJECTION for {target}: {critic_res.rejection_reason}")
            record_rejected_hypothesis(
                container_name=target,
                hypothesis_title=f"Remediación {action}",
                recommended_fix=action,
                worker_task=f"{action} on {target}",
                rejection_source="CRITIC_PREFLIGHT",
                rejection_reason=critic_res.rejection_reason,
                action_type=action,
                db_path=DB_PATH,
            )
            continue

        if args.dry_run:
            logger.info(f"[DRY-RUN] Would execute: {cand['action']} on {cand['target']}")
            remediations.append(
                {
                    "action": cand["action"],
                    "target": cand["target"],
                    "status": "DRY_RUN",
                    "message": "Dry-run execution simulated",
                }
            )
            audits.append(
                {
                    "verdict": "DRY_RUN_SIMULATED",
                    "details": "Simulated audit check (no actual changes applied).",
                    "improved": True,
                }
            )
        else:
            res = {}
            if action == "restart_container":
                res = restart_container(target)
            elif action == "prune_docker_dangling_images":
                res = prune_docker_dangling_images()

            remediations.append(res)
            # Post-action audit
            audit_res = audit_remediation(res)
            audits.append(audit_res)
            executed_count += 1

            # Evidence-aware feedback: If audit failed, record failed action for veto window
            verdict = audit_res.get("verdict", "")
            if verdict in ["STILL_UNHEALTHY", "CRITICAL_REGRESSION", "FAILED_TO_APPLY"] or not audit_res.get(
                "improved", False
            ):
                record_failed_action(
                    run_id=None,
                    container_name=target,
                    action_type=action,
                    verdict=verdict,
                    details=audit_res.get("details", "Audit reported unimproved or failed remediation"),
                    error_msg=res.get("message", ""),
                    db_path=DB_PATH,
                )

    report_html = format_report_html(diag, remediations, audits)
    print("\n" + "=" * 50)
    print(report_html)
    print("=" * 50 + "\n")

    run_id = save_run(diag, remediations, audits, report_html)

    # HITL Anomaly Analysis: analyze non-trivial log anomalies and offer Worker plans
    log_anomalies = diag.get("log_anomalies", [])
    if log_anomalies and not args.dry_run:
        logger.info(f"🔎 Found {len(log_anomalies)} services with log anomalies. Evaluating HITL fixes...")
        for anom in log_anomalies[:2]:  # Analyze top 2 services to avoid overwhelming
            c_name = anom.get("container")
            samples = anom.get("sample_errors", [])
            if samples:
                discarded = get_discarded_hypotheses(c_name, window_cycles=3, db_path=DB_PATH)
                diagnosis = analyze_log_anomaly(c_name, samples, discarded_hypotheses=discarded)

                # Pre-flight screening of cognitive proposal before submitting HITL plan
                worker_task = diagnosis.get("worker_task", "")
                critic_res = lint_remediation_proposal(task_description=worker_task, target_container=c_name)
                if not critic_res.is_valid:
                    logger.warning(f"🛡️ CRITIC REJECTED HITL proposal for {c_name}: {critic_res.rejection_reason}")
                    record_rejected_hypothesis(
                        container_name=c_name,
                        hypothesis_title=diagnosis.get("title", "Cognitive diagnosis"),
                        recommended_fix=diagnosis.get("recommended_fix", ""),
                        worker_task=worker_task,
                        rejection_source="CRITIC_PREFLIGHT",
                        rejection_reason=critic_res.rejection_reason,
                        run_id=run_id,
                        db_path=DB_PATH,
                    )
                else:
                    plan_id = submit_hitl_approval_plan(diagnosis, c_name)
                    logger.info(f"💡 Generated HITL Approval Plan '{plan_id}' for {c_name}")

    if args.notify:
        logger.info("📤 Sending Telegram digest...")
        send_telegram_digest(report_html)

    logger.info("✅ Auto-healing run finished successfully.")


if __name__ == "__main__":
    main()
