"""Anonymous Viewable CSV evidence; never transactional duplicate protection."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

SOURCE_SHEET_ID = "1rAtRXT3GK2EQsdl1opMQpiTYfZJ-mCvjkNES8hz1vTQ"
SOURCE_GID = "6355673"
SOURCE_SHEET_NAME = "Viewable"
SINGAPORE = ZoneInfo("Asia/Singapore")
MAX_BODY_BYTES = 10 * 1024 * 1024
CACHE_SECONDS = 30
_REQUIRED = ("Timestamp", "[Myinfo] Name", "Department", "Status")
_TIMESTAMP = re.compile(r"[0-9]{1,2}/[0-9]{1,2}/([0-9]{4}) [0-9]{1,2}:[0-9]{2}")
_YEAR = re.compile(r"^[0-9]{1,2}/[0-9]{1,2}/([0-9]{4})(?:\s|$)")
# Only attendance information, not arbitrary extra identity/contact columns.
_DETAIL_HEADERS = frozenset(
    {
        "Do you have an MA/AL/OIL/WFH?",
        "Is your status for AM or PM?",
        "What is your AM Status",
        "What is your PM Status",
        "Medical Appointment (MA) Time",
        "MC Visit Status",
        "Appointment Timing (Indicate estimated/fixed appointment time)",
        "Appointment Timing",
        "Clinic / Hospital / Medical Centre Name",
        "IS Duties",
        "Roles",
        "Remarks (NSC/IS)",
        "Remarks (Event Name)",
        "Remarks (Location & Reporting Time)",
        "Remarks (OIL/PHIL)",
        "Date of Deployment",
        "Start Date (AL/OL)",
        "End Date (AL/OL)",
        "Start Date (OIL/PHIL/Birthday Off)",
        "End Date (OIL/PHIL/Birthday Off)",
        "Start Date (HL)",
        "End Date (HL)",
    }
)


def normalize_name(value: str) -> str:
    """Normalize a supplied full MyInfo name without inferring identity."""
    if not isinstance(value, str):
        raise ValueError("attendance name must be text")
    normalized = " ".join(value.split()).upper()
    if not normalized or len(normalized) > 200:
        raise ValueError("attendance name must contain 1 to 200 characters")
    return normalized


@dataclass(frozen=True)
class SubmissionRecord:
    timestamp: datetime
    status: str
    details: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RecordCheck:
    status: Literal["found", "not_found", "unavailable"]
    records: tuple[SubmissionRecord, ...]
    checked_at: datetime
    digest: str | None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "records": [record.to_dict() for record in self.records],
            "checked_at": self.checked_at.isoformat(),
            "digest": self.digest,
            "error_code": self.error_code,
        }


class SourceVerificationError(ValueError):
    """Safe source failure; deliberately excludes fetched content and URLs."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Snapshot:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    checked_at: datetime


def _parse(body: bytes, checked_at: datetime) -> _Snapshot:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceVerificationError("invalid_encoding") from None
    if re.match(r"\s*<(?:!doctype\s+html|html|head|body)\b", text, re.IGNORECASE):
        raise SourceVerificationError("invalid_response")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (csv.Error, ValueError):
        raise SourceVerificationError("invalid_csv") from None
    candidates = [i for i, row in enumerate(rows) if set(_REQUIRED).issubset(row)]
    if len(candidates) != 1:
        raise SourceVerificationError("invalid_headers")
    start = candidates[0]
    headers = tuple(rows[start])
    if len(set(headers)) != len(headers) or any(not header.strip() for header in headers):
        raise SourceVerificationError("invalid_headers")
    data = rows[start + 1 :]
    if any(len(row) != len(headers) for row in data):
        raise SourceVerificationError("invalid_row_width")
    return _Snapshot(headers, tuple(tuple(row) for row in data), checked_at)


