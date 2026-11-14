"""Network tools: fetch_url source records, allow-list, private-address refusal, web_search (A3).

No test opens a real network connection. Fetches use an injected fake transport
and an injected resolver. The one test that uses the default transport points it
at a server bound to 127.0.0.1 and proves the request is refused before any
connection is made.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import http.server
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import unittest
from unittest import mock

import core.tools.net as net_mod
from core.kernel.types import ActionKind, Budget
from core.tools.spec import ToolContext, ToolSpec
from core.tools.net import (
    HttpResponse, is_public_address, make_fetch_url, make_web_search,
)

CLOCK = "2026-01-02T03:04:05Z"
GLOBAL_IP = "93.184.216.34"   # a public address; never contacted (fake transport)
OTHER_GLOBAL_IP = "93.184.216.35"
_REAL_GETADDRINFO = socket.getaddrinfo


class MemoryJournal:
    def __init__(self):
        self.items = {}

    def seen(self, key):
        return self.items.get(key)

    def record(self, key, result):
        self.items[key] = result


class FakeTransport:
    """Returns scripted responses in order and records every URL and pinned address asked for."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []
        self.addresses = []

    def __call__(self, url, timeout_s, max_bytes, address):
        self.urls.append(url)
        self.addresses.append(address)
        return self.responses.pop(0)


def raw_response(status_line, headers, body=b""):
    """Bytes of one HTTP/1.1 response with a correct Content-Length."""
    lines = [status_line] + [f"{k}: {v}" for k, v in headers.items()]
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


class FakeConnector:
    """Stands in for ``core.tools.net._open_socket``, the only way the default
    transport opens a TCP connection. Records every (ip, port) it is asked to
    connect to. Each connection is one end of a socketpair; the other end sends
    the next canned response and keeps whatever the client writes."""

    def __init__(self, *raw_responses):
        self.raw = list(raw_responses)
        self.targets = []
        self._peers = []

    def __call__(self, address, timeout_s):
        self.targets.append(tuple(address))
        client, peer = socket.socketpair()
        peer.settimeout(2)
        peer.sendall(self.raw.pop(0) if self.raw else b"")
        self._peers.append(peer)
        return client

    def requests(self):
        """The request bytes each connection received, in connect order."""
        return [peer.recv(65536) for peer in self._peers]

    def close(self):
        for peer in self._peers:
            peer.close()


def _guarded_getaddrinfo(host, *args, **kwargs):
    """A real name lookup would be a real network lookup. Fail instead. IP literals pass."""
    try:
        ipaddress.ip_address(host)
    except (TypeError, ValueError):
        raise AssertionError(f"real name lookup attempted for {host!r}")
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def make_ctx(root):
    unit = root / "unit"
    scratch = root / "scratch"
    unit.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return ToolContext(root=root, unit_dir=unit, scratch_dir=scratch,
                       writable=("unit", "scratch"), blind=(), budget=Budget(),
                       journal=MemoryJournal())


def resolver_for(table):
    def resolve(host):
        if host not in table:
            raise OSError(f"no such host {host}")
        return table[host]
    return resolve


def no_resolver(host):
    raise AssertionError(f"resolver must not be asked about IP literal {host!r}")


class PublicAddressTests(unittest.TestCase):
    def test_public_addresses_pass(self):
        self.assertTrue(is_public_address(GLOBAL_IP))
        self.assertTrue(is_public_address("2606:4700:4700::1111"))

    def test_private_and_special_addresses_refused(self):
        for addr in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "172.16.4.4",
                     "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
                     "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1",
                     "not-an-address"):
            with self.subTest(addr=addr):
                self.assertFalse(is_public_address(addr))


class FetchUrlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.ctx = make_ctx(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fetch_records_source_and_reports_touched(self):
        body = b"hello from the fake server\n"
        transport = FakeTransport(HttpResponse(200, {"Content-Type": "text/plain"}, body))
        spec = make_fetch_url(allowed_hosts={"docs.example.test"}, transport=transport,
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}),
                              clock=lambda: CLOCK)
        self.assertIsInstance(spec, ToolSpec)
        url = "https://docs.example.test/page"
        result = spec.run({"url": url}, self.ctx)

        self.assertEqual(transport.urls, [url])
        self.assertEqual(len(result.touched), 1)
        rel = result.touched[0]
        self.assertTrue(rel.startswith("unit/sources/") and rel.endswith(".json"), rel)
        record = json.loads((self.root / rel).read_text(encoding="utf-8"))
        self.assertEqual(record["url"], url)
        self.assertEqual(record["fetched_at"], CLOCK)
        self.assertEqual(record["sha256"], hashlib.sha256(body).hexdigest())
        self.assertIn(hashlib.sha256(body).hexdigest(), result.content)

    def test_action_is_net_and_declares_net_effect(self):
        spec = make_fetch_url(allowed_hosts={"docs.example.test"},
                              transport=FakeTransport(), resolver=no_resolver)
        action = spec.action_for({"url": "https://docs.example.test/x"})
        self.assertEqual(action.kind, ActionKind.NET)
        self.assertEqual(action.url, "https://docs.example.test/x")
        self.assertIn("net", spec.effects)

    def test_host_off_allow_list_is_refused_without_a_request(self):
        transport = FakeTransport()
        spec = make_fetch_url(allowed_hosts={"docs.example.test"}, transport=transport,
                              resolver=resolver_for({"evil.example.test": [GLOBAL_IP]}))
        result = spec.run({"url": "https://evil.example.test/x"}, self.ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertEqual(result.touched, ())
        self.assertEqual(transport.urls, [])
        self.assertFalse((self.ctx.unit_dir / "sources").exists())

    def test_private_ip_literals_refused_even_when_listed(self):
        literals = ["http://127.0.0.1/", "http://10.1.2.3/", "http://169.254.169.254/latest",
                    "http://[::1]/", "http://192.168.0.1:8080/"]
        hosts = {"127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "192.168.0.1"}
        transport = FakeTransport()
        spec = make_fetch_url(allowed_hosts=hosts, transport=transport, resolver=no_resolver)
        for url in literals:
            with self.subTest(url=url):
                result = spec.run({"url": url}, self.ctx)
                self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertEqual(transport.urls, [])

    def test_name_resolving_to_private_address_is_refused(self):
        transport = FakeTransport()
        spec = make_fetch_url(allowed_hosts={"intranet.example.test"}, transport=transport,
                              resolver=resolver_for({"intranet.example.test": ["192.168.1.9"]}))
        result = spec.run({"url": "http://intranet.example.test/"}, self.ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertIn("non-public", result.content)
        self.assertEqual(transport.urls, [])

    def test_unresolvable_name_is_refused(self):
        transport = FakeTransport()
        spec = make_fetch_url(allowed_hosts={"gone.example.test"}, transport=transport,
                              resolver=resolver_for({}))
        result = spec.run({"url": "https://gone.example.test/"}, self.ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertEqual(transport.urls, [])

    def test_non_http_scheme_refused(self):
        transport = FakeTransport()
        spec = make_fetch_url(allowed_hosts={"docs.example.test"}, transport=transport,
                              resolver=resolver_for({}))
        for url in ("file:///etc/hosts", "ftp://docs.example.test/a"):
            with self.subTest(url=url):
                result = spec.run({"url": url}, self.ctx)
                self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertEqual(transport.urls, [])

    def test_redirect_to_private_host_is_not_followed(self):
        transport = FakeTransport(HttpResponse(302, {"Location": "http://127.0.0.1:9/admin"}, b""))
        spec = make_fetch_url(allowed_hosts={"docs.example.test"}, transport=transport,
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}))
        result = spec.run({"url": "https://docs.example.test/old"}, self.ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertEqual(transport.urls, ["https://docs.example.test/old"])
        self.assertEqual(result.touched, ())

    def test_redirect_within_allow_list_is_followed_and_recorded(self):
        transport = FakeTransport(
            HttpResponse(301, {"location": "/new"}, b""),
            HttpResponse(200, {}, b"moved"),
        )
        spec = make_fetch_url(allowed_hosts={"docs.example.test"}, transport=transport,
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}),
                              clock=lambda: CLOCK)
        result = spec.run({"url": "https://docs.example.test/old"}, self.ctx)
        self.assertEqual(transport.urls, ["https://docs.example.test/old",
                                          "https://docs.example.test/new"])
        record = json.loads((self.root / result.touched[0]).read_text(encoding="utf-8"))
        self.assertEqual(record["url"], "https://docs.example.test/new")
        self.assertEqual(record["requested_url"], "https://docs.example.test/old")

    def test_http_error_records_nothing(self):
        transport = FakeTransport(HttpResponse(404, {}, b"nope"))
        spec = make_fetch_url(allowed_hosts={"docs.example.test"}, transport=transport,
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}))
        result = spec.run({"url": "https://docs.example.test/missing"}, self.ctx)
        self.assertIn("HTTP 404", result.content)
        self.assertEqual(result.touched, ())

    def test_real_default_transport_never_reaches_loopback_server(self):
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"should never be served")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            # No transport injected: the default one would connect if the check failed.
            spec = make_fetch_url(allowed_hosts={"127.0.0.1"}, resolver=no_resolver)
            result = spec.run({"url": f"http://127.0.0.1:{port}/secret"}, self.ctx)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertEqual(hits, [])
        self.assertEqual(result.touched, ())


