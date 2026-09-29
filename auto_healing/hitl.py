import html
import json
import logging
import os
import urllib.request
import uuid
from typing import Any, Dict

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("auto_healing.hitl")


def submit_hitl_approval_plan(diagnosis: Dict[str, Any], container_name: str) -> str:
    """Inserts a pending approval plan into the orchestrator DB and dispatches interactive inline keyboard to Telegram."""
    plan_id = f"heal_{uuid.uuid4().hex[:8]}"
    title = f"Auto-Healing: {diagnosis.get('title', container_name)}"
    task = diagnosis.get("worker_task", "")
    target_project = diagnosis.get("target_project", "homelab")

    details = (
        f"Diagnóstico automático de Auto-Healing para {container_name}:\n\n"
        f"• Causa Raíz: {diagnosis.get('root_cause')}\n"
        f"• Fix Sugerido: {diagnosis.get('recommended_fix')}\n"
        f"• Tarea para Worker: {task}"
    )

    # 1. Register approval plan via Orchestrator API
    orchestrator_base = os.getenv("ORCHESTRATOR_API_URL", "http://localhost:8001").rstrip("/")
    api_url = f"{orchestrator_base}/api/plans"
    requester_id = os.getenv("REQUESTER_ID", "auto-healer")

    api_payload = {
        "plan_id": plan_id,
        "requester_id": requester_id,
        "requester_telegram_id": str(TELEGRAM_CHAT_ID),
        "title": title,
        "plan_details": details,
        "task": task,
        "target_project": target_project,
    }
    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(api_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                logger.info(f"✅ Approval plan registered via Orchestrator API: {plan_id}")
            else:
                logger.error(f"Failed to register approval plan via API: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Failed to register approval plan via API: {e}")

    # 2. Dispatch Interactive Telegram Notification for Operator Approval
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        approval_kb = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Aprobar y Ejecutar Worker",
                        "callback_data": f"approve_plan:{plan_id}",
                    },
                    {"text": "❌ Rechazar", "callback_data": f"reject_plan:{plan_id}"},
                ]
            ]
        }

        msg = (
            f"🛡️ <b>[Auto-Healing HITL] Propuesta de Fix para <code>{html.escape(container_name)}</code></b>\n\n"
            f"📌 <b>Diagnóstico:</b>\n{html.escape(diagnosis.get('root_cause', ''))}\n\n"
            f"💡 <b>Solución Sugerida:</b>\n{html.escape(diagnosis.get('recommended_fix', ''))}\n\n"
            f"📁 <b>Proyecto Destino:</b> <code>{html.escape(target_project)}</code>\n"
            f"📝 <b>Plan para Worker:</b>\n<i>{html.escape(task)}</i>\n\n"
            f"<i>¿Deseas autorizar la ejecución del Worker Antigravity para aplicar este fix?</i>"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": approval_kb,
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                if res_data.get("ok"):
                    logger.info(f"📨 Interactive HITL approval request sent to Telegram for {plan_id}")
                else:
                    logger.error(f"Telegram returned error: {res_data}")
        except Exception as e:
            logger.error(f"Failed to send interactive Telegram approval: {e}")

    return plan_id
