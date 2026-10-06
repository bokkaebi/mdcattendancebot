"""Synthetic owner evidence over loopback HTTP; no production auth bypass."""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
import unittest
import zlib
from dataclasses import FrozenInstanceError
from datetime import date, datetime
from unittest.mock import patch
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from mdcattendance.records import (
    MAX_BODY_BYTES,
    SINGAPORE,
    SourceVerificationError,
    SubmissionRecords,
    normalize_name,
)

DAY = date(2026, 10, 2)
OWNER = "TEST PERSON"
DEPARTMENT = "Synthetic Department"
HEADERS = (
    "Timestamp",
    "[Myinfo] Name",
    "Department",
    "Status",
    "Remarks (Event Name)",
    "Remarks (Location & Reporting Time)",
    "Date of Birth",
)


def row(
    timestamp="10/2/2026 00:32",
    *,
    name=OWNER,
    department=DEPARTMENT,
    status="Other Deployments",
    event="Synthetic Event",
    location="11am Test Venue",
    birth="1/1/2000",
):
    return (timestamp, name, department, status, event, location, birth)


def export(rows=(), *, headers=HEADERS, legend=True):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if legend:
        writer.writerow(["Synthetic attendance legend"])
        writer.writerow([])
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class NormalizeNameTests(unittest.TestCase):
    def test_normalization_preserves_punctuation_and_non_ascii(self):
        self.assertEqual(normalize_name("  tést\tO'Name-李\n PERSON  "), "TÉST O'NAME-李 PERSON")
        self.assertEqual(normalize_name("x" * 200), "X" * 200)
        for invalid in (" \n\t", "x" * 201, "ß" * 101, None, 42):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_name(invalid)


class RecordsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body = export([row()])
        self.status = 200
        self.headers = {"Content-Type": "text/csv"}
        self.requests = []
        self.reply = None
        application = web.Application()
        application.router.add_get("/{path:.*}", self.handle)
        self.server = web.AppRunner(application, access_log=None)
        await self.server.setup()
        await web.TCPSite(self.server, "127.0.0.1", 0).start()
        self.addAsyncCleanup(self.server.cleanup)
        self.base = f"http://127.0.0.1:{self.server.addresses[0][1]}"
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        self.addAsyncCleanup(self.session.close)
        real_get = self.session.get

        def fixture_transport(url, **kwargs):
            # Test-only transport substitution after production URL validation. The
            # implementation itself accepts neither loopback URLs nor an auth bypass.
            self.requests.append(url)
            parsed = urlsplit(url)
            return real_get(f"{self.base}{parsed.path}?{parsed.query}", **kwargs)

        transport = patch.object(self.session, "get", side_effect=fixture_transport)
        transport.start()
        self.addCleanup(transport.stop)
        self.records = SubmissionRecords(session=self.session)

    async def handle(self, request):
        if self.reply is not None:
            return await self.reply(request)
        return web.Response(body=self.body, status=self.status, headers=self.headers)

    async def lookup(self, *, name=OWNER, department=DEPARTMENT, day=DAY, fresh=True):
        return await self.records.lookup(name, department, day, fresh=fresh)

    async def test_exact_owner_and_department_no_substring_or_display_name(self):
        self.body = export(
            [
                row(name="TEST PERSONNER", event="Wrong substring"),
                row(department="Different Department", event="Wrong department"),
                row(name=" test   person ", event="Correct owner"),
                row(name="Different Person", timestamp="not a date"),
            ]
        )
        result = await self.lookup(name=" test\tperson ")
        self.assertEqual(result.status, "found")
        self.assertEqual(
            [dict(record.details)["Remarks (Event Name)"] for record in result.records],
            ["Correct owner"],
        )
        self.assertEqual(result.records[0].timestamp, datetime(2026, 10, 2, 0, 32, tzinfo=SINGAPORE))
        self.assertEqual(result.records[0].status, "Other Deployments")
        self.assertNotIn("Date of Birth", result.to_dict()["records"][0]["details"])
        self.assertNotIn("TEST PERSON", json.dumps(result.to_dict()))
        with self.assertRaises(FrozenInstanceError):
            result.status = "not_found"

    async def test_explicit_month_first_and_midnight_boundaries(self):
        self.body = export(
            [
                row("10/1/2026 23:59", event="Yesterday"),
                row("10/2/2026 00:00", event="First minute"),
                row("10/2/2026 23:59", event="Last minute"),
                row("10/3/2026 00:00", event="Tomorrow"),
                row("2/10/2026 00:32", event="February, not October"),
            ]
        )
        result = await self.lookup()
        self.assertEqual([record.timestamp.hour for record in result.records], [0, 23])
        self.assertEqual(
            [dict(record.details)["Remarks (Event Name)"] for record in result.records],
            ["First minute", "Last minute"],
        )

    async def test_malformed_matching_same_year_or_uncertain_year_blocks(self):
        for timestamp in (
            "13/2/2026 00:32",
            "2/30/2026 00:32",
            "10/2/2026 24:00",
            "10/2/2026 invalid",
            "10/2/26 00:32",
            "garbage",
            "2025-10-02 00:32",
        ):
            with self.subTest(timestamp=timestamp):
                self.body = export([row(), row(timestamp)])
                result = await self.lookup()
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.error_code, "record_date_format")
                self.assertEqual(result.records, ())
                self.assertIsNone(result.digest)

    async def test_only_unambiguous_different_year_can_be_ignored(self):
        self.body = export(
            [
                row("31/12/2025 09:00"),
                row("2/30/2027 bad time"),
                row("1/1/2024"),
                row("not a date", name="UNRELATED PERSON"),
                row(),
            ]
        )
        result = await self.lookup()
        self.assertEqual(result.status, "found")
        self.assertEqual(
            [record.timestamp for record in result.records],
            [datetime(2026, 10, 2, 0, 32, tzinfo=SINGAPORE)],
        )

    async def test_absence_is_distinct_from_unavailability(self):
        self.body = export([row(name="UNRELATED PERSON")])
        absent = await self.lookup()
        self.status = 503
        failed = await self.lookup()
        self.assertEqual((absent.status, absent.records, absent.error_code), ("not_found", (), None))
        self.assertIsNotNone(absent.digest)
        self.assertEqual(
            (failed.status, failed.records, failed.error_code), ("unavailable", (), "http_error")
        )
        self.assertIsNone(failed.digest)

    async def test_quoted_commas_newlines_and_reordered_headers(self):
        original = row(event="Synthetic, Event\nsecond line", location='Venue "B"')
        self.body = export([original])
        first = await self.lookup()
        order = [6, 3, 2, 5, 0, 4, 1]
        self.body = export([tuple(original[i] for i in order)], headers=tuple(HEADERS[i] for i in order))
        second = await self.lookup()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            dict(second.records[0].details),
            {
                "Remarks (Location & Reporting Time)": 'Venue "B"',
                "Remarks (Event Name)": "Synthetic, Event\nsecond line",
            },
        )

    async def test_duplicates_and_digest_complete_rows_not_row_numbers(self):
        a, b = row(), row("10/2/2026 01:00", event="Another Event")
        self.body = export([a, a, b])
        first = await self.lookup()
        self.assertEqual([record.timestamp.minute for record in first.records], [32, 32, 0])
        self.body = export([row(name="UNRELATED PERSON"), b, a, a], legend=False)
        reordered = await self.lookup()
        self.assertEqual(first.digest, reordered.digest)
        self.body = export([a, b])
        single = await self.lookup()
        self.assertNotEqual(first.digest, single.digest)
        self.body = export([row(birth="2/2/2000"), b])
        private_change = await self.lookup()
        self.assertNotEqual(single.digest, private_change.digest)
        self.assertEqual(single.to_dict()["records"], private_change.to_dict()["records"])

    async def test_bad_headers_width_html_encoding_and_truncation_fail_closed(self):
        cases = [
            (b"no header\n", "invalid_headers"),
            (export([], headers=HEADERS[:-1] + ("Timestamp",)), "invalid_headers"),
            (export([]) + export([], legend=False), "invalid_headers"),
            (export([row()[:-1]]), "invalid_row_width"),
            (export([row() + ("extra",)]), "invalid_row_width"),
            (export([]) + b'"unclosed quote', "invalid_csv"),
            (b"<!DOCTYPE html><html>login</html>", "invalid_response"),
            (export([]) + b"\xff", "invalid_encoding"),
        ]
        for body, code in cases:
            with self.subTest(code=code, body=body[:30]):
                self.body = body
                result = await self.lookup()
                self.assertEqual((result.status, result.error_code), ("unavailable", code))
                self.assertEqual(result.records, ())
        self.body = export([row()])
        self.headers = {"Content-Type": "text/html"}
        self.assertEqual((await self.lookup()).error_code, "invalid_response")

    async def test_decoded_size_limit_including_compressed_responses(self):
        for encoding, body in (
            ("identity", b"x" * (MAX_BODY_BYTES + 1)),
            ("gzip", gzip.compress(b"x" * (MAX_BODY_BYTES + 1))),
        ):
            with self.subTest(encoding=encoding):
                self.body = body
                self.headers = {"Content-Type": "text/csv", "Content-Encoding": encoding}
                result = await self.lookup()
                self.assertEqual(
                    (result.status, result.error_code), ("unavailable", "response_too_large")
                )

    async def test_compressed_valid_and_truncated_streams(self):
        for encoding, compress in (("gzip", gzip.compress), ("deflate", zlib.compress)):
            with self.subTest(encoding=encoding):
                self.body = compress(export([row()]))
                self.headers = {"Content-Type": "text/csv", "Content-Encoding": encoding}
                self.assertEqual((await self.lookup()).status, "found")
                self.body = self.body[:-4]
                self.assertEqual((await self.lookup()).error_code, "invalid_response")

    async def test_truncated_network_response_is_not_empty_evidence(self):
        async def truncate(request):
            response = web.StreamResponse(
                headers={"Content-Type": "text/csv", "Content-Length": str(len(self.body) + 100)}
            )
            await response.prepare(request)
            await response.write(self.body)
            request.transport.close()
            return response

        self.reply = truncate
        result = await self.lookup()
        self.assertEqual((result.status, result.error_code), ("unavailable", "network_error"))
        self.assertIsNone(result.digest)

    async def test_redirects_only_allow_google_export_hosts(self):
        async def redirect(request):
            if request.host.startswith("127.0.0.1") and "redirected" not in request.query:
                return web.Response(
                    status=302,
                    headers={
                        "Location": "https://doc-0a-ab-sheets.googleusercontent.com/export?redirected=1"
                    },
                )
            return web.Response(body=self.body, content_type="text/csv")

        self.reply = redirect
        self.assertEqual((await self.lookup()).status, "found")
        self.assertEqual(len(self.requests), 2)
        self.reply = None
        self.status = 302
        for target in (
            "https://accounts.google.com/login",
            "http://docs.google.com/export",
            "https://docs.google.com.evil.example/export",
            "https://docs.google.com:444/export",
            "https://user:password@docs.google.com/export",
            "http://127.0.0.1/",
            "https://unrelated.googleusercontent.com/export",
        ):
            with self.subTest(target=target):
                self.headers = {"Location": target}
                before = len(self.requests)
                result = await self.lookup()
                self.assertEqual(result.error_code, "unsafe_redirect")
                self.assertEqual(len(self.requests), before + 1)

    async def test_cached_ui_fresh_checks_and_cache_expiry(self):
        first = await self.lookup(fresh=False)
        self.body = export([])
        cached = await self.lookup(fresh=False)
        self.assertEqual(
            (cached.status, cached.checked_at, cached.digest), ("found", first.checked_at, first.digest)
        )
        fresh = await self.lookup()
        self.assertEqual(fresh.status, "not_found")
        self.body = export([row()])
        self.records._cached_at -= 31
        self.assertEqual((await self.lookup(fresh=False)).status, "found")

    async def test_concurrent_fresh_fetches_coalesce_and_ui_joins_new_evidence(self):
        await self.lookup()
        self.body = export([])
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(request):
            entered.set()
            await release.wait()
            return web.Response(body=self.body, content_type="text/csv")

        self.reply = wait
        fresh_a = asyncio.create_task(self.lookup())
        await entered.wait()
        fresh_b = asyncio.create_task(self.lookup())
        ui = asyncio.create_task(self.lookup(fresh=False))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(fresh_a, fresh_b, ui)
        self.assertEqual([result.status for result in results], ["not_found"] * 3)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len({result.checked_at for result in results}), 1)

    async def test_cancelled_waiter_does_not_cancel_shared_fetch(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(request):
            entered.set()
            await release.wait()
            return web.Response(body=self.body, content_type="text/csv")

        self.reply = wait
        cancelled = asyncio.create_task(self.lookup())
        await entered.wait()
        survivor = asyncio.create_task(self.lookup())
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release.set()
        self.assertEqual((await survivor).status, "found")
        self.assertEqual(len(self.requests), 1)

    async def test_verify_source_same_representation_and_mismatch(self):
        self.assertTrue(await self.records.verify_source())
        self.assertTrue(all("/gviz/tq?" in url and "tqx=out%3Acsv" in url for url in self.requests))
        self.assertTrue(any("sheet=Viewable" in url for url in self.requests))
        self.assertTrue(any("gid=6355673" in url for url in self.requests))

        async def wrong_sheet(request):
            body = export([row(event="Wrong Sheet")]) if "sheet" in request.query else self.body
            return web.Response(body=body, content_type="text/csv")

        self.reply = wrong_sheet
        with self.assertRaises(SourceVerificationError) as failure:
            await self.records.verify_source()
        self.assertEqual(failure.exception.code, "source_mismatch")
        self.reply = None
        self.status = 403
        with self.assertRaises(SourceVerificationError) as unavailable:
            await self.records.verify_source()
        self.assertEqual(unavailable.exception.code, "http_error")

    async def test_anonymous_session_and_source_constructor_boundaries(self):
        for kwargs in (
            {"sheet_id": "../escape"},
            {"gid": "1&sheet=Other"},
            {"sheet_id": "https://evil.example"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SubmissionRecords(**kwargs)
        async with aiohttp.ClientSession() as cookie_session:
            with self.assertRaises(ValueError):
                SubmissionRecords(session=cookie_session)
        self.session.headers["Authorization"] = "Bearer synthetic-secret"
        with self.assertRaises(ValueError):
            SubmissionRecords(session=self.session)


if __name__ == "__main__":
    unittest.main()