class PinnedTransportTests(unittest.TestCase):
    """The default transport: proxy-blind, connects only to the validated IP.

    Every connection goes through a FakeConnector, and any real name lookup
    fails the test, so nothing here reaches the network.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.ctx = make_ctx(self.root)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(socket, "getaddrinfo", _guarded_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connector(self, *raw_responses):
        connector = FakeConnector(*raw_responses)
        self.addCleanup(connector.close)
        patcher = mock.patch.object(net_mod, "_open_socket", connector)
        patcher.start()
        self.addCleanup(patcher.stop)
        return connector

    def test_proxy_env_is_ignored(self):
        hits = []

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        env = {name: proxy_url for name in
               ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}

        connector = self._connector(raw_response("HTTP/1.1 200 OK", {}, b"direct"))
        spec = make_fetch_url(allowed_hosts={"docs.example.test"},
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}),
                              clock=lambda: CLOCK)
        with mock.patch.dict(os.environ, env):
            result = spec.run({"url": "http://docs.example.test/page"}, self.ctx)

        self.assertEqual(hits, [], "a proxy from the environment received a request")
        self.assertEqual(connector.targets, [(GLOBAL_IP, 80)])
        self.assertIn("direct", result.content)

    def test_dns_rebinding_connects_to_validated_ip(self):
        lookups = []

        def rebinding_resolver(host):
            lookups.append(host)
            return [GLOBAL_IP] if len(lookups) == 1 else ["127.0.0.1"]

        connector = self._connector(raw_response("HTTP/1.1 200 OK", {}, b"ok"))
        spec = make_fetch_url(allowed_hosts={"docs.example.test"},
                              resolver=rebinding_resolver, clock=lambda: CLOCK)
        result = spec.run({"url": "http://docs.example.test/page"}, self.ctx)

        self.assertEqual(connector.targets, [(GLOBAL_IP, 80)])
        self.assertEqual(lookups, ["docs.example.test"], "the name was resolved again")
        self.assertIn("ok", result.content)

    def test_pinned_request_keeps_original_host_header(self):
        connector = self._connector(raw_response("HTTP/1.1 200 OK", {}, b"ok"))
        spec = make_fetch_url(allowed_hosts={"docs.example.test"},
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}),
                              clock=lambda: CLOCK)
        spec.run({"url": "http://docs.example.test/page?q=1"}, self.ctx)

        (request,) = connector.requests()
        self.assertTrue(request.startswith(b"GET /page?q=1 HTTP/1.1\r\n"), request)
        self.assertIn(b"\r\nHost: docs.example.test\r\n", request)

    def test_redirect_hop_is_revalidated_and_repinned(self):
        table = {"docs.example.test": [GLOBAL_IP], "other.example.test": [OTHER_GLOBAL_IP]}
        lookups = []

        def resolver(host):
            lookups.append(host)
            return table[host]

        connector = self._connector(
            raw_response("HTTP/1.1 302 Found", {"Location": "http://other.example.test/x"}),
            raw_response("HTTP/1.1 200 OK", {}, b"moved"),
        )
        spec = make_fetch_url(allowed_hosts={"docs.example.test", "other.example.test"},
                              resolver=resolver, clock=lambda: CLOCK)
        result = spec.run({"url": "http://docs.example.test/old"}, self.ctx)

        self.assertEqual(lookups, ["docs.example.test", "other.example.test"])
        self.assertEqual(connector.targets, [(GLOBAL_IP, 80), (OTHER_GLOBAL_IP, 80)])
        self.assertIn("moved", result.content)

    def test_https_pins_ip_and_uses_hostname_for_tls(self):
        server_names = []

        def fake_wrap(context, sock, *args, **kwargs):
            server_names.append(kwargs.get("server_hostname"))
            raise OSError("stop after recording the TLS target")

        connector = self._connector()
        spec = make_fetch_url(allowed_hosts={"docs.example.test"},
                              resolver=resolver_for({"docs.example.test": [GLOBAL_IP]}),
                              clock=lambda: CLOCK)
        with mock.patch.object(ssl.SSLContext, "wrap_socket", fake_wrap):
            with self.assertRaises(OSError):
                spec.run({"url": "https://docs.example.test/page"}, self.ctx)

        self.assertEqual(connector.targets, [(GLOBAL_IP, 443)])
        self.assertEqual(server_names, ["docs.example.test"])


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = make_ctx(pathlib.Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_default_backend(self):
        with self.assertRaises(ValueError):
            make_web_search(None)

    def test_uses_configured_backend(self):
        seen = []

        def backend(query, limit):
            seen.append((query, limit))
            return [{"title": "Alpha", "url": "https://a.example.test/", "snippet": "first"}]

        spec = make_web_search(backend)
        action = spec.action_for({"query": "alpha facts"})
        self.assertEqual(action.kind, ActionKind.NET)
        result = spec.run({"query": "alpha facts", "limit": 3}, self.ctx)
        self.assertEqual(seen, [("alpha facts", 3)])
        self.assertIn("Alpha", result.content)
        self.assertEqual(result.touched, ())

    def test_backend_failure_is_reported_not_raised(self):
        def backend(query, limit):
            raise RuntimeError("provider down")

        result = make_web_search(backend).run({"query": "x"}, self.ctx)
        self.assertIn("search failed", result.content)


if __name__ == "__main__":
    unittest.main()
