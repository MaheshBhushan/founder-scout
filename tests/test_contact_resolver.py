"""Boundary regressions with in-memory HTTP transports; never contact real services."""

import copy
import json
import unittest
from unittest.mock import patch

import httpx

from founder_scout.contact_resolver import (
    ManifestError, REACHER_FIELDS, candidate_emails, resolve_contact,
    validate_manifest, verification_reasons,
)


SETTINGS = {
    "REACHER_URL": "https://reacher.test", "REACHER_API_KEY": "reacher-test-secret",
    "TAILOR_TRACKER_URL": "https://tracker.test", "TAILOR_TRACKER_TOKEN": "tracker-test-secret",
}


def manifest():
    return {
        "contact": {
            "name": "Anna Schmidt", "domain": "company.test", "company": "Company",
            "role": "Engineering Manager", "location": "Berlin",
            "source_url": "https://company.test/team",
            "source_excerpt": "Anna Schmidt is the Engineering Manager at Company.",
        },
        "outreach": {"subject": "Engineering enquiry", "body": "Please point me to the hiring contact.",
                     "purpose": "Application referral"},
    }


def reacher_result(email, safe=True):
    result = {"input": email, "is_reachable": "safe" if safe else "invalid"}
    for section, key, expected in REACHER_FIELDS:
        result.setdefault(section, {})[key] = expected
    if not safe:
        result["smtp"]["is_deliverable"] = False
    return result