def _digest(rows: list[tuple[tuple[str, str], ...]]) -> str:
    # Sort complete selected-owner rows, retaining duplicates; sheet order is irrelevant.
    canonical = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and (
            host == "docs.google.com"
            or re.fullmatch(r"doc-[a-z0-9-]+-sheets\.googleusercontent\.com", host) is not None
        )
    )


class SubmissionRecords:
    """Bounded anonymous fetches, shared in-memory snapshots and exact-owner lookups.

    An injected session remains caller-owned and must have no cookies, default auth
    or environment proxy credentials. There is deliberately no alternate source URL.
    Positive observations must be retained by the caller's durable owner/day store.
    """

    def __init__(
        self,
        *,
        sheet_id: str = SOURCE_SHEET_ID,
        gid: str | int = SOURCE_GID,
        session: aiohttp.ClientSession | None = None,
    ):
        if not isinstance(sheet_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
            raise ValueError("invalid sheet ID")
        if not re.fullmatch(r"[0-9]+", str(gid)):
            raise ValueError("invalid worksheet gid")
        if session is not None and (
            not isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
            or session.trust_env
            or getattr(session, "_default_auth", None) is not None
            or any(key.lower() in {"authorization", "cookie"} for key in session.headers)
        ):
            raise ValueError("records session must be anonymous and cookie-free")
        self.sheet_id = sheet_id
        self.gid = str(gid)
        self._session = session
        self._cache: _Snapshot | None = None
        self._cached_at = 0.0
        self._inflight: asyncio.Task[_Snapshot] | None = None
        self._expiry: asyncio.TimerHandle | None = None

    def _url(self, *, verification: bool = False, by_name: bool = False) -> str:
        selector = {"sheet": SOURCE_SHEET_NAME} if by_name else {"gid": self.gid}
        if verification:
            query = {"tqx": "out:csv", **selector}
            endpoint = "gviz/tq"
        else:
            query = {"format": "csv", **selector}
            endpoint = "export"
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/{endpoint}?{urlencode(query)}"

    async def _download(self, session: aiohttp.ClientSession, url: str) -> bytes:
        for _ in range(6):
            if not _allowed_url(url):
                raise SourceVerificationError("unsafe_redirect")
            async with session.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
                auto_decompress=False,
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise SourceVerificationError("unsafe_redirect")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise SourceVerificationError("http_error")
                if "html" in response.headers.get("Content-Type", "").lower():
                    raise SourceVerificationError("invalid_response")
                encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
                if encoding not in {"identity", "gzip", "deflate"}:
                    raise SourceVerificationError("invalid_response")
                decoder = (
                    zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
                    if encoding != "identity"
                    else None
                )
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if decoder is not None:
                        try:
                            # Bound decompression itself, not just aiohttp's decoded
                            # buffer, so a tiny compression bomb cannot allocate GB.
                            chunk = decoder.decompress(chunk, MAX_BODY_BYTES - len(body) + 1)
                        except zlib.error:
                            raise SourceVerificationError("invalid_response") from None
                        if decoder.unused_data:
                            raise SourceVerificationError("invalid_response")
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise SourceVerificationError("response_too_large")
                    body.extend(chunk)
                if decoder is not None and not decoder.eof:
                    raise SourceVerificationError("invalid_response")
                return bytes(body)
        raise SourceVerificationError("unsafe_redirect")

    async def _fetch(self, url: str) -> _Snapshot:
        try:
            async with asyncio.timeout(15):
                if self._session is None:
                    async with aiohttp.ClientSession(
                        cookie_jar=aiohttp.DummyCookieJar(), trust_env=False
                    ) as session:
                        body = await self._download(session, url)
                else:
                    body = await self._download(self._session, url)
                return _parse(body, datetime.now(UTC))
        except SourceVerificationError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            raise SourceVerificationError("network_error") from None

    async def _refresh(self) -> _Snapshot:
        snapshot = await self._fetch(self._url())
        self._cache = snapshot
        self._cached_at = time.monotonic()
        if self._expiry is not None:
            self._expiry.cancel()
        self._expiry = asyncio.get_running_loop().call_later(CACHE_SECONDS, self._expire)
        return snapshot

    def _expire(self) -> None:
        self._cache = None
        self._expiry = None

    def _finished(self, task: asyncio.Task[_Snapshot]) -> None:
        # Do not retain a completed task's full snapshot beyond the cache lifetime.
        if self._inflight is task:
            self._inflight = None
        if not task.cancelled():
            task.exception()

    async def _snapshot(self, *, fresh: bool) -> _Snapshot:
        # A fresh fetch already in flight wins over cached evidence for every caller.
        if self._inflight is None or self._inflight.done():
            if (
                not fresh
                and self._cache is not None
                and time.monotonic() - self._cached_at < CACHE_SECONDS
            ):
                return self._cache
            self._inflight = asyncio.create_task(self._refresh())
            # Retrieve late failures even when every waiter has been cancelled.
            self._inflight.add_done_callback(self._finished)
        return await asyncio.shield(self._inflight)

    async def verify_source(self) -> bool:
        """Fail installation unless Viewable and configured gid have identical data.

        Compare two gviz CSV responses, not gviz against export: those endpoints
        represent some values differently. Source changes between requests can
        conservatively fail verification; retry rather than silently choosing a tab.
        """
        by_gid, by_name = await asyncio.gather(
            self._fetch(self._url(verification=True)),
            self._fetch(self._url(verification=True, by_name=True)),
        )
        canonical_gid = sorted(
            tuple(sorted(zip(by_gid.headers, row, strict=True))) for row in by_gid.rows
        )
        canonical_name = sorted(
            tuple(sorted(zip(by_name.headers, row, strict=True))) for row in by_name.rows
        )
        if sorted(by_gid.headers) != sorted(by_name.headers) or canonical_gid != canonical_name:
            raise SourceVerificationError("source_mismatch")
        return True

    async def lookup(self, name: str, department: str, day: date, *, fresh: bool = False) -> RecordCheck:
        owner = normalize_name(name)
        if not isinstance(department, str) or not department.strip():
            raise ValueError("provisioned department is required")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError("Singapore attendance date is required")
        try:
            snapshot = await self._snapshot(fresh=fresh)
        except SourceVerificationError as error:
            return RecordCheck("unavailable", (), datetime.now(UTC), None, error.code)
        indexes = {header: index for index, header in enumerate(snapshot.headers)}
        selected = []
        canonical_rows = []
        for row in snapshot.rows:
            try:
                row_name = normalize_name(row[indexes["[Myinfo] Name"]])
            except ValueError:
                continue
            if row_name != owner or row[indexes["Department"]] != department:
                continue
            raw_timestamp = row[indexes["Timestamp"]]
            match = _TIMESTAMP.fullmatch(raw_timestamp)
            try:
                if match is None:
                    raise ValueError("invalid timestamp syntax")
                timestamp = datetime.strptime(raw_timestamp, "%m/%d/%Y %H:%M").replace(tzinfo=SINGAPORE)
            except ValueError:
                # An anchored m/d/four-digit-year prefix establishes the year even
                # when the rest of a historical timestamp is malformed.
                year = _YEAR.match(raw_timestamp)
                if year is not None and int(year[1]) != day.year:
                    continue
                return RecordCheck("unavailable", (), snapshot.checked_at, None, "record_date_format")
            if timestamp.date() != day:
                continue
            details = tuple(
                (header, row[index])
                for index, header in enumerate(snapshot.headers)
                if header in _DETAIL_HEADERS and row[index]
            )
            selected.append(SubmissionRecord(timestamp, row[indexes["Status"]], details))
            canonical_rows.append(tuple(sorted(zip(snapshot.headers, row, strict=True))))
        return RecordCheck(
            "found" if selected else "not_found",
            tuple(selected),
            snapshot.checked_at,
            _digest(canonical_rows),
        )
