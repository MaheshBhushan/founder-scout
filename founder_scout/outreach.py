"""Outreach stage: draft an email per founder with Claude and queue it in the tailor tracker.

The drafting rules are the outreach skill's own files (SKILL.md plus the tailor
references it points to), read at runtime so the bot and the skill never drift.
German companies get a working-student pitch; everyone else a remote-role pitch.
When no project in the catalog fits the company, the email falls back to
Mahesh's habit of learning a field from first principles.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import httpx

from .scout import REGION_TIERS, Founder, Scout, Store, domain, page_emails

SKILL_DIR = Path.home() / ".codex" / "skills" / "outreach"
REFERENCES = Path.home() / ".codex" / "skills" / "tailor" / "references"
TRACKER_CONFIG = Path.home() / ".config" / "tailor-tracker"
MODEL = "claude-opus-5-5"

GERMAN_PLACES = [c.lower() for c in REGION_TIERS[0]] + ["germany", "deutschland", "münchen", "köln", "nürnberg", "frankfurt am main"]

TRACKS = {
    "working_student": (
        "Track: WORKING STUDENT (Germany). Mahesh is enrolled in a German master's programme and can work as a "
        "Werkstudent alongside his studies. The ask is whether they take working students, or a short call about it."
    ),
    "remote": (
        "Track: REMOTE (outside Germany). The ask is whether they are open to a remote engineer, intern or contractor "
        "working from Germany, or a short call about it. Do not claim work authorisation in their country."
    ),
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "skip_reason": {"type": "string"},
        "language": {"type": "string", "enum": ["en", "de"]},
        "fit": {"type": "string", "enum": ["project", "fallback"]},
        "projects": {"type": "array", "items": {"type": "string"}},
        "why_them": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "purpose": {"type": "string"},
    },
    "required": ["skip", "skip_reason", "language", "fit", "projects", "why_them", "subject", "body", "purpose"],
    "additionalProperties": False,
}

TASK = """You draft one outreach email from Mahesh Koduri to a startup founder, following the outreach skill below exactly.

The track (working student or remote) is given at the top of the user message.

Project fit:
- Read what the company builds. Pick one or two catalog projects (exact names from the `### name — Language` headings) whose subject matter genuinely overlaps with it, and set fit="project".
- If nothing in the catalog overlaps honestly, set fit="fallback" and projects=[]. The fallback email leads with curiosity: Mahesh learns a field by building it from first principles and refining it until it holds up (for example writing a vector database or an LLM inference engine from scratch to understand them), and he wants to learn their domain the same way. Still cite one such project as evidence, but do not pretend it relates to their product.

