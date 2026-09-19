#!/usr/bin/env python3
"""Unit tests for cerulean-provision.py — the pure parts, which is where the
mistakes in this kind of script actually live.

Three of these pin down something that was measured rather than assumed, and
each one is a way to break a live range silently:

* a wildcard certificate covers `student.ontrak.innotel.us` and *not* the apex,
  so a plan that trusts one certificate for all three names serves the range's
  own name over a certificate that does not include it;
* Cerulean's certificate request spells a wildcard as `domain: <base>` plus
  `wildcard: true` — a literal `*` in `domain` is refused outright;
* an edge host is matched on its whole name, because `admin.ontrak…` and
  `admin-old.ontrak…` share a prefix and repointing the wrong one is a range
  going dark.

The module under test has a hyphen in its name, so it is loaded by path.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "cerulean-provision.py"


def _load():
    spec = importlib.util.spec_from_file_location("cerulean_provision", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provision = _load()


def cert(**overrides) -> dict:
    data = {
        "id": 7,
        "domain": "ontrak.innotel.us",
        "wildcard": False,
        "status": "issued",
        "hasMaterial": True,
        "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=60)).isoformat(),
    }
    data.update(overrides)
    return data


class TheBridgePathsAreRight(unittest.TestCase):
    """The one shape that is not a straight prefix swap."""

    def api(self, zone: str = "") -> object:
        api = provision.Api("https://cerulean.example", "ceru" + "_" + "a" * 16 + "_" + "b" * 8)
        api.zone = zone
        return api

    def test_the_records_route_is_zone_addressed(self):
        self.assertEqual(
            self.api("innotel.us")._path("/api/dns/records"),
            "/api/service/dns/records?zone=innotel.us",
        )

    def test_a_zone_that_is_not_known_yet_is_a_refusal_not_a_bad_request(self):
        with self.assertRaises(provision.CannotRun):
            self.api()._path("/api/dns/records")

    def test_the_other_routes_are_prefix_swaps(self):
        api = self.api("innotel.us")
        self.assertEqual(api._path("/api/domains"), "/api/service/domains")
        self.assertEqual(api._path("/api/certificates/12"), "/api/service/certificates/12")
        self.assertEqual(api._path("/api/npm/hosts"), "/api/service/npm/hosts")

    def test_a_session_only_route_is_named_rather_than_passed_through(self):
        # Passing it through would send a service key at a session-only route and
        # come back 401, which says nothing about the cause.
        with self.assertRaises(provision.CannotRun):
            self.api("innotel.us")._path("/api/users")


class NamesAreZoneRelative(unittest.TestCase):
    """Technitium writes zone-relative names and reads back FQDNs."""

    def test_subdomain_keeps_its_own_label(self):
        self.assertEqual(
            provision.zone_relative("student.ontrak.innotel.us", "innotel.us"), "student.ontrak"
        )

    def test_apex_keeps_the_first_label(self):
        self.assertEqual(provision.zone_relative("ontrak.innotel.us", "innotel.us"), "ontrak")

    def test_a_name_outside_the_zone_is_left_alone(self):
        self.assertEqual(provision.zone_relative("example.com", "innotel.us"), "example.com")

    def test_trailing_dots_and_case_do_not_matter(self):
        self.assertEqual(provision.zone_relative("Admin.Ontrak.Innotel.US.", "innotel.us"), "admin.ontrak")


class RecordsAreMatchedByTheirPayload(unittest.TestCase):
    def test_technitium_spells_it_rddata(self):
        self.assertEqual(provision.record_value({"rData": "73.68.203.71"}), "73.68.203.71")

    def test_other_responses_say_value(self):
        self.assertEqual(provision.record_value({"value": "73.68.203.71"}), "73.68.203.71")

    def test_the_fqdn_matches_even_though_the_create_call_takes_a_relative_name(self):
        records = [{"name": "student.ontrak.innotel.us.", "type": "A", "rData": "1.2.3.4"}]
        found = provision.find_record(records, "student.ontrak.innotel.us", "A")
        self.assertIsNotNone(found)
        self.assertEqual(provision.record_value(found), "1.2.3.4")

    def test_a_different_type_is_a_different_record(self):
        records = [{"name": "ontrak.innotel.us", "type": "CNAME", "rData": "elsewhere"}]
        self.assertIsNone(provision.find_record(records, "ontrak.innotel.us", "A"))


class WildcardsAreSpelledTheWayCeruleanAccepts(unittest.TestCase):
    def test_a_wildcard_sends_the_base_and_a_flag(self):
        self.assertEqual(
            provision.certificate_body("*.ontrak.innotel.us"),
            {"domain": "ontrak.innotel.us", "wildcard": True, "name": "*.ontrak.innotel.us"},
        )

    def test_an_exact_name_sends_itself(self):
        self.assertEqual(
            provision.certificate_body("ontrak.innotel.us"),
            {"domain": "ontrak.innotel.us", "name": "ontrak.innotel.us"},
        )


class CoverageDecidesWhetherANameHasTls(unittest.TestCase):
    """RFC 6125, and the one place Cerulean's row is deliberately generous."""

    def test_a_bare_wildcard_covers_a_subdomain_and_not_the_base(self):
        names = ["*.ontrak.innotel.us"]
        self.assertTrue(provision.covers(names, "student.ontrak.innotel.us"))
        self.assertTrue(provision.covers(names, "admin.ontrak.innotel.us"))
        self.assertFalse(provision.covers(names, "ontrak.innotel.us"))

    def test_a_bare_wildcard_covers_one_label_only(self):
        self.assertFalse(provision.covers(["*.ontrak.innotel.us"], "a.b.ontrak.innotel.us"))

    def test_a_wildcard_row_from_cerulean_also_names_its_base(self):
        # Cerulean stores `domain: <base>` + `wildcard: true` and the row is read
        # through the flag, so the base is one of the names. If that ever stops
        # being true, this is the test that says the apex lost its coverage.
        names = provision.certificate_names(cert(wildcard=True))
        self.assertIn("ontrak.innotel.us", names)
        self.assertIn("*.ontrak.innotel.us", names)

    def test_an_exact_certificate_covers_only_its_own_name(self):
        names = provision.certificate_names(cert())
        self.assertTrue(provision.covers(names, "ontrak.innotel.us"))
        self.assertFalse(provision.covers(names, "student.ontrak.innotel.us"))

    def test_a_name_list_from_cerulean_is_used_as_is(self):
        names = provision.certificate_names(cert(domains=["student.ontrak.innotel.us"]))
        self.assertTrue(provision.covers(names, "student.ontrak.innotel.us"))


