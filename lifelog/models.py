"""LLM adapter. The nightly path may only ever use a LOCAL model (D-008).

`assert_local` is the enforcement point: if anyone — the owner, an agent, a
future contributor — wires a cloud model into the automatic path, this raises
instead of quietly shipping a diary somewhere.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Optional

from . import config


class ModelUnavailable(Exception):
    pass


class NotLocalModel(Exception):
    pass


class OllamaModel(object):
    local = True

    def __init__(self, model: Optional[str] = None):
        self.id = "ollama/" + (model or config.MODEL)
        self.model = model or config.MODEL

    def health(self) -> bool:
        try:
            urllib.request.urlopen(config.OLLAMA_BASE + "/api/tags", timeout=5)
            return True
        except Exception:                                  # noqa: BLE001
            return False

    def wake(self, attempts: int = 6, delay: float = 2.0) -> bool:
        """Ollama.app may be asleep at 00:01. Poll, with an actual sleep between
        tries — the original implementation spun 10 times in ~0ms and gave up."""
        if self.health():
            return True
        for _ in range(attempts):
            time.sleep(delay)
            if self.health():
                return True
        return False

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        payload = json.dumps({
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": temperature},
        }).encode("utf-8")
        request = urllib.request.Request(
            config.OLLAMA_BASE + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=config.MODEL_TIMEOUT_S) as response:
            body = json.load(response)
        return (body.get("message") or {}).get("content", "").strip()


def assert_local(model) -> None:
    if not getattr(model, "local", False):
        raise NotLocalModel(
            "the nightly path may only use local models; {} is remote".format(
                getattr(model, "id", model)))


def get_model(name: Optional[str] = None):
    return OllamaModel(name)


def complete_with_retries(model, system: str, user: str, attempts: Optional[int] = None):
    """Returns (text, attempts_used, error). Never raises — a failed night must
    still produce an entry, flagged as partial."""
    assert_local(model)
    attempts = attempts or config.MODEL_ATTEMPTS
    last_error = None
    if not model.wake():
        return None, 0, "ollama unreachable at {}".format(config.OLLAMA_BASE)
    for attempt in range(1, attempts + 1):
        try:
            text = model.complete(system, user)
            if text:
                return text, attempt, None
            last_error = "empty response"
        except Exception as exc:                           # noqa: BLE001
            last_error = "{}: {}".format(type(exc).__name__, exc)
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    return None, attempts, last_error