Hard rules:
- Every fact about Mahesh comes from the profile or catalog; every fact about the recipient comes from the page text supplied. Never invent metrics, customers or news.
- Body 80 to 150 words, plain text, paragraphs separated by a blank line, ending with the one-line opt-out and the signature (name, mobile, LinkedIn from the profile; add https://mahesh.lol/ only if it helps).
- No links besides LinkedIn and the portfolio. No attachments.
- Set skip=true (with skip_reason) if the page gives you nothing specific to say about why this person, or the person is not a founder or executive.
- why_them: one sentence naming the specific thing from their page the email refers to.
- purpose: one internal line for the dashboard, e.g. "Working-student enquiry; fits vektordb (vector search)".

=== OUTREACH SKILL (SKILL.md) ===
{skill}

=== PROFILE (profile.md) ===
{profile}

=== PROJECT CATALOG (projects.md) ===
{projects}

=== STYLE (style.md) ===
{style}
"""


def track_for(f: Founder) -> str:
    loc = (f.location or "").lower()
    if any(p in loc for p in GERMAN_PLACES):
        return "working_student"
    if not loc and f.company_website and domain(f.company_website).endswith(".de"):
        return "working_student"
    return "remote"


def project_names(catalog: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"^### (.+?) — ", catalog, re.M)}


def excerpt_for(text: str, email: str) -> str:
    """The line of page text that contains the address, trimmed to the tracker's 1000 chars."""
    for line in text.splitlines():
        if email in line.lower():
            line = line.strip()
            if len(line) <= 1000:
                return line
            i = line.lower().index(email)
            return line[max(0, i - 400): i + 400]
    return ""


def tracker_setting(env: str, file: str) -> str:
    value = os.environ.get(env, "")
    if not value and (TRACKER_CONFIG / file).exists():
        value = (TRACKER_CONFIG / file).read_text().strip()
    return value.rstrip("/")


def anthropic_key() -> str:
    """ANTHROPIC_API_KEY from the environment, else read (not copied) from the env file named by ANTHROPIC_ENV_FILE."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    path = Path(os.path.expanduser(os.environ.get("ANTHROPIC_ENV_FILE", "~/.hermes/.env")))
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("ANTHROPIC_API_KEY not set and not found in ANTHROPIC_ENV_FILE")


@dataclass
class Result:
    founder: Founder
    track: str
    decision: str  # queued | missing_evidence | draft_requested | already_contacted | skipped | error | preview
    reason: str = ""
    subject: str = ""
    body: str = ""
    fit: str = ""
    projects: list[str] | None = None
    url: str = ""


class Outreacher:
    def __init__(self, store: Store, scout: Scout, log=print):
        self.store = store
        self.scout = scout
        self.log = log
        self.daily_limit = int(os.environ.get("OUTREACH_DAILY_LIMIT", "8"))
        self.claude = anthropic.AsyncAnthropic(api_key=anthropic_key())
        self.tracker_url = tracker_setting("TAILOR_TRACKER_URL", "url")
        self.tracker_token = tracker_setting("TAILOR_TRACKER_TOKEN", "token")
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS outreach (founder_id INTEGER PRIMARY KEY, track TEXT, decision TEXT, reason TEXT, "
            "fit TEXT, projects TEXT, subject TEXT, body TEXT, url TEXT, at TEXT)"
        )
        store.db.commit()

    def _system(self) -> str:
        catalog = (REFERENCES / "projects.md").read_text()
        return TASK.format(
            skill=(SKILL_DIR / "SKILL.md").read_text(),
            profile=(REFERENCES / "profile.md").read_text(),
            projects=catalog,
            style=(REFERENCES / "style.md").read_text(),
        )

    # ---------- selection ----------

    def sent_last_24h(self) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        return self.store.db.execute("SELECT count(*) FROM outreach WHERE decision='queued' AND at>=?", (since,)).fetchone()[0]

    def room(self) -> int:
        return max(0, self.daily_limit - self.sent_last_24h())

    def pending(self, limit: int) -> list[Founder]:
        rows = self.store.db.execute(
            "SELECT f.* FROM founders f LEFT JOIN outreach o ON o.founder_id=f.id "
            "WHERE f.email != '' AND f.email_source != '' AND (o.founder_id IS NULL OR o.decision='error') "
            "ORDER BY f.email_type='personal' DESC, f.id DESC LIMIT ?",
            (limit,),
        )
        return [Store._row(r) for r in rows]

    def founder(self, founder_id: int) -> Founder | None:
        r = self.store.db.execute("SELECT * FROM founders WHERE id=?", (founder_id,)).fetchone()
        return Store._row(r) if r else None

    def record(self, r: Result) -> None:
        self.store.db.execute(
            "INSERT OR REPLACE INTO outreach VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.founder.id, r.track, r.decision, r.reason, r.fit, json.dumps(r.projects or []), r.subject, r.body, r.url,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self.store.db.commit()

    # ---------- pipeline ----------

    async def draft(self, f: Founder, page_text: str, track: str) -> dict:
        system = self._system()
        catalog_names = project_names((REFERENCES / "projects.md").read_text())
        user = (
            f"Recipient: {f.name}, {f.role} at {f.company or 'unknown company'}\n"
            f"Location: {f.location or 'unknown'}\nCompany website: {f.company_website or 'unknown'}\n"
            f"Email: {f.email} (listed on {f.email_source})\n"
            f"Company description (from scraping): {f.company_description}\nBio: {f.bio}\n\n"
            f"=== PAGE TEXT from {f.email_source} ===\n{page_text[:12000]}"
        )
        resp = await self.claude.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
            # The skill and references are identical across founders, so cache them.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": f"{TRACKS[track]}\n\n{user}"}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined to draft")
        text = next(b.text for b in resp.content if b.type == "text")
        d = json.loads(text)
        d["projects"] = [p for p in d["projects"] if p in catalog_names]
        if d["fit"] == "project" and not d["projects"]:
            d["fit"] = "fallback"
        return d

    async def prepare(self, f: Founder, send: bool) -> tuple[Result, dict | None]:
        track = track_for(f)
        # Re-fetch the page: the address must still be printed there, and the excerpt comes from the live page.
        data = await self.scout.scrape(f.email_source, extract=False)
        text = data.get("markdown") or ""
        if f.email.lower() not in page_emails(text):
            return Result(f, track, "skipped", "address no longer on its source page"), None
        d = await self.draft(f, text, track)
        if d["skip"]:
            return Result(f, track, "skipped", d["skip_reason"]), None
        words = len(d["body"].split())
        if not 60 <= words <= 190:
            return Result(f, track, "skipped", f"draft body was {words} words"), None
        payload = {
            "to_email": f.email.lower(),
            "to_name": f.name,
            "to_company": f.company or None,
            "to_role": f.role or None,
            "purpose": d["purpose"][:500],
            "source_url": f.email_source,
            "source_excerpt": excerpt_for(text, f.email.lower()),
            "campaign": f"founder-scout-{track}",
            "subject": d["subject"][:300],
            "body": d["body"],
            "send": send,
        }
        return Result(f, track, "preview", d["why_them"], d["subject"], d["body"], d["fit"], d["projects"]), payload

    async def post(self, payload: dict) -> tuple[str, str]:
        r = await self.scout.http.post(
            f"{self.tracker_url}/api/outreach",
            json=payload,
            # Cloudflare answers default library user agents with error 1010.
            headers={"authorization": f"Bearer {self.tracker_token}", "user-agent": "founder-scout/1"},
        )
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                detail = "; ".join(err.get("details") or []) or err.get("message", "")
            except (ValueError, KeyError):
                detail = r.text[:200]
            raise RuntimeError(f"tracker HTTP {r.status_code}: {detail}")
        body = r.json()
        return body["outreach"], body.get("url", "")

    async def process(self, f: Founder, send: bool = True, dry_run: bool = False) -> Result:
        try:
            result, payload = await self.prepare(f, send)
            if payload and not dry_run:
                result.decision, result.url = await self.post(payload)
        except (httpx.HTTPError, anthropic.APIError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as e:
            result = Result(f, track_for(f), "error", str(e)[:300])
        if not dry_run:
            self.record(result)
        self.log(f"outreach {f.name} <{f.email}> [{result.track}/{result.fit or '-'}]: {result.decision} {result.reason}")
        return result

    async def run(self, limit: int | None = None) -> list[Result]:
        """Process pending founders within the daily limit."""
        n = min(self.room(), limit if limit is not None else self.daily_limit)
        results = []
        for f in self.pending(n):
            results.append(await self.process(f))
        return results
