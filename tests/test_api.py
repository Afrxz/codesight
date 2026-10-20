"""The HTTP API end to end (fake models, in-memory Qdrant, fake LLM)."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, llm
from app.api import app

H = {"X-RAGG": "1"}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def ready_repo(client, sample_repo):
    r = client.post("/api/repos", json={"source": str(sample_repo), "name": "sample"}, headers=H)
    assert r.status_code == 200
    repo_id, job_id = r.json()["repo"]["id"], r.json()["job"]["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["phase"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["phase"] == "done", job
    return repo_id


def test_state_changing_requests_need_the_header(client, sample_repo):
    assert client.post("/api/repos", json={"source": str(sample_repo)}).status_code == 403
    assert client.post("/api/settings", json={"values": {}}).status_code == 403


def test_add_index_list_and_remove(client, ready_repo):
    repos = client.get("/api/repos").json()
    assert [r["id"] for r in repos] == [ready_repo] and repos[0]["stats"]["files"] == 4
    assert "files" not in repos[0]                                     # the hash table stays server-side
    assert client.delete(f"/api/repos/{ready_repo}", headers=H).json() == {"ok": True}
    assert client.get("/api/repos").json() == []


def test_bad_source_is_a_clear_400(client, tmp_path):
    r = client.post("/api/repos", json={"source": str(tmp_path / "nope")}, headers=H)
    assert r.status_code == 400 and "Folder not found" in r.json()["detail"]


def test_file_endpoint_serves_only_indexed_files(client, ready_repo):
    ok = client.get(f"/api/repos/{ready_repo}/file", params={"path": "pkg/net.py", "start": 7, "end": 8}).json()
    assert ok["lines"][0].startswith("def fetch_with_retry") and ok["start"] == 7 and ok["total_lines"] > 20
    for bad in ("../../etc/passwd", "secret/keys.py", "debug.log", "pkg/../secret/keys.py"):
        assert client.get(f"/api/repos/{ready_repo}/file", params={"path": bad}).status_code == 404


def test_ask_streams_retrieval_then_tokens_then_done(client, ready_repo, monkeypatch):
    config.save({"DEEPSEEK_API_KEY": "sk-test-1234567"})
    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return iter(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=t))])
                    for t in ["`fetch_with_retry` retries ", "with backoff [1]."])
    monkeypatch.setattr(llm, "COMPLETION", fake_completion)
    r = client.post("/api/ask", json={"repo_id": ready_repo, "question": "how are downloads retried with backoff?"},
                    headers=H)
    events = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["retrieval", "token", "token", "done"]
    assert "fetch_with_retry" in events[0]["hits"][0]["symbol"]
    assert events[-1]["answer"] == "`fetch_with_retry` retries with backoff [1]." and events[-1]["cited"] == [1]
    assert seen["model"] == "deepseek/deepseek-chat" and "pkg/net.py" in seen["messages"][-1]["content"]


def test_ask_without_a_key_explains(client, ready_repo):
    r = client.post("/api/ask", json={"repo_id": ready_repo, "question": "anything"}, headers=H)
    events = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    assert events == [{"type": "error", "message": "No DeepSeek API key yet. Add it in Settings → API keys."}]


def test_settings_round_trip(client):
    data = client.get("/api/settings").json()
    assert {p["id"] for p in data["providers"]} == {"deepseek", "groq", "openai", "gemini"}
    r = client.post("/api/settings", json={"values": {"TOP_K": "9", "RERANK": "off"}}, headers=H)
    values = {s["key"]: s.get("value") for s in r.json()["settings"]}
    assert values["TOP_K"] == 9 and values["RERANK"] == "off"
    assert client.post("/api/settings", json={"values": {"TOP_K": "0"}}, headers=H).status_code == 400


def test_health_and_ui(client):
    assert client.get("/api/health").json()["provider"] == "deepseek"
    page = client.get("/")
    assert page.status_code == 200 and "RAGG" in page.text
    assert client.get("/static/app.js").status_code == 200
