from app import llm


def test_extract_code_closed_fence():
    assert llm._extract_code("hi\n```python\nprint(1)\n```\nbye") == "print(1)"


def test_extract_code_truncated_fence():
    # reply cut off by max_tokens: opening fence, no closing fence
    assert llm._extract_code("```python\nimport pandas as pd\nprint(2)") == "import pandas as pd\nprint(2)"


def test_extract_code_empty():
    assert llm._extract_code("") == ""


def test_write_code_retries_on_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "mode", lambda: "vultr")

    def fake_chat(messages, max_tokens=1200, temperature=0.1):
        calls.append(max_tokens)
        return "" if len(calls) == 1 else "```python\nprint('ok')\n```"

    monkeypatch.setattr(llm, "_chat", fake_chat)
    assert llm.write_code("t", "f", []) == "print('ok')"
    assert calls == [4000, 4000]
