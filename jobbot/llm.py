"""Free-tier LLM chain (Gemini, Groq, OpenRouter) with rotation and fallback.

Calls rotate between providers so no single free quota is exhausted, a provider
that errors or is rate-limited is put on a short cool-down, and if every
provider fails the caller gets LLMError (the resume step then falls back to
offline tailoring, so a failure never blocks the daily run).
API keys are read from the environment (.env) and sent only in request headers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from typing import Optional

import requests

log = logging.getLogger("jobbot.llm")

DEFAULT_PROVIDERS = [
    {"name": "gemini", "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY"},
    {"name": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"name": "openrouter", "model": "meta-llama/llama-3.3-70b-instruct:free",
     "key_env": "OPENROUTER_API_KEY"},
]
_OPENAI_BASE = {"groq": "https://api.groq.com/openai/v1",
                "openrouter": "https://openrouter.ai/api/v1"}


class LLMError(Exception):
    pass


def extract_json(text: str) -> dict:
    """Parse a JSON object out of model output (handles code fences and chatter)."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        data = json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("no JSON object in model output")
        data = json.loads(t[a:b + 1])
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")
    return data


class LLMChain:
    def __init__(self, providers: Optional[list[dict]] = None, *, session=None,
                 min_interval: float = 4.0, sleep=time.sleep, clock=time.monotonic):
        self.session = session or requests.Session()
        self.min_interval, self.sleep, self.clock = min_interval, sleep, clock
        self.providers = [p for p in (providers or DEFAULT_PROVIDERS)
                          if p.get("name") in ("gemini", *_OPENAI_BASE) and self._key(p)]
        self._rr = 0
        self._cool: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self.usage: Counter = Counter()

    @staticmethod
    def _key(p: dict) -> str:
        return os.environ.get(p.get("key_env", ""), "").strip()

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def complete_json(self, system: str, user: str) -> tuple[dict, str]:
        if not self.providers:
            raise LLMError("no LLM API keys found in .env")
        n, errors = len(self.providers), []
        order = [self.providers[(self._rr + i) % n] for i in range(n)]
        for p in order:
            name = p["name"]
            if self.clock() < self._cool.get(name, 0):
                errors.append(f"{name}: cooling down")
                continue
            self._throttle(name)
            try:
                data = extract_json(self._call(p, system, user))
            except Exception as exc:  # noqa: BLE001 - any failure -> next provider
                self._cool[name] = self.clock() + (300 if re.search(r"429|timed out|Timeout", str(exc)) else 20)
                errors.append(f"{name}: {str(exc)[:140]}")
                log.warning("LLM provider %s failed: %s", name, str(exc)[:140])
                continue
            self.usage[name] += 1
            self._rr = (self.providers.index(p) + 1) % n
            return data, name
        raise LLMError("; ".join(errors))

    def _throttle(self, name: str) -> None:
        wait = self._last.get(name, -1e9) + self.min_interval - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last[name] = self.clock()

    # ---- model auto-discovery: configured names get retired; ask the provider what exists
    _PREFER = {
        "gemini": [r"^gemini-flash-latest$", r"gemini-3.*flash(?!.*(lite|image|tts|live))",
                   r"gemini-2\.5-flash(?!.*(lite|image|tts|live))", r"gemini-2\.0-flash(?!.*(lite|image|tts|live))",
                   r"flash(?!.*(lite|image|tts|live|thinking|exp))"],
        "groq": [r"llama-3\.3-70b", r"gpt-oss-120b", r"gpt-oss-20b", r"llama-3\.1-8b-instant", r"llama|qwen|gemma"],
        "openrouter": [r"llama-3\.3-70b.*:free", r"gpt-oss-120b:free", r"gpt-oss-20b:free", r"gemma.*:free",
                       r"qwen.*:free", r":free$"],
    }

    def _discover(self, p: dict) -> list:
        name, key = p["name"], self._key(p)
        try:
            if name == "gemini":
                r = self.session.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": key}, timeout=(10, 30))
                ids = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                       if "generateContent" in m.get("supportedGenerationMethods", [])]
            else:
                r = self.session.get(_OPENAI_BASE[name] + "/models", headers={"Authorization": f"Bearer {key}"},
                                     timeout=(10, 30))
                ids = [m["id"] for m in r.json().get("data", [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list %s models: %s", name, str(exc)[:100])
            return []
        ids = [i for i in ids if not re.search(r"whisper|guard|tts|embed|image|vision|audio|orpheus|safeguard", i, re.I)]
        out = []
        for rx in self._PREFER[name]:
            out += sorted((i for i in ids if re.search(rx, i, re.I) and i not in out), reverse=True)
        return out[:4]

    def _call(self, p: dict, system: str, user: str) -> str:
        last, tried = None, []
        while True:
            model = next((m for m in [p["model"]] + p.get("_alts", []) if m not in tried), None)
            if model is None:
                raise last
            tried.append(model)
            try:
                out = self._call_model(p, model, system, user)
                if model != p["model"]:
                    log.info("LLM %s: using model %s", p["name"], model)
                    p["model"] = model
                return out
            except LLMError as exc:
                last = exc
                if not re.search(r"HTTP (404|400)", str(exc)):
                    raise
                if "_alts" not in p:                  # first model failure: find what exists now
                    p["_alts"] = [m for m in self._discover(p) if m != p["model"]]

    def _call_model(self, p: dict, model: str, system: str, user: str) -> str:
        name, key = p["name"], self._key(p)
        if name == "gemini":
            r = self.session.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"temperature": 0.3,
                                           "responseMimeType": "application/json"}},
                timeout=(10, 45))
            self._check(r)
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(x.get("text", "") for x in parts)
        body = {"model": model, "temperature": 0.3,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if name == "groq":
            body["response_format"] = {"type": "json_object"}
        r = self.session.post(_OPENAI_BASE[name] + "/chat/completions",
                              headers={"Authorization": f"Bearer {key}"}, json=body,
                              timeout=(10, 45))
        self._check(r)
        return r.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _check(r) -> None:
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:120]}")


def make_llm(cfg: dict) -> LLMChain:
    c = cfg.get("llm", {}) or {}
    return LLMChain(c.get("providers") or None, min_interval=float(c.get("min_interval_seconds", 4)))