class ExpiryIsReadInBothSpellings(unittest.TestCase):
    """Cerulean hands back OpenSSL's `notAfter`, not ISO 8601 — measured on the
    live estate, where the ISO-only parser made every plan want to reissue."""

    def test_openssl_not_after_is_utc(self):
        when = provision.parse_expiry("Dec 17 19:14:45 2026 GMT")
        self.assertEqual(when, dt.datetime(2026, 12, 17, 19, 14, 45, tzinfo=dt.timezone.utc))

    def test_openssl_pads_a_single_digit_day_with_two_spaces(self):
        when = provision.parse_expiry("Dec  7 19:14:45 2026 GMT")
        self.assertEqual(when, dt.datetime(2026, 12, 7, 19, 14, 45, tzinfo=dt.timezone.utc))

    def test_iso_8601_still_works(self):
        when = provision.parse_expiry("2026-12-17T19:14:45Z")
        self.assertEqual(when, dt.datetime(2026, 12, 17, 19, 14, 45, tzinfo=dt.timezone.utc))

    def test_a_naive_iso_value_is_taken_as_utc(self):
        when = provision.parse_expiry("2026-12-17T19:14:45")
        self.assertEqual(when.tzinfo, dt.timezone.utc)

    def test_nonsense_is_none(self):
        self.assertIsNone(provision.parse_expiry("whenever"))
        self.assertIsNone(provision.parse_expiry(""))


