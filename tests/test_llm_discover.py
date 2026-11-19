import json
from jobbot.llm import LLMChain


class R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._b


class S:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        m = kw["json"]["model"]
        self.calls.append(m)
        if m == "old":
            return R(404, {"error": "gone"})
        return R(200, {"choices": [{"message": {"content": '{"ok": 1}'}}]})

    def get(self, url, **kw):
        return R(200, {"data": [{"id": "whisper-large"}, {"id": "llama-3.1-8b-instant"}, {"id": "llama-3.3-70b-versatile"}]})


def test_recovers_from_retired_model(monkeypatch):
    monkeypatch.setenv("K", "x")
    s = S()
    c = LLMChain([{"name": "groq", "model": "old", "key_env": "K"}], session=s, sleep=lambda x: None)
    data, name = c.complete_json("s", "u")
    assert data == {"ok": 1} and s.calls[-1] == "llama-3.3-70b-versatile"
    c.complete_json("s", "u")
    assert s.calls[-1] == "llama-3.3-70b-versatile" and s.calls.count("old") == 1
