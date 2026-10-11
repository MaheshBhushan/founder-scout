"""Outreach stage: draft an email per founder with Codex and queue it in the tailor tracker.

Drafting runs `codex exec` with the $outreach skill, so Codex reads the skill's
SKILL.md and the tailor references itself and the bot never drifts from them.
German companies get a working-student pitch; everyone else a remote-role pitch.
When no project in the catalog fits the company, the email falls back to
Mahesh's habit of learning a field from first principles.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .scout import REGION_TIERS, Founder, Scout, Store, domain, page_emails

SKILL_DIR = Path.home() / ".codex" / "skills" / "outreach"
REFERENCES = Path.home() / ".codex" / "skills" / "tailor" / "references"
TRACKER_CONFIG = Path.home() / ".config" / "tailor-tracker"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
CODEX_EFFORT = os.environ.get("CODEX_EFFORT", "medium")
CODEX_TIMEOUT = 600

GERMAN_PLACES = [c.lower() for c in REGION_TIERS[0]] + ["germany", "deutschland", "münchen", "köln", "nürnberg", "frankfurt am main"]

# Time zone per place, matched as a substring of the founder's location (first match wins),
# then by the address or website country domain. Unknown places default to Berlin.
PLACE_TZ = [
    (("germany", "deutschland", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne", "köln", "stuttgart",
      "düsseldorf", "leipzig", "karlsruhe", "dresden", "aachen", "nuremberg", "nürnberg"), "Europe/Berlin"),
    (("london", "united kingdom", " uk", "england", "scotland"), "Europe/London"),
    (("dublin", "ireland"), "Europe/Dublin"), (("lisbon", "portugal"), "Europe/Lisbon"),
    (("paris", "france"), "Europe/Paris"), (("amsterdam", "netherlands"), "Europe/Amsterdam"),
    (("brussels", "belgium"), "Europe/Brussels"), (("zurich", "zürich", "switzerland"), "Europe/Zurich"),
    (("vienna", "wien", "austria"), "Europe/Vienna"), (("madrid", "barcelona", "spain"), "Europe/Madrid"),
    (("milan", "italy"), "Europe/Rome"), (("copenhagen", "denmark"), "Europe/Copenhagen"),
    (("stockholm", "sweden"), "Europe/Stockholm"), (("oslo", "norway"), "Europe/Oslo"),
    (("helsinki", "finland"), "Europe/Helsinki"), (("tallinn", "estonia"), "Europe/Tallinn"),
    (("warsaw", "poland"), "Europe/Warsaw"), (("prague", "czech"), "Europe/Prague"),
    (("san francisco", "bay area", "palo alto", "los angeles", "seattle", "california"), "America/Los_Angeles"),
    (("new york", "boston", "toronto", "miami"), "America/New_York"), (("austin", "texas", "chicago"), "America/Chicago"),
    (("mexico",), "America/Mexico_City"), (("são paulo", "sao paulo", "brazil"), "America/Sao_Paulo"),
    (("bangalore", "bengaluru", "india", "mumbai", "delhi", "hyderabad"), "Asia/Kolkata"),
    (("singapore",), "Asia/Singapore"), (("tel aviv", "israel"), "Asia/Jerusalem"), (("dubai", "uae"), "Asia/Dubai"),
    (("lagos", "nigeria"), "Africa/Lagos"), (("nairobi", "kenya"), "Africa/Nairobi"),
    (("cape town", "south africa"), "Africa/Johannesburg"), (("sydney", "australia"), "Australia/Sydney"),
    (("tokyo", "japan"), "Asia/Tokyo"), (("seoul", "korea"), "Asia/Seoul"), (("jakarta", "indonesia"), "Asia/Jakarta"),
]
TLD_TZ = {"de": "Europe/Berlin", "at": "Europe/Vienna", "ch": "Europe/Zurich", "uk": "Europe/London", "fr": "Europe/Paris",
          "nl": "Europe/Amsterdam", "es": "Europe/Madrid", "it": "Europe/Rome", "se": "Europe/Stockholm", "in": "Asia/Kolkata",
          "sg": "Asia/Singapore", "il": "Asia/Jerusalem", "jp": "Asia/Tokyo", "br": "America/Sao_Paulo", "ca": "America/Toronto"}
# Send window in the recipient's local time, Monday to Friday; slots every SLOT_MINUTES.
MORNING_START, MORNING_END, SLOT_MINUTES, HORIZON_DAYS = (8, 0), (11, 0), 5, 3


def tz_for(f: Founder) -> str:
    loc = f" {(f.location or '').lower()}"
    for places, tz in PLACE_TZ:
        if any(p in loc for p in places):
            return tz
    for host in (f.email.split("@")[-1], domain(f.company_website or "")):
        tld = host.rsplit(".", 1)[-1] if host else ""
        if tld in TLD_TZ:
            return TLD_TZ[tld]
    return "Europe/Berlin"


# Shared inboxes that never belong to a job or collaboration enquiry.
ROLE_INBOXES = {"sales", "support", "press", "media", "jobs", "careers", "karriere", "bewerbung", "hr", "privacy", "datenschutz", "billing", "legal", "abuse", "security"}

# Appended by code so every email carries the same links. The tracker renders
# [text](url) as a hyperlink in the HTML part and as "text: url" in plain text.
SIGNATURE = (
    "Mahesh Koduri\n+49 1551 0823544\n"
    "[Portfolio](https://mahesh.lol/) | [LinkedIn](https://linkedin.com/in/maheshkoduri/) | [GitHub](https://github.com/MaheshBhushan)"
)

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

TASK = """Use the $outreach skill to draft one outreach email from Mahesh Koduri to a startup founder. Read its SKILL.md and the tailor references it names (profile.md, projects.md, style.md) and follow them exactly.

