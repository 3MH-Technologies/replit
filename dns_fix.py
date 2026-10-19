# -*- coding: utf-8 -*-
"""إجبار الاتصالات على IPv4 — يحل مشاكل DNS داخل حاويات Hugging Face."""
import socket

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    if host and host not in ("localhost", "127.0.0.1", "::1"):
        try:
            return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        except socket.gaierror:
            pass
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_first
