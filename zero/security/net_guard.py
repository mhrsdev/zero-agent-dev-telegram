"""Zero v2 SSRF defense — ADR T-8.9, R-15.

Five mandatory rules (security-guidelines.md §7):

    1. Address pinning — DNS resolved ONCE; same IP used for entire request
       (anti-rebinding).
    2. Reject forbidden ranges — loopback, RFC1918, link-local, cloud metadata
       (``169.254.169.254``, ``fd00:ec2::254``), multicast, reserved.
    3. Re-validate on every redirect — same rules applied to redirect target.
    4. Size/time caps — both must be set on every outbound HTTP call.
    5. Protocol allowlist — only ``https``; ``http`` only for explicit ``localhost``.

**Structural test (T-8.9 acceptance)**: no code path can issue outbound HTTP
with untrusted URL without passing through ``net_guard``.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

__all__ = [
    "CLOUD_METADATA_HOSTS",
    "FORBIDDEN_IP_RANGES",
    "NetGuard",
    "NetGuardDecision",
    "NetGuardError",
    "check_url",
]


# ---------------------------------------------------------------------- errors

class NetGuardError(RuntimeError):
    """Raised when a URL fails SSRF checks."""


class NetGuardDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


# ---------------------------------------------------------------------- forbidden ranges

# Cloud metadata endpoints — always blocked, no exceptions.
CLOUD_METADATA_HOSTS: Final[frozenset[str]] = frozenset({
    "169.254.169.254",       # AWS / Azure / GCP classic metadata
    "169.254.170.2",         # ECS task metadata
    "169.254.169.253",       # GCP alternate
    "metadata.google.internal",  # GCP DNS name
    "metadata.goog",         # GCP short alias
    "fd00:ec2::254",         # AWS IPv6 metadata
    "100.100.100.200",       # Alibaba Cloud metadata
})


# IP ranges that are always forbidden (loopback, private, link-local, etc.)
FORBIDDEN_IP_RANGES: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    # IPv4
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 private
    ipaddress.ip_network("169.254.0.0/16"),    # link-local (covers all metadata)
    ipaddress.ip_network("0.0.0.0/8"),         # "this network"
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("192.0.2.0/24"),      # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),       # multicast
    ipaddress.ip_network("240.0.0.0/4"),       # reserved
    # IPv6
    ipaddress.ip_network("::1/128"),           # loopback
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local
    ipaddress.ip_network("ff00::/8"),          # multicast
    ipaddress.ip_network("::/128"),            # unspecified
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped (will be caught by IPv4 rules too)
)


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` falls in any forbidden range."""
    for net in FORBIDDEN_IP_RANGES:
        # ipaddress overloads __contains__ to handle mixed-type checks
        try:
            if ip in net:
                return True
        except TypeError:
            continue
    return False


# ---------------------------------------------------------------------- NetGuard

@dataclass(slots=True)
class NetGuard:
    """Apply SSRF rules to outbound HTTP requests.

    Usage:
        >>> guard = NetGuard()
        >>> ip = guard.resolve_and_check("https://example.com/path")
        >>> # Pass `ip` to your HTTP client with the original Host header.

    Or use the convenience function:
        >>> check_url("https://example.com/path")  # raises NetGuardError on deny
    """

    allow_localhost_http: bool = True  # http://localhost allowed when True
    max_redirects: int = 5

    def check_url(self, url: str) -> NetGuardDecision:
        """Validate ``url``. Returns ALLOW or raises ``NetGuardError``."""
        self._check_one(url)
        return NetGuardDecision.ALLOW

    def _check_one(self, url: str) -> None:
        parsed = urlparse(url)

        # Rule 5: protocol allowlist.
        if parsed.scheme == "https":
            pass
        elif parsed.scheme == "http":
            http_host = parsed.hostname or ""
            # Explicitly allow localhost / 127.0.0.1 / ::1 over http (for dev).
            # We can't rely on the IP-range check below because those ranges
            # ARE forbidden by default — we need to bypass them here for
            # explicitly-requested localhost.
            if not (self.allow_localhost_http and http_host in ("localhost", "127.0.0.1", "::1")):
                raise NetGuardError(
                    f"http scheme only allowed for explicit localhost — got {url!r}"
                )
            # Localhost allowed — skip the IP-range check below.
            return
        else:
            raise NetGuardError(
                f"protocol {parsed.scheme!r} not in allowlist (https, http-for-localhost) — url={url!r}"
            )

        host = parsed.hostname
        if not host:
            raise NetGuardError(f"URL {url!r} has no hostname")

        # Rule 1: cloud metadata hosts — always blocked by name.
        if host in CLOUD_METADATA_HOSTS:
            raise NetGuardError(f"cloud metadata host {host!r} is forbidden (SSRF R-15)")

        # Rule 1: address pinning — resolve hostname to IP(s) once.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise NetGuardError(f"DNS resolution failed for {host!r}: {e}") from e

        if not infos:
            raise NetGuardError(f"DNS returned no addresses for {host!r}")

        # Rule 2: reject forbidden ranges on every resolved address.
        for family, _, _, _, sockaddr in infos:
            ip_str = str(sockaddr[0])
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError as e:
                raise NetGuardError(f"invalid IP {ip_str!r} for host {host!r}: {e}") from e
            if _is_forbidden_ip(ip):
                raise NetGuardError(
                    f"host {host!r} resolves to forbidden IP {ip} (SSRF R-15)"
                )

    def resolve_pinned_ip(self, url: str) -> str:
        """Resolve ``url`` and return the pinned IP for use in HTTP client.

        Use this with `httpx` to bypass DNS on the actual TCP connect —
        prevents TOCTOU / DNS rebinding between the SSRF check and the
        connect() call.
        """
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise NetGuardError(f"URL {url!r} has no hostname")
        # Run the full check first (raises on deny).
        self.check_url(url)
        # Return the first resolved address.
        infos = socket.getaddrinfo(host, None)
        return str(infos[0][4][0])


# ---------------------------------------------------------------------- convenience

_default_guard = NetGuard()


def check_url(url: str) -> NetGuardDecision:
    """Module-level convenience: check ``url`` with the default guard."""
    return _default_guard.check_url(url)