class ManifestTests(unittest.TestCase):
    def test_requires_literal_identity_and_official_source(self):
        for key, value in (("name", "Other Person"), ("source_url", "ftp://company.test/team"),
                           ("source_url", "https://company.test.attacker.test/team"),
                           ("source_url", "https://attacker.test/team"), ("company", ""), ("role", "")):
            with self.subTest(key=key, value=value):
                data = manifest()
                data["contact"][key] = value
                with self.assertRaises(ManifestError):
                    validate_manifest(data)

    def test_rejects_invalid_domains_and_cross_domain_addresses(self):
        for host in ("https://company.test", "company", "company.test:80", "-bad.test", "127.0.0.1"):
            with self.subTest(host=host):
                data = manifest()
                data["contact"]["domain"] = host
                with self.assertRaises(ManifestError):
                    validate_manifest(data)
        data = manifest()
        data["emails"] = ["anna@other.test"]
        with self.assertRaises(ManifestError):
            validate_manifest(data)

    def test_accents_bounds_dedup_and_two_part_rule(self):
        data = manifest()
        data["contact"].update(name="Ánna Schmidté", source_excerpt="Ánna Schmidté works at Company.")
        data["emails"] = ["anna.schmidte@company.test", "anna.schmidte@company.test"]
        candidates = candidate_emails(validate_manifest(data))
        self.assertEqual(candidates[0], "anna.schmidte@company.test")
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertIn("a.schmidte@company.test", candidates)
        data["emails"] = [f"person{i}@company.test" for i in range(8)]
        self.assertEqual(len(candidate_emails(validate_manifest(data))), 8)
        data["contact"].update(name="Anna", source_excerpt="Anna works at Company.")
        data["emails"] = []
        with self.assertRaises(ManifestError):
            candidate_emails(validate_manifest(data))
        data["emails"] = ["anna@company.test"]
        self.assertEqual(candidate_emails(validate_manifest(data)), ["anna@company.test"])

    def test_queue_requires_outreach(self):
        data = manifest()
        del data["outreach"]
        with self.assertRaises(ManifestError):
            validate_manifest(data, queue=True)

    def test_strict_predicate_checks_every_flag_and_exact_input(self):
        email = "anna.schmidt@company.test"
        result = reacher_result(email)
        self.assertEqual(verification_reasons(email, result), [])
        for section, key, expected in REACHER_FIELDS:
            for value in (not expected, None, int(expected)):
                with self.subTest(section=section, key=key, value=value):
                    changed = copy.deepcopy(result)
                    changed[section][key] = value
                    self.assertTrue(verification_reasons(email, changed))
            changed = copy.deepcopy(result)
            del changed[section][key]
            self.assertTrue(verification_reasons(email, changed))
        result["input"] = "other@company.test"
        self.assertIn("input_mismatch", verification_reasons(email, result))


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def resolve(self, handler, data=None, queue=False, settings=None):
        settings = SETTINGS if settings is None else settings
        with patch("founder_scout.contact_resolver.tracker_setting", side_effect=lambda key, file: settings.get(key, "")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await resolve_contact(data or manifest(), queue, http=client)

    async def test_unique_safe_checks_all_candidates_and_queues_once(self):
        checked, queued = [], []

        def handler(request):
            payload = json.loads(request.content)
            if request.url.host == "reacher.test":
                email = payload["to_email"]
                checked.append(email)
                return httpx.Response(200, json=reacher_result(email, email == "anna.schmidt@company.test"))
            queued.append(payload)
            return httpx.Response(200, json={"outreach": "queued", "url": "https://tracker.test/outreach/1"})

        result = await self.resolve(handler, queue=True)
        self.assertEqual(result["decision"], "queued")
        self.assertEqual(len(queued), 1)
        self.assertEqual(result["selected_address"], "anna.schmidt@company.test")
        self.assertEqual(checked, candidate_emails(validate_manifest(manifest())))
        self.assertEqual(queued[0], result["payload"])
        self.assertIs(queued[0]["send"], True)
        self.assertNotIn("send_at", queued[0])
        self.assertNotIn("timezone", result)
        self.assertEqual(queued[0]["campaign"], "tailor-contact-fallback")
        self.assertEqual(queued[0]["email_verification"], {
            "method": "reacher", "name": "Anna Schmidt", "domain": "company.test",
        })

    async def test_no_queue_returns_same_immediate_payload_without_posting_tracker(self):
        def handler(request):
            self.assertEqual(request.url.host, "reacher.test")
            email = json.loads(request.content)["to_email"]
            return httpx.Response(200, json=reacher_result(email, email == "anna.schmidt@company.test"))

        result = await self.resolve(handler)
        self.assertEqual(result["decision"], "verified")
        self.assertIs(result["payload"]["send"], True)
        self.assertNotIn("send_at", result["payload"])
        self.assertNotIn("timezone", result)
        self.assertNotIn("tracker", result)

    async def test_early_research_needs_no_outreach_draft(self):
        data = manifest()
        del data["outreach"]

        def handler(request):
            self.assertEqual(request.url.host, "reacher.test")
            email = json.loads(request.content)["to_email"]
            return httpx.Response(200, json=reacher_result(email, email == "anna.schmidt@company.test"))

        result = await self.resolve(handler, data=data)
        self.assertEqual(result["decision"], "verified")
        self.assertEqual(result["selected_address"], "anna.schmidt@company.test")
        self.assertNotIn("payload", result)
        self.assertNotIn("tracker", result)

    async def test_multiple_safe_is_ambiguous_and_never_queues(self):
        def handler(request):
            self.assertEqual(request.url.host, "reacher.test")
            return httpx.Response(200, json=reacher_result(json.loads(request.content)["to_email"]))
        result = await self.resolve(handler, queue=True)
        self.assertEqual(result["decision"], "ambiguous")
        self.assertIsNone(result["selected_address"])
        self.assertNotIn("payload", result)

    async def test_catchall_and_unknown_never_queue(self):
        for kind in ("catchall", "risky", "unknown", "mismatched", "missing_flag"):
            def handler(request):
                self.assertEqual(request.url.host, "reacher.test")
                result = reacher_result(json.loads(request.content)["to_email"])
                if kind == "catchall":
                    result["smtp"]["is_catch_all"] = True
                elif kind == "risky":
                    result["is_reachable"] = "risky"
                elif kind == "unknown":
                    result["is_reachable"] = "unknown"
                elif kind == "mismatched":
                    result["input"] = "someone.else@company.test"
                else:
                    del result["smtp"]["is_catch_all"]
                return httpx.Response(200, json=result)
            with self.subTest(kind=kind):
                result = await self.resolve(handler, queue=True)
                self.assertEqual(result["decision"], "verification_failed")
                self.assertIsNone(result["selected_address"])

    async def test_missing_service_or_one_timeout_blocks_safe_selection(self):
        def forbidden(request):
            self.fail("missing Reacher must not contact any service")
        result = await self.resolve(forbidden, queue=True, settings={})
        self.assertEqual(result["decision"], "verification_unavailable")

        def handler(request):
            self.assertEqual(request.url.host, "reacher.test")
            email = json.loads(request.content)["to_email"]
            if email == "annaschmidt@company.test":
                raise httpx.ReadTimeout("test timeout", request=request)
            return httpx.Response(200, json=reacher_result(email, email == "anna.schmidt@company.test"))
        result = await self.resolve(handler, queue=True)
        self.assertEqual(result["decision"], "verification_unavailable")
        self.assertIsNone(result["selected_address"])

    async def test_generic_is_not_checked_and_published_address_is_checked(self):
        data = manifest()
        data["emails"] = ["info@company.test", "anna.schmidt@company.test"]
        data["contact"]["source_excerpt"] += " Reach Anna Schmidt at anna.schmidt@company.test."
        checked = []

        def handler(request):
            email = json.loads(request.content)["to_email"]
            checked.append(email)
            return httpx.Response(200, json=reacher_result(email, email == "anna.schmidt@company.test"))
        result = await self.resolve(handler, data=data)
        self.assertEqual(result["decision"], "verified")
        self.assertNotIn("info@company.test", checked)
        self.assertIn("anna.schmidt@company.test", checked)
        self.assertEqual(result["candidates"][0]["reasons"], ["generic_inbox"])
        self.assertNotIn("tracker", result)


if __name__ == "__main__":
    unittest.main()
