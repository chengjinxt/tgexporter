from __future__ import annotations

import errno
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, TypeVar


T = TypeVar("T")
RetryDecider = Callable[[BaseException], bool]


@dataclass(frozen=True)
class _NetworkRoute:
    name: str
    opener: urllib.request.OpenerDirector


class AdaptiveOpener:
    """Prefer direct access and fall back to configured or system proxies."""

    def __init__(self, proxy_url: str | None = None) -> None:
        configured_proxy = (proxy_url or "").strip()
        system_proxies = {
            scheme: value
            for scheme, value in urllib.request.getproxies().items()
            if scheme in {"http", "https"} and value
        }

        routes = [
            _NetworkRoute(
                "direct",
                urllib.request.build_opener(urllib.request.ProxyHandler({})),
            )
        ]
        proxy_signatures: set[tuple[tuple[str, str], ...]] = set()

        if configured_proxy:
            configured_proxies = {"http": configured_proxy, "https": configured_proxy}
            routes.append(
                _NetworkRoute(
                    "configured proxy",
                    urllib.request.build_opener(urllib.request.ProxyHandler(configured_proxies)),
                )
            )
            proxy_signatures.add(_proxy_signature(configured_proxies))

        if system_proxies and _proxy_signature(system_proxies) not in proxy_signatures:
            routes.append(
                _NetworkRoute(
                    "system proxy",
                    urllib.request.build_opener(urllib.request.ProxyHandler(system_proxies)),
                )
            )

        self._routes = routes
        self._active_route = routes[0]
        self._verified = False
        self.last_failed_routes: tuple[str, ...] = ()

    @property
    def connection_mode(self) -> str:
        return self._active_route.name

    @property
    def route_count(self) -> int:
        return len(self._routes)

    @property
    def verified(self) -> bool:
        return self._verified

    def open(self, request: Any, timeout: float | None = None):
        safe_to_retry = getattr(request, "data", None) is None
        return self.execute(
            lambda opener: opener.open(request, timeout=timeout),
            retry_decider=(
                (lambda _exc: True)
                if safe_to_retry
                else is_definite_connection_failure
            ),
        )

    def execute(self, operation: Callable[[urllib.request.OpenerDirector], T], retry_decider: RetryDecider) -> T:
        failed_routes: list[str] = []
        routes = self._ordered_routes()
        for index, route in enumerate(routes):
            try:
                result = operation(route.opener)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                failed_routes.append(route.name)
                has_fallback = index + 1 < len(routes)
                if not has_fallback or not retry_decider(exc):
                    self.last_failed_routes = tuple(failed_routes)
                    raise
            else:
                self._active_route = route
                self._verified = True
                self.last_failed_routes = tuple(failed_routes)
                return result

        raise RuntimeError("No network route is available.")

    def _ordered_routes(self) -> list[_NetworkRoute]:
        return [self._active_route, *(route for route in self._routes if route is not self._active_route)]


def is_definite_connection_failure(exc: BaseException) -> bool:
    current: BaseException | object = exc
    visited: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (ConnectionRefusedError, socket.gaierror)):
            return True
        if isinstance(current, OSError):
            if current.errno in {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}:
                return True
            if getattr(current, "winerror", None) in {10061, 10064, 10065}:
                return True
        if isinstance(current, urllib.error.URLError):
            current = current.reason
            continue
        break

    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "connection refused",
            "getaddrinfo failed",
            "name or service not known",
            "network is unreachable",
            "no route to host",
            "winerror 10061",
        )
    )


def _proxy_signature(proxies: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((scheme, value.rstrip("/")) for scheme, value in proxies.items()))
