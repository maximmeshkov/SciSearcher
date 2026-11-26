"""Bounded GET requests to public HTTP(S) destinations."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit

_TLS = ssl.create_default_context()
_REDIRECTS = {301, 302, 303, 307, 308}


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _open_public(host: str, port: int):
    """Connect to a DNS result that was checked, preserving the original TLS name."""
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or any(not _public_address(answer[4][0]) for answer in answers):
        raise ValueError(f"Адрес {host} не является публичным")
    last_error = None
    for family, socktype, protocol, _, address in answers:
        client = socket.socket(family, socktype, protocol)
        try:
            client.settimeout(10)
            client.connect(address)
            return client
        except OSError as error:
            client.close()
            last_error = error
    raise OSError(f"Не удалось подключиться к {host}") from last_error


def get_bytes(url: str, limit: int, *, accept: str = "*/*") -> bytes:
    """Read a public HTTP(S) response with bounded size and redirect checks."""
    if limit < 1:
        raise ValueError("Ограничение размера должно быть положительным")
    for _ in range(6):
        if not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError("Некорректный URL")
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or "%" in parts.hostname
        ):
            raise ValueError("Для загрузки нужен публичный HTTP(S) URL без учётных данных")
        host = parts.hostname.encode("idna").decode("ascii")
        secure = parts.scheme == "https"
        port = 443 if secure else 80
        if parts.port not in (None, port):
            raise ValueError("Разрешены стандартные порты HTTP и HTTPS")

        connection = http.client.HTTPConnection(host, port, timeout=10)
        raw_socket = None
        try:
            raw_socket = _open_public(host, port)
            connection.sock = _TLS.wrap_socket(raw_socket, server_hostname=host) if secure else raw_socket
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            target = quote(target, safe="/%:+?=&!$'()*,;@[]~")
            connection.request("GET", target, headers={
                "Accept": accept, "Accept-Encoding": "identity",
                "User-Agent": "LiteratureIndexer/1.0", "Connection": "close",
            })
            with connection.getresponse() as response:
                if response.status in _REDIRECTS:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError(f"{host}: перенаправление без адреса")
                    next_url = urljoin(url, location)
                    if secure and urlsplit(next_url).scheme != "https":
                        raise ValueError(f"{host}: перенаправление с HTTPS на HTTP")
                    url = next_url
                    continue
                if response.status != 200:
                    raise ValueError(f"{host}: HTTP {response.status}")
                length = response.getheader("Content-Length")
                if length and length.isdecimal() and int(length) > limit:
                    raise ValueError(f"{host}: ответ превышает {limit} байт")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError(f"{host}: неподдерживаемый Content-Encoding")

                body = bytearray()
                deadline = time.monotonic() + 45
                while len(body) <= limit:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{host}: превышено время чтения ответа")
                    block = response.read1(min(65536, limit + 1 - len(body)))
                    if not block:
                        break
                    body.extend(block)
                if len(body) > limit:
                    raise ValueError(f"{host}: ответ превышает {limit} байт")
                return bytes(body)
        except (OSError, http.client.HTTPException) as error:
            raise ValueError(f"{host}: {type(error).__name__} при получении ответа") from error
        finally:
            connection.close()
            if raw_socket is not None:
                raw_socket.close()
    raise ValueError("Слишком много перенаправлений")
