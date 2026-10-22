"""LLM-drafted outreach must not be queued for sending without a human confirmation."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from founder_scout.outreach import Outreacher, Result
from founder_scout.scout import Founder, Store


def founder() -> Founder:
    return Founder(name="Ada Example", role="Founder", company="Example", email="ada@example.test",
                   email_source="https://example.test/team", id=7)


class OutreachConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Outreacher(Store(str(Path(self.tmp.name) / "db.sqlite")), scout=None, log=lambda _: None)
        self.posted: list[dict] = []

        async def prepare(f, send):
            return Result(f, "remote", "preview", "why", "Subject", "Body"), {"send": send}

        async def post(payload):
            self.posted.append(payload)
            return ("queued" if payload["send"] else "draft_requested"), "https://tracker.test/x"

        self.out.prepare = prepare  # type: ignore[method-assign]
        self.out.post = post  # type: ignore[method-assign]
        self.out.company_contacted = lambda f: False  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_process(self, **kwargs) -> Result:
        return asyncio.run(self.out.process(founder(), **kwargs))

    def test_without_confirmation_only_a_draft_is_requested(self) -> None:
        self.assertEqual(self.run_process().decision, "draft_requested")
        self.assertEqual([p["send"] for p in self.posted], [False])

    def test_declined_confirmation_posts_nothing(self) -> None:
        async def no(_):
            return False
        self.assertEqual(self.run_process(confirm=no).decision, "cancelled")
        self.assertEqual(self.posted, [])

    def test_confirmed_draft_is_queued_for_sending(self) -> None:
        seen: list[str] = []

        async def yes(r):
            seen.append(r.body)
            return True
        self.assertEqual(self.run_process(confirm=yes).decision, "queued")
        self.assertEqual(seen, ["Body"])
        self.assertEqual([p["send"] for p in self.posted], [True])


if __name__ == "__main__":
    unittest.main()