You only draft. Do not run send.py, do not post to the tracker, do not fetch pages or browse: the founder's page text is supplied below and is the only evidence about the recipient. Return the JSON object described by the output schema.

The track (working student or remote) is given at the top of the founder details.

Project fit:
- Read what the company builds. Pick one or two projects from projects.md (exact names from the `### name — Language` headings) whose subject matter genuinely overlaps with it, and set fit="project".
- If nothing in the catalog overlaps honestly, set fit="fallback" and projects=[]. The fallback email leads with curiosity: Mahesh learns a field by building it from first principles and refining it until it holds up (for example writing a vector database or an LLM inference engine from scratch to understand them), and he wants to learn their domain the same way. Still cite one such project as evidence, but do not pretend it relates to their product.

Hard rules:
- Every fact about Mahesh comes from the profile or catalog; every fact about the recipient comes from the page text supplied. Never invent metrics, customers or news.
- Body 70 to 140 words, plain text, paragraphs separated by a blank line, ending with the one-line opt-out. Do NOT write a closing name, phone number, signature or any links: a fixed signature with Portfolio, LinkedIn and GitHub links is appended automatically. This overrides the skill's closing rule.
- No links or URLs in the body. No attachments.
- Set skip=true (with skip_reason) if the page gives you nothing specific to say about why this person, or the person is not a founder or executive.
- why_them: one sentence naming the specific thing from their page the email refers to.
- purpose: one internal line for the dashboard, e.g. "Working-student enquiry; fits vektordb (vector search)".

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
    send_at: str = ""  # UTC ISO time the tracker sends it
    tz: str = ""


