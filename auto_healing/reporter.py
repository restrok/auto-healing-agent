import html
import json
import logging
import urllib.request
from typing import Any

from .config import NOTIFY_USER_ID, ORCHESTRATOR_API_URL

logger = logging.getLogger("auto_healing.reporter")


def format_report_html(
    diag: dict[str, Any],
    remediations: list[dict[str, Any]],
    audits: list[dict[str, Any]],
) -> str:
    """Formats findings and actions into a clean HTML message for Telegram."""
    host = diag.get("host", {})
    disk = host.get("disk", {})
    mem = host.get("memory", {})

    status_emoji = "🟢"
    if diag.get("unhealthy_containers") or diag.get("degraded_containers") or host.get("issues"):
        status_emoji = "🟡" if not any(a.get("verdict") == "CRITICAL_REGRESSION" for a in audits) else "🔴"

    lines = [
        f"{status_emoji} <b>[Homelab Auto-Healing Report]</b>",
        f"<i>Fecha: {diag.get('timestamp')[:19].replace('T', ' ')} UTC</i>",
        "",
        "💻 <b>Host Health:</b>",
        f"• <b>Disco (/):</b> {disk.get('used_pct')}% ({disk.get('free_gb')} GB libres) [{disk.get('status')}]",
        f"• <b>RAM:</b> {mem.get('available_mb')} MB disponibles / {mem.get('total_mb')} MB",
        f"• <b>Swap:</b> {mem.get('swap_used_mb')} MB en uso",
        f"• <b>Contenedores:</b> {diag.get('containers_running')}/{diag.get('containers_total')} activos",
    ]

    # Host issues if any
    if host.get("issues"):
        lines.append("")
        lines.append("⚠️ <b>Alertas de Host:</b>")
        for iss in host.get("issues"):
            lines.append(f"  • {html.escape(iss)}")

    # Containers degraded
    if diag.get("unhealthy_containers") or diag.get("degraded_containers"):
        lines.append("")
        lines.append("🐳 <b>Contenedores Afectados:</b>")
        for u in diag.get("unhealthy_containers", []):
            lines.append(f"  • ⚠️ Unhealthy: <code>{html.escape(u)}</code>")
        for d in diag.get("degraded_containers", []):
            lines.append(f"  • ❌ Degraded: <code>{html.escape(d)}</code>")

    # Remediations & Audits
    lines.append("")
    if remediations:
        lines.append("🛠️ <b>Acciones de Auto-Healing & Auditoría:</b>")
        for i, rem in enumerate(remediations):
            aud = audits[i] if i < len(audits) else {}
            verdict = aud.get("verdict", "N/A")
            v_emoji = "✅" if aud.get("improved") else "❌"
            lines.append(
                f"• {v_emoji} <b>{html.escape(rem.get('action'))}</b> sobre <code>{html.escape(rem.get('target'))}</code>"
            )
            lines.append(f"   <i>Veredicto: {verdict} - {html.escape(aud.get('details', ''))}</i>")
    else:
        lines.append("🛠️ <b>Acciones de Auto-Healing:</b> Ninguna requerida (servicios estables).")

    # Log anomalies summary
    anomalies = diag.get("log_anomalies", [])
    if anomalies:
        lines.append("")
        lines.append("📋 <b>Anomalías de Logs (24h):</b>")
        for an in anomalies[:3]:
            lines.append(
                f"• <code>{html.escape(an.get('container'))}</code>: {an.get('error_count')} errores detectados."
            )

    return "\n".join(lines)


def send_telegram_digest(html_text: str) -> bool:
    """Sends HTML notification via centralized Orchestrator notify API."""
    url = f"{ORCHESTRATOR_API_URL}/api/notify"
    payload = {
        "user_id": NOTIFY_USER_ID,
        "agent_id": "auto-healer",
        "message": html_text,
        "parse_mode": "HTML",
        "raw": True,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            if response.status == 200 and (res_json.get("status") == "success" or res_json.get("ok")):
                logger.info("✅ Telegram digest sent successfully via Orchestrator")
                return True
            else:
                logger.error(f"Orchestrator API responded with error: {res_body}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message via Orchestrator: {e}")
        return False
