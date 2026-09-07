from __future__ import annotations

import email.utils
import gzip
import io
import ipaddress
import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "charset" and value.strip():
                charset = value.strip().strip('"')
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def validate_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Localhost URLs are not allowed.")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as error:
        raise ValueError("The URL hostname could not be resolved.") from error
    if not addresses:
        raise ValueError("The URL hostname did not resolve to an address.")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("The URL resolved to a non-public address.")


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validate_redirects: bool) -> None:
        super().__init__()
        self.validate_redirects = validate_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if req.get_method() != "GET" or any(k.lower() in {"authorization", "x-goog-api-key"} for k, _ in req.header_items()):
            raise HttpError("Refusing credential-bearing or POST redirect.", status=code)
        if self.validate_redirects:
            validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PoliteHttpClient:
    """Sequential HTTP client with per-host pacing and bounded retries."""

    def __init__(
        self,
        *,
        user_agent: str,
        product_hunt_delay_seconds: float,
        product_hunt_jitter_seconds: float,
        external_delay_seconds: float,
        external_jitter_seconds: float,
        timeout_seconds: float,
        max_attempts: int,
    ) -> None:
        self.user_agent = user_agent
        self.product_hunt_delay_seconds = product_hunt_delay_seconds
        self.product_hunt_jitter_seconds = product_hunt_jitter_seconds
        self.external_delay_seconds = external_delay_seconds
        self.external_jitter_seconds = external_jitter_seconds
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self._next_request_at: dict[str, float] = {}
        self._custom_minimum_delay: dict[str, float] = {}

    @staticmethod
    def _is_product_hunt(hostname: str) -> bool:
        hostname = hostname.lower().rstrip(".")
        return hostname == "producthunt.com" or hostname.endswith(".producthunt.com")

    def _pace(self, hostname: str) -> None:
        wait_seconds = self._next_request_at.get(hostname, 0.0) - time.monotonic()
        if wait_seconds > 0:
            LOGGER.info("Waiting %.1fs before requesting %s", wait_seconds, hostname)
            time.sleep(wait_seconds)

    def _mark_request_complete(self, hostname: str) -> None:
        if self._is_product_hunt(hostname):
            base = self.product_hunt_delay_seconds
            jitter = self.product_hunt_jitter_seconds
        else:
            base = self.external_delay_seconds
            jitter = self.external_jitter_seconds
        base = max(base, self._custom_minimum_delay.get(hostname, 0.0))
        self._next_request_at[hostname] = time.monotonic() + base + random.uniform(0, jitter)

    def set_minimum_delay(self, hostname: str, seconds: float) -> None:
        hostname = hostname.lower().rstrip(".")
        self._custom_minimum_delay[hostname] = max(
            self._custom_minimum_delay.get(hostname, 0.0),
            max(0.0, seconds),
        )

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        raw = headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                moment = email.utils.parsedate_to_datetime(raw)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return None

    @staticmethod
    def _decode_content(body: bytes, encoding: str, max_bytes: int) -> bytes:
        encoding = encoding.lower().strip()
        try:
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
                    decoded = archive.read(max_bytes + 1)
            elif encoding == "deflate":
                decompressor = zlib.decompressobj()
                decoded = decompressor.decompress(body, max_bytes + 1)
                remaining = max_bytes + 1 - len(decoded)
                if remaining > 0:
                    decoded += decompressor.flush(remaining)
                if decompressor.unconsumed_tail:
                    decoded += b"\x00"
            elif encoding in {"", "identity"}:
                return body
            else:
                raise HttpError(
                    f"Unsupported Content-Encoding response: {encoding}"
                )
        except (OSError, EOFError, zlib.error) as error:
            raise HttpError("The response used invalid compressed content.") from error
        if len(decoded) > max_bytes:
            raise HttpError(
                f"Decompressed response exceeded {max_bytes} bytes."
            )
        return decoded

    def get(
        self,
        url: str,
        *,
        max_bytes: int,
        validate_external_url: bool = False,
        headers: Mapping[str, str] | None = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
    ) -> HttpResponse:
        return self._request(
            url,
            method="GET",
            data=None,
            max_bytes=max_bytes,
            validate_external_url=validate_external_url,
            accept=accept,
            extra_headers=headers or {},
        )

    def post_json(
        self,
        url: str,
        payload: object,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self._request(
            url,
            method="POST",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            max_bytes=max_bytes,
            validate_external_url=False,
            accept="application/json",
            extra_headers={"Content-Type": "application/json", **(headers or {})},
        )

    def _request(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None,
        max_bytes: int,
        validate_external_url: bool,
        accept: str,
        extra_headers: Mapping[str, str],
    ) -> HttpResponse:
        if validate_external_url:
            validate_public_http_url(url)
        last_error: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            parsed = urllib.parse.urlsplit(url)
            hostname = (parsed.hostname or "").lower()
            self._pace(hostname)
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": accept,
                    "Accept-Encoding": "gzip, deflate",
                    **extra_headers,
                },
                method=method,
            )
            opener = urllib.request.build_opener(
                _ValidatingRedirectHandler(validate_external_url)
            )
            try:
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise HttpError(
                            f"Response from {url} exceeded {max_bytes} bytes.",
                            status=getattr(response, "status", None),
                        )
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    decoded = self._decode_content(
                        body,
                        headers.get("content-encoding", ""),
                        max_bytes,
                    )
                    return HttpResponse(
                        status=int(getattr(response, "status", 200)),
                        url=response.geturl(),
                        headers=headers,
                        body=decoded,
                    )
            except urllib.error.HTTPError as error:
                headers = {
                    key.lower(): value for key, value in (error.headers.items() if error.headers else [])
                }
                error_body = error.read(min(max_bytes, 256_000)).decode(
                    "utf-8", errors="replace"
                )
                last_error = HttpError(
                    f"HTTP {error.code} for {url}",
                    status=int(error.code),
                    body=error_body,
                    retry_after=self._retry_after(headers),
                )
                retryable = int(error.code) in RETRYABLE_STATUS
                retry_after = self._retry_after(headers)
            except (urllib.error.URLError, TimeoutError, OSError, HttpError) as error:
                last_error = error
                retryable = not isinstance(error, HttpError) or error.status in RETRYABLE_STATUS
                retry_after = None
            finally:
                self._mark_request_complete(hostname)

            if not retryable or attempt >= self.max_attempts:
                break
            backoff = min(120.0, 2 ** (attempt - 1) + random.uniform(0.5, 2.0))
            delay = max(backoff, retry_after or 0.0)
            LOGGER.warning(
                "Request failed (attempt %d/%d); retrying in %.1fs: %s",
                attempt,
                self.max_attempts,
                delay,
                last_error,
            )
            time.sleep(delay)

        if isinstance(last_error, HttpError):
            raise last_error
        raise HttpError(f"Request failed for {url}: {last_error}") from last_error


