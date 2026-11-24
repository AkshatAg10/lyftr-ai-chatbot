"""Backend tests. The provider is replaced by LiteLLM's own mock_response, so LiteLLM's real
cache and exception handling run, but no network call and no real key is used."""
import litellm
import pytest
from fastapi.testclient import TestClient
from litellm.caching.caching import Cache

import main

real_completion = litellm.completion


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("LLM_API_KEYS", "fake-key-1,fake-key-2")
    monkeypatch.setenv("ENABLE_FAILURE_SIMULATION", "true")
    monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2.5-flash")
    litellm.cache = Cache(type="local")  # fresh cache per test
    state = {"calls": [], "bad_keys": {}}

    def fake_completion(**kwargs):
        key = kwargs["api_key"]
        if key in state["bad_keys"]:
            raise state["bad_keys"][key]
        last = kwargs["messages"][-1]["content"]
        resp = real_completion(mock_response=f"echo: {last}", mock_delay=0.2, **kwargs)
        if not resp._hidden_params.get("cache_hit"):
            state["calls"].append(key)  # count only real provider calls
        return resp

    monkeypatch.setattr(litellm, "completion", fake_completion)
    state["real"] = real_completion
    return state


@pytest.fixture
def client(env):
    with TestClient(main.app) as c:
        yield c


def chat(client, sid, msg, **kw):
    return client.post("/chat", json={"session_id": sid, "message": msg}, **kw)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


def test_chat_success_contract(client):
    r = chat(client, "s1", "hello")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s1" and body["reply"]
    m = body["metadata"]
    assert m["resolution_source"] == "LIVE_INFERENCE"
    assert m["model"] == "gemini/gemini-2.5-flash"
    assert isinstance(m["latency_ms"], int)
    assert isinstance(m["input_tokens"], int) and isinstance(m["output_tokens"], int)
    assert m["key_id"] == "key_1" and m["fallback_attempts"] == 0
    assert "fake-key" not in r.text


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hi"},
        {"session_id": "s", "message": ""},
        {"session_id": "s", "message": "   "},
        {"session_id": "", "message": "hi"},
        {"session_id": "x" * 129, "message": "hi"},
        {"session_id": "s", "message": "a" * 4001},
        {"session_id": "s"},
    ],
)
def test_validation_422(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_malformed_json_422(client):
    r = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "detail" in r.json()


def test_message_boundaries_ok(client):
    assert chat(client, "s", "a" * 4000).status_code == 200
    assert chat(client, "s2", "  hi  ").status_code == 200


def test_session_history_and_isolation(client):
    chat(client, "A", "first")
    chat(client, "A", "second")
    chat(client, "B", "other")
    a = client.get("/sessions/A/messages").json()
    assert [m["role"] for m in a["messages"]] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in a["messages"] if m["role"] == "user"] == ["first", "second"]
    assert set(a["messages"][1]["metadata"]) >= {"resolution_source", "input_tokens", "output_tokens"}
    assert "metadata" not in a["messages"][0]
    assert all(m["role"] != "system" for m in a["messages"])
    b = client.get("/sessions/B/messages").json()
    assert len(b["messages"]) == 2


def test_unknown_session_404(client):
    r = client.get("/sessions/nope/messages")
    assert r.status_code == 404 and r.json() == {"detail": "session not found"}


