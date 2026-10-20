"""LLM layer (provider switching via LiteLLM, with a fake completion), prompt + citations, settings."""

from types import SimpleNamespace

import pytest

from app import config, generation, llm
from app.retrieval import Hit


class FakeCompletion:
    """Stands in for litellm.completion; records every call."""

    def __init__(self, reply="The answer [1] and more [2]. Not [9]."):
        self.calls, self.reply = [], reply

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])
                        for piece in [self.reply[:10], self.reply[10:]])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))],
                               usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5))


@pytest.fixture
def fake(monkeypatch):
    f = FakeCompletion()
    monkeypatch.setattr(llm, "COMPLETION", f)
    return f


def test_switching_provider_is_only_a_setting(fake):
    config.save({"DEEPSEEK_API_KEY": "sk-deepseek-123456", "GROQ_API_KEY": "gsk-groq-123456"})
    llm.complete([{"role": "user", "content": "hi"}])
    config.save({"LLM_PROVIDER": "groq"})
    llm.complete([{"role": "user", "content": "hi"}])
    config.save({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "gem-123456789", "LLM_MODEL": "gemini-2.5-pro"})
    llm.complete([{"role": "user", "content": "hi"}])
    assert [(c["model"], c["api_key"]) for c in fake.calls] == [
        ("deepseek/deepseek-chat", "sk-deepseek-123456"),
        ("groq/llama-3.3-70b-versatile", "gsk-groq-123456"),
        ("gemini/gemini-2.5-pro", "gem-123456789")]


def test_missing_key_is_a_clear_error(fake):
    config.save({"LLM_PROVIDER": "openai"})
    with pytest.raises(config.ConfigError, match="No OpenAI API key yet"):
        llm.target()
    assert fake.calls == []


def test_stream_yields_text(fake):
    config.save({"DEEPSEEK_API_KEY": "sk-deepseek-123456"})
    assert "".join(llm.stream([{"role": "user", "content": "x"}])) == fake.reply


def test_check_key_reports_rejection(monkeypatch):
    def reject(**kwargs):
        raise type("AuthenticationError", (Exception,), {})("401 invalid api key")
    monkeypatch.setattr(llm, "COMPLETION", reject)
    ok, message = llm.check_key("deepseek", "sk-wrong-000000")
    assert not ok and "rejected the key" in message and "sk-wrong" not in message


def _hits(n=3):
    return [Hit(id=str(i), path=f"src/m{i}.py", start_line=i, end_line=i + 4, kind="function", symbol=f"f{i}",
                language="python", text=f"def f{i}():\n    return {i}") for i in range(1, n + 1)]


def test_prompt_numbers_excerpts_with_paths_and_lines():
    messages = generation.build_messages("demo", "what does f2 return?", _hits(),
                                         history=[{"question": "earlier?", "answer": "earlier answer"}])
    assert "demo" in messages[0]["content"] and "Cite" in messages[0]["content"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    user = messages[-1]["content"]
    assert "[2] src/m2.py:2-6 (f2)" in user and "```python" in user and "def f2():" in user


def test_citations_keep_only_valid_numbers():
    assert generation.cited("See [2], then [1][2] and [7].", 3) == [2, 1]


def test_short_follow_up_borrows_the_previous_question():
    history = [{"question": "How are retries handled?", "answer": "..."}]
    assert generation.search_query("and tests?", history) == "How are retries handled? and tests?"
    long_q = "Where is the configuration for the database connection pool defined?"
    assert generation.search_query(long_q, history) == long_q


def test_settings_validation_and_secrets_are_masked():
    with pytest.raises(config.ConfigError, match="between"):
        config.save({"TOP_K": "99"})
    with pytest.raises(config.ConfigError, match="one of"):
        config.save({"LLM_PROVIDER": "bard"})
    with pytest.raises(config.ConfigError, match="invalid characters"):
        config.save({"LLM_MODEL": "x\nEVIL=1"})
    config.save({"TOP_K": "8", "DEEPSEEK_API_KEY": "sk-abcdefghijkl1234"})
    assert config.get("TOP_K") == 8
    view = {s["key"]: s for s in config.public_view()}
    assert view["DEEPSEEK_API_KEY"] == {**view["DEEPSEEK_API_KEY"], "is_set": True, "hint": "…1234"}
    assert "value" not in view["DEEPSEEK_API_KEY"] and "sk-abcdefghijkl1234" not in str(view)
    assert "TOP_K=8" in config.ENV_PATH.read_text()


def test_empty_secret_keeps_the_saved_one_and_remove_deletes_it():
    config.save({"GROQ_API_KEY": "gsk-1234567890"})
    config.save({"GROQ_API_KEY": ""})
    assert config.get("GROQ_API_KEY") == "gsk-1234567890"
    config.save({}, remove=["GROQ_API_KEY"])
    assert config.get("GROQ_API_KEY") is None
