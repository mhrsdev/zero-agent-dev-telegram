"""Unit tests for zero.security.net_guard — ADR T-8.9 (SSRF defense)."""
from __future__ import annotations

import pytest
from zero.security.net_guard import (
    CLOUD_METADATA_HOSTS,
    NetGuard,
    NetGuardError,
    check_url,
)


class TestNetGuard:
    def test_https_url_allowed(self) -> None:
        # Use a real domain that resolves to a public IP.
        guard = NetGuard()
        # We don't actually make a network call here — just check the URL parsing.
        # check_url() resolves DNS, which may fail in CI without network.
        # We use a domain that's guaranteed to resolve to a public IP.
        # Skip if no network.
        try:
            decision = guard.check_url("https://example.com/path")
            assert decision.value == "allow"
        except NetGuardError as e:
            if "DNS resolution failed" in str(e):
                pytest.skip("no network in CI")
            raise

    def test_http_localhost_allowed(self) -> None:
        guard = NetGuard(allow_localhost_http=True)
        decision = guard.check_url("http://localhost:8080/path")
        assert decision.value == "allow"

    def test_http_127_allowed(self) -> None:
        guard = NetGuard(allow_localhost_http=True)
        decision = guard.check_url("http://127.0.0.1:8080")
        assert decision.value == "allow"

    def test_http_non_localhost_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="http scheme only allowed"):
            guard.check_url("http://example.com")

    def test_ftp_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="protocol 'ftp' not in allowlist"):
            guard.check_url("ftp://example.com/file")

    def test_file_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="protocol 'file' not in allowlist"):
            guard.check_url("file:///etc/passwd")

    def test_cloud_metadata_aws_rejected(self) -> None:
        """169.254.169.254 (AWS metadata) must always be blocked."""
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="cloud metadata host"):
            guard.check_url("https://169.254.169.254/latest/meta-data/")

    def test_cloud_metadata_gcp_rejected(self) -> None:
        """metadata.google.internal must always be blocked."""
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="cloud metadata host"):
            guard.check_url("https://metadata.google.internal/computeMetadata/v1/")

    def test_loopback_ipv4_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="forbidden IP"):
            guard.check_url("https://127.0.0.1:443")

    def test_loopback_ipv6_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="forbidden IP"):
            guard.check_url("https://[::1]:443")

    def test_rfc1918_private_rejected(self) -> None:
        """All RFC1918 private ranges must be blocked."""
        guard = NetGuard()
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with pytest.raises(NetGuardError, match="forbidden IP"):
                guard.check_url(f"https://{ip}/")

    def test_link_local_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="forbidden IP"):
            guard.check_url("https://169.254.1.1/")

    def test_no_hostname_rejected(self) -> None:
        guard = NetGuard()
        with pytest.raises(NetGuardError, match="no hostname"):
            guard.check_url("https:///path")

    def test_module_level_check_url(self) -> None:
        """Module-level convenience function works."""
        try:
            check_url("http://localhost:8080")
        except NetGuardError as e:
            pytest.fail(f"localhost should be allowed: {e}")

    def test_metadata_hosts_constant_complete(self) -> None:
        """Verify all known metadata hosts are in CLOUD_METADATA_HOSTS."""
        expected = {
            "169.254.169.254",
            "169.254.170.2",
            "169.254.169.253",
            "metadata.google.internal",
            "metadata.goog",
            "fd00:ec2::254",
            "100.100.100.200",
        }
        assert expected.issubset(CLOUD_METADATA_HOSTS)
