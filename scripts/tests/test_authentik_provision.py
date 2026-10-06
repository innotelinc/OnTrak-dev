#!/usr/bin/env python3
"""Unit tests for deploy/authentik/provision.py — the pure parts.

Three of these pin down what a live Authentik answered rather than what its
documentation implies, and each one is a way to end up with a provider that
looks correct from the outside and signs nobody in:

* `redirect_uris` is both the field name *and* a list of objects
  (`{matching_mode, url}`). The flat list of strings that superseded
  `allowed_redirect_uris` in 2024.2 has been folded back into the old shape, so
  all three spellings have to be tried or a checkout and an image a release apart
  cannot both provision;
* a provider created without `grant_types` refuses `authorization_code` with
  "Invalid grant_type for provider", which reaches the browser as
  `error=invalid_request` and reads as a callback problem;
* the REST API moved from `/api/v2` to `/api/v3`, so the version is probed and
  not assumed.

The module under test lives outside the package, so it is loaded by path — the
same way scripts/tests/test_cerulean_provision.py loads its subject.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "authentik" / "provision.py"


def _load():
    spec = importlib.util.spec_from_file_location("authentik_provision", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provision = _load()

CALLBACK = "http://localhost:8080/oidc/callback"
FLOWS = ({"pk": "flow-authorize"}, {"pk": "flow-invalidate"})
MAPPINGS = [{"pk": "mapping-openid"}, {"pk": "mapping-groups"}]
KEY = {"pk": "key-signing"}


def provider(**overrides):
    """A provider object as the API returns one, when everything is right."""
    value = {
        "pk": "provider-1",
        "authorization_flow": "flow-authorize",
        "grant_types": ["authorization_code", "refresh_token"],
        "client_id": "ontrak",
        "signing_key": "key-signing",
        "property_mappings": ["mapping-openid", "mapping-groups"],
        "redirect_uris": [{"matching_mode": "strict", "url": CALLBACK}],
    }
    value.update(overrides)
    return value


def drift(overrides):
    return provision.provider_drift(
        provider(**overrides),
        flows=FLOWS,
        mappings=MAPPINGS,
        key=KEY,
        client_id="ontrak",
        redirect_uris=[CALLBACK],
    )


def ensure(api):
    with redirect_stdout(io.StringIO()):  # the provisioner reports what it corrects
        return provision.ensure_provider(
            api,
            flows=FLOWS,
            mappings=MAPPINGS,
            key=KEY,
            client_id="ontrak",
            client_secret="secret",
            redirect_uris=[CALLBACK],
        )


class RedirectUriShapesTest(unittest.TestCase):
    def test_every_spelling_the_releases_have_used_is_tried_in_release_order(self):
        self.assertEqual(
            provision._redirect_uri_shapes([CALLBACK]),
            (
                {"redirect_uris": [{"matching_mode": "strict", "url": CALLBACK}]},
                {"redirect_uris": [CALLBACK]},
                {"allowed_redirect_uris": [{"matching_mode": "strict", "url": CALLBACK}]},
            ),
        )

    def test_redirect_urls_read_both_shapes_the_api_returns(self):
        self.assertEqual(provision.redirect_urls([{"matching_mode": "strict", "url": CALLBACK}]), [CALLBACK])
        self.assertEqual(provision.redirect_urls([CALLBACK]), [CALLBACK])
        self.assertEqual(provision.redirect_urls(None), [])

    def test_a_shape_that_is_refused_moves_on_and_one_that_works_stops(self):
        api = mock.Mock()
        attempts = []

        def send(method, path, body):
            attempts.append(body)
            if "allowed_redirect_uris" not in body:
                raise provision.ProvisionError(
                    'POST /api/v3/providers/oauth2/ -> HTTP 400 {"redirect_uris":["bad shape"]}'
                )
            return {"pk": "provider-1"}

        api.send.side_effect = send
        written = provision.write_provider(api, "POST", "/providers/oauth2/", {"name": "OnTrak"}, [CALLBACK])
        self.assertEqual(written["pk"], "provider-1")
        self.assertEqual(len(attempts), 3, "all three spellings should be tried")
        self.assertIn("allowed_redirect_uris", attempts[-1])

    def test_a_refusal_about_another_field_is_not_retried(self):
        api = mock.Mock()
        api.send.side_effect = provision.ProvisionError(
            'HTTP 400 {"client_id":["provider with this client_id already exists"]}'
        )
        with self.assertRaises(provision.ProvisionError):
            provision.write_provider(api, "POST", "/providers/oauth2/", {}, [CALLBACK])
        self.assertEqual(api.send.call_count, 1)


class ProviderDriftTest(unittest.TestCase):
    def test_a_provider_that_matches_is_left_alone(self):
        self.assertEqual(drift({}), [])

    def test_a_provider_without_grant_types_is_drift(self):
        self.assertIn("grant_types", drift({"grant_types": []}))
        self.assertIn("grant_types", drift({"grant_types": ["client_credentials"]}))

    def test_the_consent_flow_is_drift(self):
        # The local stack takes the no-consent flow; a range that stops at a
        # consent screen nobody asked for is the symptom.
        self.assertIn("authorization_flow", drift({"authorization_flow": "flow-consent"}))

    def test_redirect_uris_are_compared_by_url_not_by_shape(self):
        self.assertEqual(drift({"redirect_uris": [CALLBACK]}), [])
        self.assertIn("redirect_uris", drift({"redirect_uris": []}))
        self.assertIn("redirect_uris", drift({"redirect_uris": [{"url": "http://elsewhere/cb"}]}))

    def test_identity_and_scope_mappings_are_compared(self):
        self.assertIn("client_id", drift({"client_id": "something-else"}))
        self.assertIn("signing_key", drift({"signing_key": "some-other-key"}))
        self.assertIn("property_mappings", drift({"property_mappings": ["mapping-openid"]}))


class EnsureProviderTest(unittest.TestCase):
    def test_a_correct_provider_is_returned_without_writing_to_it(self):
        api = mock.Mock()
        api.one.return_value = provider()
        api.send.side_effect = AssertionError("a correct provider must not be rewritten")
        found = ensure(api)
        self.assertEqual(found["pk"], "provider-1")

    def test_a_provider_missing_grant_types_is_corrected_in_place(self):
        api = mock.Mock()
        api.one.return_value = provider(grant_types=[])
        api.send.return_value = {"pk": "provider-1"}
        ensure(api)
        method, path, body = api.send.call_args[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "/providers/oauth2/provider-1/")
        self.assertEqual(body["grant_types"], ["authorization_code", "refresh_token"])

    def test_a_missing_provider_is_created(self):
        api = mock.Mock()
        api.one.return_value = None
        api.send.return_value = {"pk": "provider-new"}
        ensure(api)
        method, path, body = api.send.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/providers/oauth2/")
        self.assertEqual(body["client_id"], "ontrak")
        self.assertEqual(body["redirect_uris"], [{"matching_mode": "strict", "url": CALLBACK}])


class ApiVersionTest(unittest.TestCase):
    def test_url_carries_the_version_it_was_given(self):
        api = provision.Api("http://auth:9000/", "token", "v3")
        self.assertEqual(api._url("/core/users/me/"), "http://auth:9000/api/v3/core/users/me/")
        self.assertEqual(api._url("/core/users/me/", {"username": "a@b"}), 
                         "http://auth:9000/api/v3/core/users/me/?username=a%40b")

    def test_v3_is_probed_first(self):
        seen = []

        def get(self, path):
            seen.append(self.version)
            return {"pk": 1}

        with mock.patch.object(provision.Api, "get", get):
            self.assertEqual(provision.detect_version("http://auth:9000", "token"), "v3")
        self.assertEqual(seen, ["v3"])

    def test_an_older_instance_falls_back_to_v2(self):
        def get(self, path):
            if self.version == "v3":
                raise provision.ProvisionError("GET /api/v3/... -> HTTP 404")
            return {"pk": 1}

        with mock.patch.object(provision.Api, "get", get):
            self.assertEqual(provision.detect_version("http://auth:9000", "token"), "v2")

    def test_an_instance_that_answers_neither_is_an_error_not_a_guess(self):
        def get(self, path):
            raise provision.ProvisionError("404")

        with (
            mock.patch.object(provision.Api, "get", get),
            self.assertRaises(provision.ProvisionError),
        ):
            provision.detect_version("http://not-authentik:9000", "token")


if __name__ == "__main__":
    unittest.main()
