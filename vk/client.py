"""VK client protocol, network-free mock, and standard-library production sender."""

import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .exceptions import PermanentVKError, TemporaryVKError


class VKClientProtocol(Protocol):
    def send_message(self, peer_id: int, text: str, keyboard: dict | None = None) -> None: ...

    def answer_callback(self, event_id: str, user_id: int, peer_id: int, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SentMessage:
    peer_id: int
    text: str
    keyboard: dict | None


class MockVKClient:
    def __init__(self):
        self.sent: list[SentMessage] = []
        self.callbacks: list[dict] = []
        self._lock = threading.Lock()

    def send_message(self, peer_id: int, text: str, keyboard: dict | None = None) -> None:
        with self._lock:
            self.sent.append(SentMessage(peer_id, text, keyboard))

    def answer_callback(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        with self._lock:
            self.callbacks.append({"event_id": event_id, "user_id": user_id, "peer_id": peer_id, "text": text})

    @property
    def call_count(self) -> int:
        return len(self.sent)


class RealVKClient:
    """Minimal VK API sender. Construction performs no network request."""

    API_URL = "https://api.vk.com/method/"

    def __init__(self, token: str, api_version: str, timeout: float = 10.0):
        self._token = token
        self.api_version = api_version
        self.timeout = timeout

    def _call(self, method: str, parameters: dict) -> dict:
        body = urllib.parse.urlencode({**parameters, "access_token": self._token, "v": self.api_version}).encode()
        request = urllib.request.Request(self.API_URL + method, data=body)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or exc.code == 429:
                raise TemporaryVKError("Временная HTTP ошибка VK") from exc
            raise PermanentVKError("Постоянная HTTP ошибка VK") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise TemporaryVKError("Временная сетевая ошибка VK") from exc
        if "error" in result:
            code = result["error"].get("error_code")
            error_type = TemporaryVKError if code in {1, 6, 9, 10, 29} else PermanentVKError
            raise error_type(f"VK API error code {code}")
        return result

    def send_message(self, peer_id: int, text: str, keyboard: dict | None = None) -> None:
        parameters = {"peer_id": peer_id, "message": text, "random_id": secrets.randbelow(2**31)}
        if keyboard is not None:
            parameters["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        self._call("messages.send", parameters)

    def answer_callback(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        data = json.dumps({"type": "show_snackbar", "text": text}, ensure_ascii=False)
        self._call("messages.sendMessageEventAnswer", {"event_id": event_id, "user_id": user_id, "peer_id": peer_id, "event_data": data})

    def long_poll_events(self, group_id: int):
        """Yield raw Group Long Poll updates until interrupted."""
        server = self._call("groups.getLongPollServer", {"group_id": group_id})["response"]
        url, key, ts = server["server"], server["key"], server["ts"]
        while True:
            query = urllib.parse.urlencode({"act": "a_check", "key": key, "ts": ts, "wait": 25})
            try:
                with urllib.request.urlopen(f"{url}?{query}", timeout=35) as response:
                    payload = json.loads(response.read())
            except (TimeoutError, urllib.error.URLError) as exc:
                raise TemporaryVKError("Временная ошибка VK Long Poll") from exc
            if "failed" in payload:
                raise TemporaryVKError("VK Long Poll session expired")
            ts = payload["ts"]
            yield from payload.get("updates", [])
