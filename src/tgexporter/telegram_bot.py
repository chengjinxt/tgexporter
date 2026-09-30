from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .network import AdaptiveOpener, is_definite_connection_failure


class TelegramBotError(RuntimeError):
    pass


def normalize_chat_id(chat_id: str | int) -> str:
    value = str(chat_id).strip()
    if not value:
        raise ValueError("Telegram chat ID is required.")
    if value.startswith("@") or value.lstrip("-").isdigit():
        return value
    return f"@{value}"


def telegram_text_length(text: str) -> int:
    visible_text = re.sub(r"<[^>]+>", "", text)
    return len(html.unescape(visible_text))


class TelegramBotClient:
    def __init__(self, token: str, proxy_url: str | None = None) -> None:
        if not token:
            raise ValueError("Telegram bot token is required.")
        self.token = token
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.opener = AdaptiveOpener(proxy_url)

    @property
    def connection_mode(self) -> str:
        if isinstance(self.opener, AdaptiveOpener):
            return self.opener.connection_mode
        return "custom"

    def get_me(self) -> dict[str, Any]:
        return self._request_json("getMe", timeout=10)

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_connection_route()
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = json.dumps(allowed_updates, ensure_ascii=False)
        request_timeout = max(15, timeout + 15)
        result = self._request_json("getUpdates", params, timeout=request_timeout)
        if not isinstance(result, list):
            raise TelegramBotError("Unexpected getUpdates response.")
        return result

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self._request_json("getFile", {"file_id": file_id})

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
    ) -> dict[str, Any]:
        if telegram_text_length(text) > 4096:
            raise ValueError("Telegram text messages cannot exceed 4096 characters.")
        result = self._request_json(
            "sendMessage",
            {
                "chat_id": normalize_chat_id(chat_id),
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": str(disable_web_page_preview).lower(),
            },
        )
        if not isinstance(result, dict):
            raise TelegramBotError("Unexpected sendMessage response.")
        return result

    def send_article(self, chat_id: str, text: str, media: Iterable[Any]) -> dict[str, Any]:
        assets = list(media)
        image = next((asset for asset in assets if asset.kind == "image" and asset.path.exists()), None)
        video = next((asset for asset in assets if asset.kind == "video" and asset.path.exists()), None)
        media_errors: list[str] = []

        if video is not None:
            try:
                if telegram_text_length(text) <= 1024:
                    result = self.send_video(chat_id, video.path, caption=text)
                else:
                    self.send_video(chat_id, video.path)
                    result = self.send_message(chat_id, text)
                result = dict(result)
                result["media_errors"] = media_errors
                return result
            except TelegramBotError as exc:
                media_errors.append(str(exc))

        if image is not None and telegram_text_length(text) <= 1024:
            result = self.send_photo(chat_id, image.path, caption=text)
        elif image is not None:
            self.send_photo(chat_id, image.path)
            result = self.send_message(chat_id, text)
        else:
            result = self.send_message(chat_id, text)

        result = dict(result)
        result["media_errors"] = media_errors
        return result

    def send_photo(self, chat_id: str, path: Path, caption: str = "") -> dict[str, Any]:
        if telegram_text_length(caption) > 1024:
            raise ValueError("Telegram photo captions cannot exceed 1024 characters.")
        result = self._request_multipart(
            "sendPhoto",
            {
                "chat_id": normalize_chat_id(chat_id),
                "caption": caption,
                "parse_mode": "HTML",
                "disable_notification": "false",
            },
            file_field="photo",
            path=path,
        )
        if not isinstance(result, dict):
            raise TelegramBotError("Unexpected sendPhoto response.")
        return result

    def send_video(self, chat_id: str, path: Path, caption: str = "") -> dict[str, Any]:
        if telegram_text_length(caption) > 1024:
            raise ValueError("Telegram video captions cannot exceed 1024 characters.")
        result = self._request_multipart(
            "sendVideo",
            {
                "chat_id": normalize_chat_id(chat_id),
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": "true",
            },
            file_field="video",
            path=path,
        )
        if not isinstance(result, dict):
            raise TelegramBotError("Unexpected sendVideo response.")
        return result

    def download_file(self, file_path: str, destination: Path, attempts: int = 3) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.file_base}/{urllib.parse.quote(file_path, safe='/')}"
        request = urllib.request.Request(url, headers={"User-Agent": "tgexporter/0.1"})
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with self.opener.open(request, timeout=90) as response:
                    with destination.open("wb") as file:
                        shutil.copyfileobj(response, file)
                return
            except OSError as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(attempt)
        raise TelegramBotError(f"Failed to download Telegram file to {destination}: {last_error}") from last_error

    def _request_json(self, method: str, params: dict[str, Any] | None = None, timeout: int = 90) -> Any:
        if method not in {"getMe", "getUpdates", "getFile"}:
            self._ensure_connection_route()
        body = urllib.parse.urlencode(params or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/{method}",
            data=body if params is not None else None,
            headers={"User-Agent": "tgexporter/0.1"},
        )

        def perform(opener):
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            payload = self._execute_request(
                perform,
                allow_ambiguous_retry=method in {"getMe", "getUpdates", "getFile"},
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise TelegramBotError(f"Telegram API {method} failed: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramBotError(f"Telegram API {method} failed: {exc}") from exc

        if not payload.get("ok"):
            description = payload.get("description", "unknown error")
            raise TelegramBotError(f"Telegram API {method} failed: {description}")
        return payload.get("result")

    def _ensure_connection_route(self) -> None:
        if not isinstance(self.opener, AdaptiveOpener):
            return
        if self.opener.verified or self.opener.route_count <= 1:
            return
        self.get_me()

    def _execute_request(self, operation, *, allow_ambiguous_retry: bool):
        if not isinstance(self.opener, AdaptiveOpener):
            return operation(self.opener)
        return self.opener.execute(
            operation,
            retry_decider=(
                (lambda _exc: True)
                if allow_ambiguous_retry
                else is_definite_connection_failure
            ),
        )

    def _request_multipart(
        self,
        method: str,
        params: dict[str, Any],
        *,
        file_field: str,
        path: Path,
        timeout: int = 180,
    ) -> Any:
        self._ensure_connection_route()
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        boundary = f"tgexporter-{uuid4().hex}"
        body = bytearray()
        for name, value in params.items():
            body.extend(f"--{boundary}\r\n".encode("ascii"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        filename = path.name.replace('"', "'").replace("\r", " ").replace("\n", " ")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        body.extend(path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
        request = urllib.request.Request(
            f"{self.api_base}/{method}",
            data=bytes(body),
            headers={
                "User-Agent": "tgexporter/0.1",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

        def perform(opener):
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            payload = self._execute_request(perform, allow_ambiguous_retry=False)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise TelegramBotError(f"Telegram API {method} failed: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramBotError(f"Telegram API {method} failed: {exc}") from exc
        if not payload.get("ok"):
            description = payload.get("description", "unknown error")
            raise TelegramBotError(f"Telegram API {method} failed: {description}")
        return payload.get("result")