class RobotsPolicy:
    def __init__(self, client: PoliteHttpClient, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | bool] = {}

    def can_fetch(self, url: str) -> tuple[bool, str | None]:
        validate_public_http_url(url)
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._cache.get(origin)
        if cached is False:
            return False, "robots-unavailable"
        if isinstance(cached, urllib.robotparser.RobotFileParser):
            allowed = cached.can_fetch(self.user_agent, url)
            return allowed, None if allowed else "robots-disallowed"

        robots_url = origin + "/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = self.client.get(
                robots_url,
                max_bytes=512_000,
                validate_external_url=True,
                accept="text/plain,*/*;q=0.1",
            )
            parser.parse(response.text().splitlines())
        except HttpError as error:
            if error.status == 404:
                parser.parse([])
            else:
                self._cache[origin] = False
                return False, "robots-unavailable"
        agent_token = self.user_agent.split("/", 1)[0]
        crawl_delay = parser.crawl_delay(agent_token) or parser.crawl_delay("*")
        request_rate = parser.request_rate(agent_token) or parser.request_rate("*")
        if request_rate and request_rate.requests > 0:
            rate_delay = request_rate.seconds / request_rate.requests
        else:
            rate_delay = 0.0
        self.client.set_minimum_delay(
            parsed.hostname or "", max(float(crawl_delay or 0), rate_delay)
        )
        self._cache[origin] = parser
        allowed = parser.can_fetch(self.user_agent, url)
        return allowed, None if allowed else "robots-disallowed"