class ACertificateIsReusedOrReissued(unittest.TestCase):
    def test_a_healthy_wildcard_is_reused(self):
        chosen = provision.select_certificate([cert(wildcard=True)], "student.ontrak.innotel.us", 21)
        self.assertEqual(chosen["id"], 7)

    def test_a_wildcard_with_an_openssl_expiry_is_reused(self):
        # The exact row shape the live Cerulean returned for certificate #31.
        live = cert(
            id=31,
            wildcard=True,
            domains=["ontrak.innotel.us", "*.ontrak.innotel.us"],
            expiresAt="Dec 17 19:14:37 2026 GMT",
        )
        far_from_it = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
        chosen = provision.select_certificate([live], "student.ontrak.innotel.us", 21, now=far_from_it)
        self.assertEqual(chosen["id"], 31)

    def test_an_unparseable_expiry_is_not_reused(self):
        broken = cert(wildcard=True, expiresAt="whenever")
        self.assertIsNone(provision.select_certificate([broken], "student.ontrak.innotel.us", 21))

    def test_one_that_is_about_to_expire_is_not_reused(self):
        soon = cert(
            wildcard=True,
            expiresAt=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).isoformat(),
        )
        self.assertIsNone(provision.select_certificate([soon], "student.ontrak.innotel.us", 21))

    def test_an_unissued_certificate_is_not_reused(self):
        pending = cert(wildcard=True, status="pending", hasMaterial=False)
        self.assertIsNone(provision.select_certificate([pending], "student.ontrak.innotel.us", 21))

    def test_the_exact_match_wins_over_the_wildcard(self):
        # The range's own name should not borrow the certificate that exists for
        # its two subdomains when it has one of its own.
        wildcard = cert(id=2, wildcard=True)
        exact = cert(id=1, domain="ontrak.innotel.us")
        chosen = provision.select_certificate([wildcard, exact], "ontrak.innotel.us", 21)
        self.assertEqual(chosen["id"], 1)

    def test_a_wildcard_is_still_used_for_the_subdomains(self):
        wildcard = cert(id=2, wildcard=True)
        exact = cert(id=1, domain="ontrak.innotel.us")
        chosen = provision.select_certificate([wildcard, exact], "student.ontrak.innotel.us", 21)
        self.assertEqual(chosen["id"], 2)


class EdgeHostsAreMatchedOnTheWholeName(unittest.TestCase):
    def test_the_exact_name_matches(self):
        hosts = [{"id": 3, "domain_names": ["admin.ontrak.innotel.us"]}]
        self.assertEqual(provision.find_proxy_host(hosts, "admin.ontrak.innotel.us")["id"], 3)

    def test_a_longer_name_with_the_same_prefix_does_not(self):
        hosts = [{"id": 3, "domain_names": ["admin-old.ontrak.innotel.us"]}]
        self.assertIsNone(provision.find_proxy_host(hosts, "admin.ontrak.innotel.us"))

    def test_an_empty_host_list_is_not_an_error(self):
        self.assertIsNone(provision.find_proxy_host([], "ontrak.innotel.us"))


class WithoutAKeyItSaysWhereToGetOne(unittest.TestCase):
    """The service key is the only unattended door, so the refusal has to name it."""

    def setUp(self):
        self._saved = dict(provision.os.environ)
        provision.os.environ.pop("CERULEAN_API_TOKEN", None)

    def tearDown(self):
        provision.os.environ.clear()
        provision.os.environ.update(self._saved)

    def test_no_key_refuses_with_instructions(self):
        args = provision.argparse.Namespace(
            apply=False, repoint=False, wait=1, renew_days=21
        )
        with self.assertRaises(provision.CannotRun) as caught:
            provision.plan(args)
        self.assertIn("Service API keys", str(caught.exception))

    def test_something_that_is_not_a_service_key_is_refused(self):
        provision.os.environ["CERULEAN_API_TOKEN"] = "not-a-" + "key"
        args = provision.argparse.Namespace(
            apply=False, repoint=False, wait=1, renew_days=21
        )
        with self.assertRaises(provision.CannotRun) as caught:
            provision.plan(args)
        self.assertIn("ceru_", str(caught.exception))


class ADryRunWritesNothing(unittest.TestCase):
    """The default is a plan, and a plan must not mutate the estate."""

    class FakeApi:
        def __init__(self, *_, **__):
            self.zone = ""
            self.registered = False
            self.writes: list[tuple[str, str]] = []

        def get_list(self, path, what):
            return []

        def call(self, path, method="GET", body=None, timeout=60):
            if method != "GET":
                self.writes.append((method, path))
            return (200, [] if method == "GET" else {})

    def setUp(self):
        self._saved = dict(provision.os.environ)
        self._real_api = provision.Api
        provision.os.environ["CERULEAN_API_TOKEN"] = "ceru" + "_" + "a" * 8 + "_" + "b" * 8
        self.fake = self.FakeApi()
        provision.Api = lambda *a, **k: self.fake

    def tearDown(self):
        # Restore the class as well as the environment: a leaked patch is how the
        # next test ends up asserting against a stub it never asked for.
        provision.Api = self._real_api
        provision.os.environ.clear()
        provision.os.environ.update(self._saved)

    def test_the_plan_names_all_three_hosts_and_writes_nothing(self):
        args = provision.argparse.Namespace(apply=False, repoint=False, wait=1, renew_days=21)
        out = io.StringIO()
        with redirect_stdout(out):
            provision.plan(args)
        printed = out.getvalue()
        for name in ("ontrak.innotel.us", "student.ontrak.innotel.us", "admin.ontrak.innotel.us"):
            self.assertIn(name, printed)
        self.assertEqual(self.fake.writes, [], "a dry run must not write")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
