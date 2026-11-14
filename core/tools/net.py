"""Network tools: ``fetch_url`` and ``web_search`` (plan section 1.10, brief A3).

``fetch_url`` is refused unless the URL is http or https, its host is on the
pack's allow-list, and every address the host resolves to is public. The name
is resolved once, here. The transport connects to the first validated address
and never resolves the name again, so a DNS rebinding answer cannot redirect
the connection. The Host header and TLS server name stay the original host.
The default transport ignores proxy environment variables. Redirects are not
followed by the transport; each hop is validated and pinned again here. Every
successful fetch writes a source record (URL, time, content sha256) under
``<unit_dir>/sources/`` and reports that file in ``touched``.

``web_search`` has no backend of its own. The caller supplies one through
configuration, and the factory refuses to build the tool without one.

Standard library and ``core.kernel`` only.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
from typing import Callable, Mapping, Optional, Sequence

from core.kernel.paths import atomic_write
from core.kernel.types import Action, ActionKind, canonical_json
from core.tools.spec import ToolContext, ToolResult, ToolSpec

ALLOWED_SCHEMES = frozenset({"http", "https"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 20_000
SOURCES_DIR = "sources"
SEARCH_LIMIT_MAX = 20


@dataclasses.dataclass(frozen=True)
class HttpResponse:
    """One HTTP response. ``headers`` keys may be any case; lookups lower-case them."""
    status: int
    headers: Mapping[str, str]
    body: bytes


# A transport fetches one URL without following redirects.
# Signature: (url, timeout_s, max_bytes, address) -> HttpResponse. ``address`` is
# the IP that was validated for this URL's host. The transport must connect to it
# and must not resolve the host name again.
Transport = Callable[[str, float, int, str], HttpResponse]
# A resolver maps a host name to its IP addresses. It may raise OSError.
Resolver = Callable[[str], Sequence[str]]
# A search backend returns hits with "title", "url" and "snippet" keys.
SearchBackend = Callable[[str, int], Sequence[Mapping[str, str]]]


def is_public_address(addr: str) -> bool:
    """True only for a globally routable IP address.

    IPv4-mapped IPv6 addresses are judged by the IPv4 address they carry. A zone
    suffix (``fe80::1%eth0``) is ignored. Anything that does not parse is not public.
    """
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return False
    if ip.is_reserved or ip.is_unspecified:
        return False
    return ip.is_global


def _system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def _open_socket(address: tuple[str, int], timeout_s: float) -> socket.socket:
    """Open the TCP connection. The only place the default transport dials out.

    Tests replace this function, so they can see the exact (ip, port) dialled.
    """
    return socket.create_connection(address, timeout_s)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP. The Host header keeps the original name; the socket goes to ``pinned``."""

    def __init__(self, host: str, port: int, *, pinned: str, timeout_s: float):
        super().__init__(host, port, timeout=timeout_s)
        self._pinned = pinned

    def connect(self) -> None:
        self.sock = _open_socket((self._pinned, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS. The socket goes to ``pinned``; TLS SNI and certificate checks use the original name."""

    def __init__(self, host: str, port: int, *, pinned: str, timeout_s: float,
                 tls: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout_s, context=tls)
        self._pinned = pinned
        self._tls = tls

    def connect(self) -> None:
        raw = _open_socket((self._pinned, self.port), self.timeout)
        try:
            self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def default_transport(url: str, timeout_s: float, max_bytes: int, address: str) -> HttpResponse:
    """The production transport: one GET to ``address``, no redirects followed, body capped.

    http.client is used directly, not urllib, so no proxy environment variable
    can take the request elsewhere. The name is never resolved here.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    secure = parts.scheme.lower() == "https"
    port = parts.port or (443 if secure else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if secure:
        conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
            host, port, pinned=address, timeout_s=timeout_s, tls=ssl.create_default_context())
    else:
        conn = _PinnedHTTPConnection(host, port, pinned=address, timeout_s=timeout_s)
    try:
        conn.request("GET", target, headers={"User-Agent": "workbench-fetch/1", "Accept": "*/*"})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return HttpResponse(resp.status, headers, resp.read(max_bytes))
    finally:
        conn.close()


def _vet(url: str, allowed_hosts: frozenset[str],
         resolver: Resolver) -> tuple[Optional[str], str]:
    """Check ``url`` for one hop.

    Returns ``(reason, address)``. ``reason`` is why the URL may not be fetched,
    or None. When it is None, ``address`` is the validated IP to connect to. For
    a name, every address it resolves to must be public, and the first is pinned.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return "URL is malformed", ""
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"scheme {scheme!r} is not allowed (http and https only)", ""
    host = (parts.hostname or "").lower()
    if not host:
        return "URL has no host", ""
    if host not in allowed_hosts:
        return f"host {host!r} is not on the allow-list", ""
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = list(resolver(host))
        except OSError as exc:
            return f"host {host!r} does not resolve ({exc.__class__.__name__})", ""
        if not addresses:
            return f"host {host!r} does not resolve", ""
    for addr in addresses:
        if not is_public_address(addr):
            return f"host {host!r} resolves to a non-public address", ""
    return None, str(addresses[0])


def _refused(reason: str) -> ToolResult:
    return ToolResult(content=f"refused: {reason}")


def _url_arg(args: dict) -> str:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("fetch_url needs a non-empty string 'url'")
    return url.strip()


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def make_fetch_url(
    *,
    allowed_hosts,
    transport: Optional[Transport] = None,
    resolver: Optional[Resolver] = None,
    clock: Optional[Callable[[], str]] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ToolSpec:
    """Build the ``fetch_url`` tool for one pack's allow-list.

    ``transport``, ``resolver`` and ``clock`` default to the production behaviour.
    Tests inject them so that nothing reaches the network.
    """
    hosts = frozenset(h.lower() for h in allowed_hosts)
    fetch = transport if transport is not None else default_transport
    resolve = resolver if resolver is not None else _system_resolver
    now = clock if clock is not None else _utc_now

    def to_action(args: dict) -> Action:
        return Action(kind=ActionKind.NET, tool="fetch_url", url=_url_arg(args))

    def run(args: dict, ctx: ToolContext) -> ToolResult:
        requested = _url_arg(args)
        current = requested
        hops = 0
        while True:
            reason, address = _vet(current, hosts, resolve)
            if reason is not None:
                return _refused(reason)
            resp = fetch(current, timeout_s, max_bytes, address)
            if resp.status in REDIRECT_STATUSES:
                location = _header(resp.headers, "location")
                if not location:
                    return ToolResult(content=f"fetch failed: HTTP {resp.status} without Location")
                hops += 1
                if hops > MAX_REDIRECTS:
                    return _refused("too many redirects")
                current = urllib.parse.urljoin(current, location)
                continue
            break

        if not 200 <= resp.status < 300:
            return ToolResult(content=f"fetch failed: HTTP {resp.status}")

        body = resp.body[:max_bytes]
        truncated_bytes = len(resp.body) > max_bytes
        digest = hashlib.sha256(body).hexdigest()
        fetched_at = now()
        entry = {
            "url": current,
            "requested_url": requested,
            "fetched_at": fetched_at,
            "sha256": digest,
            "bytes": len(body),
            "status": resp.status,
            "content_type": _header(resp.headers, "content-type"),
            "truncated": truncated_bytes,
        }
        name = hashlib.sha256(
            canonical_json([requested, fetched_at, digest]).encode("utf-8")
        ).hexdigest()[:24] + ".json"
        record_path = ctx.unit_dir / SOURCES_DIR / name
        try:
            rel = record_path.resolve().relative_to(ctx.root.resolve()).as_posix()
        except ValueError:
            raise ValueError("the unit directory must lie inside the workspace root")
        atomic_write(record_path, json.dumps(entry, sort_keys=True, indent=2) + "\n")

        text = body.decode("utf-8", errors="replace")
        note = ""
        if len(text) > max_chars:
            text = text[:max_chars]
            note = f"\n[content truncated at {max_chars} characters]"
        if truncated_bytes:
            note += f"\n[body truncated at {max_bytes} bytes; sha256 covers the stored bytes]"
        content = (f"fetched {current} ({len(body)} bytes, sha256 {digest}). "
                   f"Source recorded at {rel}.\n\n{text}{note}")
        return ToolResult(content=content, touched=(rel,))

    return ToolSpec(
        name="fetch_url",
        description=("Fetch an http or https URL on the pack's allow-list. "
                     "The fetch is recorded as a source under sources/."),
        args_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        effects=frozenset({"net", "write"}),
        to_action=to_action,
        run=run,
        idempotent=True,
    )


def make_web_search(backend: Optional[SearchBackend]) -> ToolSpec:
    """Build ``web_search`` around a configured backend.

    No backend ships with the product. Passing None is an error, so a
    configuration that asks for search without a backend fails at load time.
    """
    if backend is None:
        raise ValueError("web_search has no default backend; configure one to enable it")
    if not callable(backend):
        raise TypeError("web_search backend must be callable")

    def to_action(args: dict) -> Action:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("web_search needs a non-empty string 'query'")
        return Action(kind=ActionKind.NET, tool="web_search", content=query)

    def run(args: dict, ctx: ToolContext) -> ToolResult:
        action = to_action(args)
        limit = args.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("web_search 'limit' must be a positive integer")
        limit = min(limit, SEARCH_LIMIT_MAX)
        try:
            hits = list(backend(action.content, limit))[:limit]
        except Exception as exc:  # the backend is configuration; report, do not crash the loop
            return ToolResult(content=f"search failed: {exc.__class__.__name__}")
        if not hits:
            return ToolResult(content="no results")
        lines = []
        for hit in hits:
            title = str(hit.get("title", "")).strip() or "(untitled)"
            url = str(hit.get("url", "")).strip()
            snippet = str(hit.get("snippet", "")).strip()
            line = f"- {title} <{url}>"
            lines.append(f"{line}: {snippet}" if snippet else line)
        return ToolResult(content="\n".join(lines))

    return ToolSpec(
        name="web_search",
        description="Search the web through the configured search backend.",
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT_MAX},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        effects=frozenset({"net"}),
        to_action=to_action,
        run=run,
        idempotent=True,
    )
