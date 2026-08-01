"""Regression tests for web_fetch SSRF protection.

Covers the IPv4-mapped-IPv6 bypass (::ffff:127.0.0.1 etc.) and the
internal-range blocklist. These are pure checks of _is_blocked_ip /
_resolve_and_check — no network is touched.
"""

from microagent.tools.builtins.web_fetch import _is_blocked_ip


class TestSSRFBlocklist:
    def test_blocks_loopback_v4(self):
        assert _is_blocked_ip("127.0.0.1") is True

    def test_blocks_rfc1918(self):
        for ip in ("10.1.2.3", "172.16.0.1", "192.168.1.1"):
            assert _is_blocked_ip(ip) is True, ip

    def test_blocks_link_local(self):
        # AWS metadata endpoint lives here
        assert _is_blocked_ip("169.254.169.254") is True

    def test_allows_public_ip(self):
        for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
            assert _is_blocked_ip(ip) is False, ip

    def test_blocks_ipv6_loopback(self):
        assert _is_blocked_ip("::1") is True

    def test_blocks_ipv6_unique_local(self):
        assert _is_blocked_ip("fd00::1") is True


class TestIPv4MappedIPv6Bypass:
    """Regression: ::ffff:127.0.0.1 / ::ffff:169.254.169.254 used to
    bypass the blocklist because ip_address() reports family=6 and the
    IPv4Network 'in' check is family-sensitive."""

    def test_blocks_ipv4_mapped_loopback(self):
        assert _is_blocked_ip("::ffff:127.0.0.1") is True

    def test_blocks_ipv4_mapped_aws_metadata(self):
        # The headline exploit: cloud metadata endpoint via IPv6 mapping
        assert _is_blocked_ip("::ffff:169.254.169.254") is True

    def test_blocks_ipv4_mapped_rfc1918(self):
        for ip in ("::ffff:10.0.0.1", "::ffff:172.16.0.1", "::ffff:192.168.0.1"):
            assert _is_blocked_ip(ip) is True, ip

    def test_allows_ipv4_mapped_public(self):
        # A mapped public IP should not be blocked by the mapped-v4 check.
        assert _is_blocked_ip("::ffff:8.8.8.8") is False

    def test_invalid_ip_not_blocked(self):
        assert _is_blocked_ip("not-an-ip") is False