def test_history_is_sent_to_llm(client, monkeypatch):
    seen = []

    def spy(**kwargs):
        seen.append(kwargs["messages"])
        return main_real(**kwargs)

    main_real = litellm.completion
    monkeypatch.setattr(litellm, "completion", spy)
    chat(client, "H", "my name is Sam")
    chat(client, "H", "what is my name?")
    roles = [m["role"] for m in seen[-1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert seen[-1][1]["content"] == "my name is Sam"


def test_system_prompt_override_persists_in_session(client, monkeypatch):
    seen = []
    base = litellm.completion

    def spy(**kwargs):
        seen.append(kwargs["messages"][0]["content"])
        return base(**kwargs)

    monkeypatch.setattr(litellm, "completion", spy)
    client.post("/chat", json={"session_id": "P", "message": "a", "system_prompt": "Be a pirate."})
    chat(client, "P", "b")
    assert seen == ["Be a pirate.", "Be a pirate."]


def test_cache_hit_across_sessions(client, env):
    first = chat(client, "c1", "What is the capital of France?").json()
    second = chat(client, "c2", "What is the capital of France?").json()
    assert first["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    m = second["metadata"]
    assert m["resolution_source"] == "LLM_CACHE"
    assert second["reply"] == first["reply"]
    assert m["key_id"] is None and m["fallback_attempts"] == 0
    assert m["input_tokens"] == first["metadata"]["input_tokens"]
    assert m["output_tokens"] == first["metadata"]["output_tokens"]
    assert m["latency_ms"] < first["metadata"]["latency_ms"]
    assert len(env["calls"]) == 1  # provider was called once only


def test_different_prompt_not_cached(client):
    chat(client, "d1", "What is the capital of France?")
    r = chat(client, "d2", "What is the capital of Spain?").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert "Spain" in r["reply"]


def test_failover_on_real_provider_429(client, env):
    env["bad_keys"]["fake-key-1"] = litellm.RateLimitError("quota", "gemini", "m")
    r = chat(client, "f1", "failover please")
    assert r.status_code == 200
    m = r.json()["metadata"]
    assert m["key_id"] == "key_2" and m["fallback_attempts"] == 1
    assert m["resolution_source"] == "LIVE_INFERENCE"


def test_failover_on_real_provider_5xx(client, env):
    env["bad_keys"]["fake-key-1"] = litellm.InternalServerError("boom", "gemini", "m")
    r = chat(client, "f2", "5xx please")
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_2"


def test_all_keys_fail_503_and_no_history(client, env):
    err = litellm.RateLimitError("quota", "gemini", "m")
    env["bad_keys"].update({"fake-key-1": err, "fake-key-2": err})
    r = chat(client, "x1", "everything fails")
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/x1/messages").status_code == 404


@pytest.mark.parametrize("header,key", [("key_1=429", "key_2"), ("key_1=500", "key_2"), ("key_1=502", "key_2"), ("key_1=503", "key_2")])
def test_simulated_failover(client, header, key):
    r = chat(client, "sim-" + header, "simulate " + header, headers={"X-Simulate-Failure": header})
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["key_id"] == key and body["metadata"]["fallback_attempts"] == 1
    assert body["reply"].startswith("echo:")  # real (mocked-provider) answer, not an error text


def test_simulated_failover_ignores_cache(client):
    chat(client, "w1", "warm cache")
    r = chat(client, "w2", "warm cache", headers={"X-Simulate-Failure": "key_1=429"}).json()
    assert r["metadata"]["key_id"] == "key_2" and r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_simulated_all_keys_503(client):
    r = chat(client, "sx", "all down", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/sx/messages").status_code == 404
    r = chat(client, "sy", "both listed", headers={"X-Simulate-Failure": "key_1=429,key_2=500"})
    assert r.status_code == 503


def test_simulation_affects_only_current_request(client):
    chat(client, "o1", "one", headers={"X-Simulate-Failure": "key_1=429"})
    r = chat(client, "o2", "two").json()
    assert r["metadata"]["key_id"] == "key_1" and r["metadata"]["fallback_attempts"] == 0


def test_simulation_disabled_by_switch(client, monkeypatch):
    monkeypatch.setenv("ENABLE_FAILURE_SIMULATION", "false")
    r = chat(client, "off", "hello", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_1"


def test_data_persists_across_restart(env, tmp_path):
    with TestClient(main.app) as c1:
        chat(c1, "persist", "remember me")
    with TestClient(main.app) as c2:  # new startup, same DB file
        assert len(c2.get("/sessions/persist/messages").json()["messages"]) == 2
