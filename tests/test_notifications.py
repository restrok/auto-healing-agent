import json
from unittest.mock import MagicMock, patch

from auto_healing.hitl import create_approval_plan, submit_hitl_approval_plan
from auto_healing.reporter import send_telegram_digest


def test_send_telegram_digest_success():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({"status": "success", "telegram_message_id": "12345"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = send_telegram_digest("<b>Test Digest</b>")
        assert result is True

        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/notify")
        assert req.headers["Content-type"] == "application/json"
        body = json.loads(req.data.decode("utf-8"))
        assert body["agent_id"] == "auto-healer"
        assert body["message"] == "<b>Test Digest</b>"
        assert body["parse_mode"] == "HTML"
        assert body["raw"] is True


def test_send_telegram_digest_error():
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.read.return_value = json.dumps({"status": "error"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = send_telegram_digest("<b>Test Digest</b>")
        assert result is False


def test_submit_hitl_approval_plan():
    mock_resp_plans = MagicMock()
    mock_resp_plans.status = 200

    mock_resp_notify = MagicMock()
    mock_resp_notify.status = 200
    mock_resp_notify.read.return_value = json.dumps({"status": "success", "telegram_message_id": "999"}).encode("utf-8")

    responses = [mock_resp_plans, mock_resp_notify]

    def mock_urlopen_side_effect(req, *args, **kwargs):
        resp = responses.pop(0)
        resp.__enter__.return_value = resp
        return resp

    diagnosis = {
        "title": "Unhealthy container test",
        "root_cause": "OOM killed",
        "recommended_fix": "Increase memory limits",
        "worker_task": "update compose memory",
        "target_project": "homelab",
    }

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_side_effect) as mock_urlopen:
        plan_id = submit_hitl_approval_plan(diagnosis, "unhealthy_container")
        assert plan_id.startswith("heal_")
        assert mock_urlopen.call_count == 2

        # 1st call: /api/plans
        req1 = mock_urlopen.call_args_list[0][0][0]
        assert req1.full_url.endswith("/api/plans")
        body1 = json.loads(req1.data.decode("utf-8"))
        assert body1["plan_id"] == plan_id
        assert body1["requester_id"] == "test-user"

        # 2nd call: /api/notify
        req2 = mock_urlopen.call_args_list[1][0][0]
        assert req2.full_url.endswith("/api/notify")
        body2 = json.loads(req2.data.decode("utf-8"))
        assert body2["agent_id"] == "auto-healer"
        assert body2["raw"] is True
        assert body2["parse_mode"] == "HTML"
        assert "inline_keyboard" in body2
        kb = body2["inline_keyboard"]
        assert kb[0][0]["callback_data"] == f"approve_plan:{plan_id}"
        assert kb[0][1]["callback_data"] == f"reject_plan:{plan_id}"


def test_create_approval_plan_alias():
    assert create_approval_plan is submit_hitl_approval_plan
