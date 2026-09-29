import pytest


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch):
    """Provides safe mock environment variables for tests."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-telegram-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("OLLAMA_API_KEY", "mock-ollama-api-key")
    monkeypatch.setenv("REQUESTER_ID", "test-user")
    monkeypatch.setenv("ORCHESTRATOR_API_URL", "http://localhost:8001")
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", "./data/orchestrator.db")
