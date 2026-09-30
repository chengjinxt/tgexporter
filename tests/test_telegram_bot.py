import json
import urllib.error

import pytest

import tgexporter.network as network_module
from tgexporter.telegram_bot import TelegramBotClient, TelegramBotError


class TimeoutResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        raise TimeoutError("The read operation timed out")


class TimeoutOpener:
    def __init__(self):
        self.timeout = None

    def open(self, request, timeout=90):
        self.timeout = timeout
        return TimeoutResponse()


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SequenceOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.methods = []

    def open(self, request, timeout=90):
        self.methods.append(request.full_url.rsplit("/", 1)[-1])
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return JsonResponse(result)


def install_route_openers(monkeypatch, *openers, system_proxies=None):
    available = iter(openers)
    monkeypatch.setattr(network_module.urllib.request, "getproxies", lambda: system_proxies or {})
    monkeypatch.setattr(network_module.urllib.request, "build_opener", lambda *_handlers: next(available))


def test_get_updates_wraps_read_timeout_as_telegram_bot_error():
    client = TelegramBotClient("123:test")
    opener = TimeoutOpener()
    client.opener = opener

    with pytest.raises(TelegramBotError, match="read operation timed out"):
        client.get_updates(timeout=30)

    assert opener.timeout == 45


def test_get_updates_socket_timeout_exceeds_long_poll_timeout():
    client = TelegramBotClient("123:test")
    opener = TimeoutOpener()
    client.opener = opener

    with pytest.raises(TelegramBotError):
        client.get_updates(timeout=120)

    assert opener.timeout == 135


def test_direct_connection_works_without_any_proxy(monkeypatch):
    direct = SequenceOpener({"ok": True, "result": [{"update_id": 9}]})
    install_route_openers(monkeypatch, direct)
    client = TelegramBotClient("123:test")

    updates = client.get_updates(timeout=2)

    assert updates == [{"update_id": 9}]
    assert direct.methods == ["getUpdates"]
    assert client.connection_mode == "direct"


def test_configured_proxy_is_not_required_when_direct_access_works(monkeypatch):
    direct = SequenceOpener(
        {"ok": True, "result": {"id": 1, "username": "test_bot"}},
        {"ok": True, "result": [{"update_id": 10}]},
    )
    configured_proxy = SequenceOpener(
        urllib.error.URLError(ConnectionRefusedError(10061, "proxy unavailable"))
    )
    install_route_openers(monkeypatch, direct, configured_proxy)
    client = TelegramBotClient("123:test", proxy_url="http://127.0.0.1:7890")

    updates = client.get_updates(timeout=2)

    assert updates == [{"update_id": 10}]
    assert direct.methods == ["getMe", "getUpdates"]
    assert configured_proxy.methods == []
    assert client.connection_mode == "direct"


def test_configured_proxy_is_used_when_direct_access_fails(monkeypatch):
    direct = SequenceOpener(
        urllib.error.URLError(ConnectionRefusedError(10061, "direct unavailable"))
    )
    configured_proxy = SequenceOpener(
        {"ok": True, "result": {"id": 1, "username": "test_bot"}},
        {"ok": True, "result": [{"update_id": 11}]},
        {"ok": True, "result": [{"update_id": 12}]},
    )
    install_route_openers(monkeypatch, direct, configured_proxy)
    client = TelegramBotClient("123:test", proxy_url="http://127.0.0.1:7890")

    first = client.get_updates(timeout=2)
    second = client.get_updates(timeout=2)

    assert first == [{"update_id": 11}]
    assert second == [{"update_id": 12}]
    assert direct.methods == ["getMe"]
    assert configured_proxy.methods == ["getMe", "getUpdates", "getUpdates"]
    assert client.connection_mode == "configured proxy"


def test_system_proxy_is_used_when_direct_access_fails(monkeypatch):
    direct = SequenceOpener(
        urllib.error.URLError(ConnectionRefusedError(10061, "direct unavailable"))
    )
    system_proxy = SequenceOpener(
        {"ok": True, "result": {"id": 1, "username": "test_bot"}},
        {"ok": True, "result": [{"update_id": 13}]},
    )
    install_route_openers(
        monkeypatch,
        direct,
        system_proxy,
        system_proxies={"http": "http://system-proxy:8080", "https": "http://system-proxy:8080"},
    )
    client = TelegramBotClient("123:test")

    updates = client.get_updates(timeout=2)

    assert updates == [{"update_id": 13}]
    assert direct.methods == ["getMe"]
    assert system_proxy.methods == ["getMe", "getUpdates"]
    assert client.connection_mode == "system proxy"


def test_send_message_selects_a_reachable_route_before_posting(monkeypatch):
    direct = SequenceOpener(
        urllib.error.URLError(ConnectionRefusedError(10061, "direct unavailable"))
    )
    configured_proxy = SequenceOpener(
        {"ok": True, "result": {"id": 1, "username": "test_bot"}},
        {"ok": True, "result": {"message_id": 321}},
    )
    install_route_openers(monkeypatch, direct, configured_proxy)
    client = TelegramBotClient("123:test", proxy_url="http://127.0.0.1:7890")

    result = client.send_message("@DailyTech", "hello")

    assert result["message_id"] == 321
    assert direct.methods == ["getMe"]
    assert configured_proxy.methods == ["getMe", "sendMessage"]
