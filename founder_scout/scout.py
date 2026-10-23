"""Discovery, extraction, and storage for founder profiles.

Discovery finds candidate pages through Exa (semantic search) and Firecrawl search.
Extraction scrapes each page with Firecrawl and asks for structured founder data.
An email is kept only when it appears literally in scraped page text, so the model
cannot invent addresses.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

FIRECRAWL = "https://api.firecrawl.dev/v2"
EXA = "https://api.exa.ai"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
JUNK_EMAIL = re.compile(
    r"(example\.|sentry|wixpress|noreply|no-reply|donotreply|\.png$|\.jpg$|\.webp$|"
    r"@2x|u003e|privacy@|abuse@|legal@|gdpr@|dpo@|unsubscribe)",
    re.I,
)
GENERIC_LOCAL = {"hello", "hi", "info", "contact", "team", "founders", "founder", "office", "mail", "support", "sales", "press", "jobs", "careers"}

# Firecrawl cannot scrape these, and the profiles do not list emails.
SKIP_DOMAINS = ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "youtube.com")
# Lead-selling directories hide emails behind paywalls; scraping them wastes credits.
LOW_VALUE_DOMAINS = ("vcbacked.co", "connectsafely.ai", "rocketreach.co", "zoominfo.com", "apollo.io", "signalhire.com", "contactout.com", "lusha.com")

# Directory pages that the model sometimes reports as the company's own website.
AGGREGATOR_DOMAINS = ("producthunt.com", "indiehackers.com", "ycombinator.com", "wellfound.com", "crunchbase.com", "github.com", "medium.com", "substack.com")

PLACEHOLDER_NAME = re.compile(r"\b(not|unknown|mentioned|n/a|none|founder|team|anonymous)\b|\b(john|jane) doe\b|\b(max|erika) mustermann\b|lorem ipsum", re.I)
AGGREGATOR_NAMES = {"indiehackers", "producthunt", "ycombinator", "wellfound", "angellist", "crunchbase", "github", "medium", "substack"}

SECTORS = ["AI", "fintech", "climate tech", "healthtech", "developer tools", "SaaS", "robotics", "edtech", "biotech", "e-commerce", "cybersecurity", "mobility"]
# Automatic runs work through these tiers in order: Germany, rest of Europe, rest of the world.
REGION_TIERS = [
    ["Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne", "Stuttgart", "Düsseldorf", "Leipzig", "Karlsruhe", "Dresden", "Aachen", "Nuremberg"],
    ["London", "Paris", "Amsterdam", "Stockholm", "Copenhagen", "Helsinki", "Oslo", "Zurich", "Vienna", "Madrid", "Barcelona", "Lisbon",
     "Milan", "Dublin", "Warsaw", "Prague", "Tallinn", "Brussels"],
    ["San Francisco", "New York", "Boston", "Austin", "Toronto", "Bangalore", "Singapore", "Tel Aviv", "Dubai", "Lagos", "Nairobi",
     "Cape Town", "São Paulo", "Mexico City", "Sydney", "Tokyo", "Seoul", "Jakarta"],
]
# Within a tier, each pass covers every city with one sector, so consecutive runs change city.
ROTATION = [(sector, city) for tier in REGION_TIERS for sector in SECTORS for city in tier]

# Query templates per source. {topic} is the user query or a sector, {region} a city.
FIRECRAWL_TEMPLATES = [
    "site:ycombinator.com/companies {topic} {region}",
    "{topic} startup {region} founder \"email me\" OR \"reach me at\"",
    "{topic} startup {region} \"about us\" founders team contact",
    "site:producthunt.com {topic} maker founder {region}",
    "site:indiehackers.com/post {topic} founder {region} my startup",
    "site:wellfound.com/company {topic} {region}",
    # Team and leadership pages list department heads and engineers who can refer Mahesh internally.
    "{topic} startup {region} \"our team\" \"head of engineering\" OR \"VP engineering\" OR \"engineering manager\"",
    "{topic} company {region} \"meet the team\" engineers data scientists",
]
EXA_TEMPLATES = [
    ("personal site", "personal website of a {topic} startup founder based in {region}"),
    ("company", "early-stage {topic} startup in {region} with founders listed on the team page"),
    (None, "blog post by the founder of a {topic} startup in {region} sharing their contact email"),
    ("company", "{topic} startup in {region} whose team page lists the head of engineering, data, or AI and its engineers"),
    ("personal site", "personal website of an engineering manager or senior engineer at a {topic} startup in {region}"),
]

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "company_website": {"type": "string"},
        "company_description": {"type": "string"},
        "location": {"type": "string"},
        "founders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "kind": {"type": "string", "enum": ["founder", "department_head", "employee"]},
                    "email": {"type": "string"},
                    "linkedin": {"type": "string"},
                    "twitter": {"type": "string"},
                    "personal_website": {"type": "string"},
                    "bio": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
}
PROFILE_PROMPT = (
    "Extract the people described on this page who work at the company, and the company itself. "
    "Include founders and co-founders (kind=founder); department heads such as CTO, VP or Head of Engineering, "
    "Data, AI, Product or Research, and engineering managers or team leads (kind=department_head); and "
    "engineers, data scientists, researchers or product people on the team (kind=employee). "
    "Skip investors, advisors, board members, and people who do not work there. Copy each role as written. "
    "Copy emails exactly as written on the page; leave email empty if none is shown. Never guess an email."
)
KINDS = ("founder", "department_head", "employee")
FOUNDER_ROLE = re.compile(r"found", re.I)


@dataclass
class Founder:
    name: str
    role: str = ""
    company: str = ""
    company_website: str = ""
    company_description: str = ""
    location: str = ""
    email: str = ""
    email_type: str = ""  # "personal", "generic", or ""
    email_source: str = ""
    linkedin: str = ""
    twitter: str = ""
    personal_website: str = ""
    bio: str = ""
    source_url: str = ""
    found_via: str = ""
    kind: str = "founder"  # "founder", "department_head", or "employee"; non-founders get a referral ask
    found_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.name.strip().lower()}|{self.company.strip().lower()}"


@dataclass
class Settings:
    firecrawl_key: str
    exa_key: str = ""
    max_pages: int = 8
    follow_company_site: bool = True
    db_path: str = "founders.db"

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not key:
            raise SystemExit("FIRECRAWL_API_KEY is not set")
        return cls(
            firecrawl_key=key,
            exa_key=os.environ.get("EXA_API_KEY", ""),
            max_pages=int(os.environ.get("MAX_PAGES_PER_RUN", "8")),
            follow_company_site=os.environ.get("FOLLOW_COMPANY_SITE", "true").lower() == "true",
            db_path=os.environ.get("DB_PATH", "founders.db"),
        )


# ---------- storage ----------

class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        cols = ", ".join(f"{f} TEXT" for f in Founder.__dataclass_fields__ if f not in ("id",))
        self.db.execute(f"CREATE TABLE IF NOT EXISTS founders (id INTEGER PRIMARY KEY, key TEXT UNIQUE, {cols})")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(founders)")}
        for f in Founder.__dataclass_fields__:
            if f != "id" and f not in have:  # rows collected before the column existed are founders
                self.db.execute(f"ALTER TABLE founders ADD COLUMN {f} TEXT DEFAULT 'founder'" if f == "kind" else f"ALTER TABLE founders ADD COLUMN {f} TEXT")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen_urls (url TEXT PRIMARY KEY, at TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        self.db.commit()

    def seen(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen_urls WHERE url=?", (url,)).fetchone() is not None

    def mark_seen(self, url: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO seen_urls VALUES (?, ?)", (url, datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def email_known(self, email: str) -> bool:
        return bool(email) and self.db.execute("SELECT 1 FROM founders WHERE lower(email)=lower(?)", (email,)).fetchone() is not None

    def add(self, f: Founder) -> bool:
        """Insert a founder. Returns False when the person or email already exists."""
        if self.email_known(f.email):
            return False
        data = {k: v for k, v in f.__dict__.items() if k != "id"}
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO founders (key, {', '.join(data)}) VALUES (?, {', '.join('?' * len(data))})",
            (f.key, *data.values()),
        )
        self.db.commit()
        f.id = cur.lastrowid if cur.rowcount else None
        return bool(cur.rowcount)

    def search(self, text: str = "", limit: int = 10, with_email: bool = False, kind: str = "") -> list[Founder]:
        sql = "SELECT * FROM founders WHERE 1=1"
        args: list = []
        if text:
            sql += " AND (name LIKE ? OR company LIKE ? OR location LIKE ? OR company_description LIKE ? OR bio LIKE ?)"
            args += [f"%{text}%"] * 5
        if with_email:
            sql += " AND email != ''"
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.db.execute(sql, args)]

    def all(self) -> list[Founder]:
        return [self._row(r) for r in self.db.execute("SELECT * FROM founders ORDER BY id")]

    def stats(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {
            "founders": q("SELECT count(*) FROM founders"),
            "with_email": q("SELECT count(*) FROM founders WHERE email != ''"),
            "personal_email": q("SELECT count(*) FROM founders WHERE email_type='personal'"),
            "department_heads": q("SELECT count(*) FROM founders WHERE kind='department_head'"),
            "employees": q("SELECT count(*) FROM founders WHERE kind='employee'"),
            "pages_scraped": q("SELECT count(*) FROM seen_urls"),
        }

    @staticmethod
    def _row(r: sqlite3.Row) -> Founder:
        d = dict(r)
        d.pop("key", None)
        return Founder(**d)


# ---------- helpers ----------

def domain(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return host.removeprefix("www.")


def skip_url(url: str) -> bool:
    d = domain(url)
    return any(d == s or d.endswith("." + s) for s in SKIP_DOMAINS + LOW_VALUE_DOMAINS)


def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url or url.lower() in ("n/a", "none", "null"):
        return ""
    return url if "://" in url else f"https://{url}"


# Button or link text that page-to-markdown glues onto an address ("name@x.comCopy").
GLUED_SUFFIX = re.compile(r"(\.(?:com|net|org|de|io|ai|co|eu))(?:copy|copied|weitere|mehr|more|email|e-mail|kontakt|contact)$")


def page_emails(text: str) -> list[str]:
    found = []
    for m in EMAIL_RE.findall(text or ""):
        e = GLUED_SUFFIX.sub(r"\1", m.strip(".").lower())
        if not JUNK_EMAIL.search(e) and e not in found:
            found.append(e)
    return found


def classify_email(email: str, name: str) -> str:
    local = email.split("@")[0].lower()
    parts = [p for p in re.split(r"\W+", name.lower()) if len(p) > 1]
    if any(p in local or (len(local) >= 3 and p.startswith(local)) for p in parts):
        return "personal"
    if local in GENERIC_LOCAL:
        return "generic"
    return "other"


def pick_email(name: str, claimed: str, emails: list[str], company_site: str = "") -> tuple[str, str]:
    """Choose the best email for a founder from addresses that appear on the page."""
    claimed = (claimed or "").strip().lower()
    if claimed and claimed in emails:
        return claimed, classify_email(claimed, name)
    personal = [e for e in emails if classify_email(e, name) == "personal"]
    if personal:
        return personal[0], "personal"
    site = domain(company_site) if company_site else ""
    # A shared inbox only counts when it belongs to the founder's own company domain;
    # otherwise it is usually the directory site's support address.
    generic = [e for e in emails if site and classify_email(e, name) == "generic" and e.endswith("@" + site)]
    if generic:
        return generic[0], "generic"
    return "", ""


# ---------- API clients ----------

class Scout:
    def __init__(self, settings: Settings, store: Store, log=print):
        self.s = settings
        self.store = store
        self.log = log
        self.http = httpx.AsyncClient(timeout=90)
        self.fc_slots = asyncio.Semaphore(2)  # low Firecrawl plans reject more parallel requests

    async def close(self) -> None:
        await self.http.aclose()

    async def _firecrawl(self, path: str, body: dict) -> dict:
        """POST to Firecrawl, retrying with backoff when rate limited."""
        for attempt in range(5):
            async with self.fc_slots:
                r = await self.http.post(f"{FIRECRAWL}/{path}", headers={"Authorization": f"Bearer {self.s.firecrawl_key}"}, json=body)
            if r.status_code != 429 or attempt == 4:
                r.raise_for_status()
                return r.json()
            wait = float(r.headers.get("retry-after") or 0) or 5 * 2 ** attempt
            self.log(f"firecrawl rate limited, retrying in {wait:.0f}s")
            await asyncio.sleep(min(wait, 60))
        return {}

    async def firecrawl_search(self, query: str, limit: int = 5) -> list[str]:
        data = await self._firecrawl("search", {"query": query, "limit": limit})
        return [x["url"] for x in data.get("data", {}).get("web", []) if x.get("url")]

    async def exa_search(self, query: str, category: str | None, limit: int = 5) -> list[str]:
        body = {"query": query, "type": "auto", "numResults": limit}
        if category:
            body["category"] = category
        r = await self.http.post(f"{EXA}/search", headers={"x-api-key": self.s.exa_key}, json=body)
        if r.status_code == 400 and category:  # category names change between API versions
            return await self.exa_search(query, None, limit)
        r.raise_for_status()
        return [x["url"] for x in r.json().get("results", []) if x.get("url")]

    async def scrape(self, url: str, extract: bool) -> dict:
        formats: list = ["markdown"]
        if extract:
            formats.append({"type": "json", "prompt": PROFILE_PROMPT, "schema": PROFILE_SCHEMA})
        data = await self._firecrawl("scrape", {"url": url, "formats": formats, "onlyMainContent": False, "timeout": 60000})
        return data.get("data", {})

    # ---------- pipeline ----------

    async def discover(self, topic: str, region: str) -> list[tuple[str, str]]:
        """Return (url, source) candidates from every configured source."""
        jobs = []
        if self.s.exa_key:  # Exa first: its semantic results hit founder pages more often
            jobs += [(f"exa:{c or 'web'}", self.exa_search(t.format(topic=topic, region=region), c, 6)) for c, t in EXA_TEMPLATES]
        jobs += [(f"firecrawl:{t.split()[0]}", self.firecrawl_search(t.format(topic=topic, region=region).strip(), 5)) for t in FIRECRAWL_TEMPLATES]
        results = await asyncio.gather(*(j for _, j in jobs), return_exceptions=True)
        out, seen = [], set()
        # Interleave sources so a small page budget still samples each of them.
        lists = []
        for (src, _), res in zip(jobs, results):
            if isinstance(res, Exception):
                self.log(f"search failed ({src}): {res}")
                continue
            lists.append([(u, src) for u in res])
        for row in itertools.zip_longest(*lists):
            for item in row:
                if not item:
                    continue
                url = item[0].split("#")[0]
                if url in seen or skip_url(url) or self.store.seen(url):
                    continue
                seen.add(url)
                out.append((url, item[1]))
        return out

    async def extract(self, url: str, source: str) -> list[Founder]:
        data = await self.scrape(url, extract=True)
        self.store.mark_seen(url)
        info = data.get("json") or {}
        text = data.get("markdown") or ""
        emails = page_emails(text)
        site = clean_url(info.get("company_website", ""))
        if site and any(domain(site).endswith(d) for d in AGGREGATOR_DOMAINS):
            site = ""
        company = (info.get("company") or "").strip()
        if re.sub(r"\W", "", company.lower()) in AGGREGATOR_NAMES:
            return []  # a directory page that lists its own staff or users, not a startup
        founders: list[Founder] = []
        for p in info.get("founders") or []:
            name = (p.get("name") or "").strip()
            if not 2 <= len(name.split()) <= 6 or PLACEHOLDER_NAME.search(name):
                continue
            role = (p.get("role") or "").strip()
            kind = p.get("kind") if p.get("kind") in KINDS else "employee"
            if FOUNDER_ROLE.search(role):
                kind = "founder"
            email, etype = pick_email(name, p.get("email", ""), emails, site)
            # A shared inbox reaches whoever reads it, not this employee, so referral contacts need a personal address.
            if kind != "founder" and etype == "generic":
                email, etype = "", ""
            founders.append(Founder(
                name=name,
                role=role or ("Founder" if kind == "founder" else ""),
                kind=kind,
                company=(info.get("company") or "").strip(),
                company_website=site,
                company_description=(info.get("company_description") or "").strip()[:500],
                location=(info.get("location") or "").strip(),
                email=email,
                email_type=etype,
                email_source=url if email else "",
                linkedin=clean_url(p.get("linkedin", "")),
                twitter=clean_url(p.get("twitter", "")),
                personal_website=clean_url(p.get("personal_website", "")),
                bio=(p.get("bio") or "").strip()[:400],
                source_url=url,
                found_via=source,
            ))
        if self.s.follow_company_site and site and any(not f.email for f in founders) and not skip_url(site):
            await self._fill_from_site(site, founders)
        return founders

    async def _fill_from_site(self, site: str, founders: list[Founder]) -> None:
        """Scrape the company homepage and its contact page for listed emails."""
        for url in (site, urljoin(site.rstrip("/") + "/", "contact")):
            if self.store.seen(url):
                continue
            try:
                text = (await self.scrape(url, extract=False)).get("markdown") or ""
            except httpx.HTTPError:
                continue
            finally:
                self.store.mark_seen(url)
            emails = page_emails(text)
            for f in founders:
                if not f.email:
                    f.email, f.email_type = pick_email(f.name, "", emails, site)
                    if f.kind != "founder" and f.email_type == "generic":
                        f.email, f.email_type = "", ""
                    f.email_source = url if f.email else ""
            if all(f.email for f in founders):
                return

    async def run(self, topic: str | None = None, region: str | None = None, max_pages: int | None = None) -> list[Founder]:
        """Discover and extract founders. Returns only founders new to the database."""
        if not topic:
            i = int(self.store.get("rotation_index", "0")) % len(ROTATION)
            topic, region = ROTATION[i]
            self.store.set("rotation_index", str(i + 1))
        region = region or ""
        budget = max_pages or self.s.max_pages
        self.log(f"scouting topic={topic!r} region={region!r} pages={budget}")
        candidates = await self.discover(topic, region)
        self.log(f"{len(candidates)} candidate pages")
        new: list[Founder] = []
        sem = asyncio.Semaphore(3)

        async def one(url: str, src: str) -> list[Founder]:
            async with sem:
                try:
                    return await self.extract(url, src)
                except httpx.HTTPError as e:
                    self.log(f"scrape failed {url}: {e}")
                    self.store.mark_seen(url)
                    return []

        for batch in await asyncio.gather(*(one(u, s) for u, s in candidates[:budget])):
            for f in batch:
                if self.store.add(f):
                    new.append(f)
        n_ref = sum(1 for f in new if f.kind != "founder")
        self.log(f"{len(new)} new people ({n_ref} department heads or employees), {sum(1 for f in new if f.email)} with email")
        return new