class Outreacher:
    def __init__(self, store: Store, scout: Scout, log=print):
        self.store = store
        self.scout = scout
        self.log = log
        self.daily_limit = int(os.environ.get("OUTREACH_DAILY_LIMIT", "30"))
        self.tracker_url = tracker_setting("TAILOR_TRACKER_URL", "url")
        self.tracker_token = tracker_setting("TAILOR_TRACKER_TOKEN", "token")
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS outreach (founder_id INTEGER PRIMARY KEY, track TEXT, decision TEXT, reason TEXT, "
            "fit TEXT, projects TEXT, subject TEXT, body TEXT, url TEXT, at TEXT)"
        )
        if "send_at" not in {r[1] for r in store.db.execute("PRAGMA table_info(outreach)")}:
            store.db.execute("ALTER TABLE outreach ADD COLUMN send_at TEXT")
        store.db.commit()

    # ---------- selection ----------

    def booked(self) -> list[datetime]:
        rows = self.store.db.execute("SELECT send_at FROM outreach WHERE decision='queued' AND send_at >= ?",
                                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
        return [datetime.fromisoformat(r[0]) for r in rows if r[0]]

    def scheduled_today(self) -> int:
        today = datetime.now(timezone.utc).date()
        return sum(1 for t in self.booked() if t.date() == today)

    def next_slot(self, tz: str, taken: list[datetime] | None = None) -> datetime | None:
        """Earliest free weekday-morning slot in the recipient's zone within HORIZON_DAYS, at most daily_limit per UTC day."""
        zone, now = ZoneInfo(tz), datetime.now(timezone.utc)
        taken = sorted(self.booked() if taken is None else taken)
        per_day: dict = {}
        for t in taken:
            per_day[t.date()] = per_day.get(t.date(), 0) + 1
        gap = timedelta(minutes=SLOT_MINUTES)
        local_today = now.astimezone(zone).date()
        for d in range(HORIZON_DAYS + 1):
            day = local_today + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            t = datetime(day.year, day.month, day.day, *MORNING_START, tzinfo=zone)
            end = datetime(day.year, day.month, day.day, *MORNING_END, tzinfo=zone)
            while t < end:
                u = t.astimezone(timezone.utc)
                if u > now + timedelta(minutes=2) and per_day.get(u.date(), 0) < self.daily_limit and all(abs(u - b) >= gap for b in taken):
                    return u
                t += gap
        return None

    def room(self) -> int:
        """Founders that could still be booked: free slots left today (UTC), as a rough count for the UI."""
        return max(0, self.daily_limit - self.scheduled_today())

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
            "INSERT OR REPLACE INTO outreach (founder_id,track,decision,reason,fit,projects,subject,body,url,at,send_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r.founder.id, r.track, r.decision, r.reason, r.fit, json.dumps(r.projects or []), r.subject, r.body, r.url,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), r.send_at or None),
        )
        self.store.db.commit()

    # ---------- pipeline ----------

    async def draft(self, f: Founder, page_text: str, track: str) -> dict:
        catalog_names = project_names((REFERENCES / "projects.md").read_text())
        details = (
            f"{TRACKS[track]}\n\n"
            f"Recipient: {f.name}, {f.role} at {f.company or 'unknown company'}\n"
            f"Location: {f.location or 'unknown'}\nCompany website: {f.company_website or 'unknown'}\n"
            f"Email: {f.email} (listed on {f.email_source})\n"
            f"Company description (from scraping): {f.company_description}\nBio: {f.bio}\n\n"
            f"=== PAGE TEXT from {f.email_source} ===\n{page_text[:12000]}"
        )
        prompt = f"{TASK}\n\n=== FOUNDER ===\n{details}"
        with tempfile.TemporaryDirectory(prefix="founder-scout-") as tmp:
            schema, out = Path(tmp) / "schema.json", Path(tmp) / "draft.json"
            schema.write_text(json.dumps(DRAFT_SCHEMA))
            # Read-only sandbox rooted in the skill, so its relative ../tailor/references paths resolve.
            proc = await asyncio.create_subprocess_exec(
                CODEX_BIN, "exec", "-s", "read-only", "--ephemeral", "--skip-git-repo-check",
                "-C", str(SKILL_DIR), "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
                "--output-schema", str(schema), "-o", str(out), "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(prompt.encode()), CODEX_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("codex timed out")
            if proc.returncode != 0 or not out.exists() or not out.read_text().strip():
                raise RuntimeError(f"codex exited {proc.returncode}: {err.decode(errors='replace')[-300:]}")
            d = json.loads(out.read_text())
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
        body = d["body"].strip()
        # Drop any sign-off the model wrote anyway, then append the fixed signature.
        body = re.split(r"\n\s*(?:Best|Kind regards|Regards|Cheers|Thanks|Viele Grüße|Beste Grüße|Mit freundlichen Grüßen)?,?\s*\n?\s*Mahesh Koduri\b", body)[0].rstrip()
        d["body"] = f"{body}\n\n{SIGNATURE}"
        words = len(body.split())
        if not 50 <= words <= 170:
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

    def company_contacted(self, f: Founder) -> bool:
        """One email per company: match on company name or on the address's domain."""
        email_domain = f.email.split("@")[-1].lower()
        row = self.store.db.execute(
            "SELECT 1 FROM outreach o JOIN founders x ON x.id=o.founder_id WHERE o.decision IN ('queued','already_contacted') AND x.id != ? "
            "AND ((x.company != '' AND lower(x.company)=lower(?)) OR lower(substr(x.email, instr(x.email,'@')+1))=?) LIMIT 1",
            (f.id or -1, f.company or "", email_domain),
        ).fetchone()
        return row is not None

    async def process(self, f: Founder, send: bool = True, dry_run: bool = False) -> Result:
        local = f.email.split("@")[0].lower()
        if local in ROLE_INBOXES or self.company_contacted(f):
            result = Result(f, track_for(f), "skipped", "role inbox" if local in ROLE_INBOXES else "company already contacted")
            if not dry_run:
                self.record(result)
            self.log(f"outreach {f.name} <{f.email}>: skipped {result.reason}")
            return result
        tz = tz_for(f)
        slot = self.next_slot(tz)
        if slot is None:
            # Every morning slot in the horizon is booked; stay pending for a later run.
            return Result(f, track_for(f), "deferred", f"no free morning slot in the next {HORIZON_DAYS} days", tz=tz)
        try:
            result, payload = await self.prepare(f, send)
            result.tz = tz
            if payload:
                payload["send_at"] = result.send_at = slot.isoformat(timespec="seconds")
            if payload and not dry_run:
                result.decision, result.url = await self.post(payload)
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as e:
            result = Result(f, track_for(f), "error", str(e)[:300])
        if not dry_run:
            self.record(result)
        when = f" at {datetime.fromisoformat(result.send_at).astimezone(ZoneInfo(tz)):%a %H:%M} {tz}" if result.send_at else ""
        self.log(f"outreach {f.name} <{f.email}> [{result.track}/{result.fit or '-'}]: {result.decision}{when} {result.reason}")
        return result

    async def run(self, limit: int | None = None) -> list[Result]:
        """Book pending founders into recipients' morning slots until the horizon is full."""
        results = []
        for f in self.pending(limit if limit is not None else self.daily_limit):
            r = await self.process(f)
            if r.decision == "deferred":
                break
            results.append(r)
        return results
