import pytest

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
