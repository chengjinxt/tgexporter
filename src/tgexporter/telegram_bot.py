from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class TelegramBotError(RuntimeError):
    pass


class TelegramBotClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("Telegram bot token is required.")
        self.token = token
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def get_me(self) -> dict[str, Any]:
        return self._request_json("getMe")

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = json.dumps(allowed_updates, ensure_ascii=False)
        result = self._request_json("getUpdates", params)
        if not isinstance(result, list):
            raise TelegramBotError("Unexpected getUpdates response.")
        return result

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self._request_json("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.file_base}/{urllib.parse.quote(file_path, safe='/')}"
        request = urllib.request.Request(url, headers={"User-Agent": "tgexporter/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                with destination.open("wb") as file:
                    shutil.copyfileobj(response, file)
        except urllib.error.URLError as exc:
            raise TelegramBotError(f"Failed to download Telegram file to {destination}.") from exc

    def _request_json(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body = urllib.parse.urlencode(params or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/{method}",
            data=body if params is not None else None,
            headers={"User-Agent": "tgexporter/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise TelegramBotError(f"Telegram API {method} failed: {detail}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise TelegramBotError(f"Telegram API {method} failed.") from exc

        if not payload.get("ok"):
            description = payload.get("description", "unknown error")
            raise TelegramBotError(f"Telegram API {method} failed: {description}")
        return payload.get("result")
