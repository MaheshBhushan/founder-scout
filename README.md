# founder-scout

A Discord bot that finds startup founders worldwide and posts their profiles, including any email address the founder publishes on a public page.

## How it works

1. **Discovery.** Firecrawl search runs queries against YC company pages, Product Hunt, Indie Hackers, Wellfound, and company "about" pages. If `EXA_API_KEY` is set, Exa semantic search adds founders' personal sites, company team pages, and founder blog posts.
2. **Extraction.** Firecrawl scrapes each page and returns structured data (name, role, company, location, LinkedIn, X, bio) for founders, department heads (CTO, Head of Engineering, Data, AI, Product, engineering managers), and employees on the team. Two Firecrawl and two Exa queries target team and leadership pages. Department heads and employees are kept only with a personal address, since a shared inbox does not reach them.
3. **Email verification.** The bot keeps an email only when the address appears literally in the scraped page text, so model-guessed addresses are dropped. When a founder page lists no email, the bot checks the company homepage and `/contact` page. Each email is labelled `personal` (matches the founder's name), `company inbox` (hello@, founders@), or `listed`, and each email links to the page where it was found.
4. **Storage.** SQLite deduplicates by person, email, and URL, so the bot posts each founder once and scrapes each page once.

## Setup

1. Create an application at https://discord.com/developers/applications, open **Bot**, and copy the token.
2. Invite the bot with your application's Client ID:
   `https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=117760`
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, and `FIRECRAWL_API_KEY`. Set `DISCORD_GUILD_ID` to make slash commands appear in your server immediately.
4. Run it:

```sh
uv run founder-scout bot
```

## Commands

| Slash command | Effect |
|---|---|
| `/scout topic:<text> [region] [pages]` | Runs a search now and posts new founders in the channel |
| `/founders [text] [with_email]` | Searches founders already collected |
| `/export` | Sends all founders as a CSV file |
| `/stats` | Shows totals |
| `/autoscout enabled:<bool>` | Pauses or resumes the hourly run; the setting survives restarts |

The CLI covers the same steps without Discord: `uv run founder-scout run "climate tech" Stockholm 5` and `uv run founder-scout list`.

## Outreach

The bot never sends an LLM-drafted email without a human reading it first. Batch and automatic runs only create drafts in the tailor tracker (`/api/outreach` with `send: false`) for review on its dashboard; `/outreach_send` shows the draft and queues it only after you press **Send**. Automatic drafting after each scouting run is off by default (`/outreach_auto enabled:true` turns it on). It drafts each email with `claude -p` and the outreach skill (`~/.claude/skills/outreach/SKILL.md` and the tailor references), with read-only tools, so the bot and the skill share one set of rules. Set `CLAUDE_BIN` if `claude` is not on the service's `PATH`, and `CLAUDE_EFFORT` to change the effort level (default `medium`).

- **Track:** founders in Germany get a working-student enquiry; everyone else a remote-role enquiry.
- **Referrals:** department heads and employees get a referral ask. The email asks them to refer Mahesh or point him to the person who hires. It never asks them to hire him directly. These emails use the `founder-scout-<track>-referral` campaign.
- **Fit:** the draft cites one or two catalog projects that overlap with what the company builds. When none fit honestly, it falls back to a curiosity-led email about learning a field from first principles.
- **Checks:** before drafting, the bot re-fetches the page the address came from and skips the founder if the address is gone. The tracker refuses to auto-send without that page excerpt and never emails an address twice.
- **Schedule:** each email is booked into the recipient's weekday morning, 08:00 to 11:00 in their time zone (worked out from their location, else their domain, else Berlin), in 5-minute slots, up to 3 days ahead. The tailor tracker sends it when the slot arrives, at least 4 minutes after the previous scheduled email.
- **Limits:** `OUTREACH_DAILY_LIMIT` (default 30) emails per day, inside the tracker's shared cap of 40.

| Command | Effect |
|---|---|
| `/outreach_preview founder_id` | Drafts the email and shows it to you only; sends nothing |
| `/outreach_send founder_id` | Drafts one email, shows it to you, and queues it only after you press **Send** (10 minute timeout cancels) |
| `/outreach_run [count]` | Drafts emails for pending founders into the tracker for review; sends nothing |
| `/outreach_auto enabled` | Toggles drafting after automatic runs (default off; drafts only) |

CLI equivalents: `uv run founder-scout outreach-preview <id>`, `outreach-send <id>` (prints the draft and asks `y/N`; never sends from a non-interactive shell), `outreach-run [n]` (drafts only). The founder id is the `#n` in each card's footer.

### Researched application-contact fallback

When an application posting has no usable personal email, research a relevant named
employee or hiring contact first, then run the explicit fallback command:

```sh
# Start contact research early, in parallel with resume/cover-letter generation:
uv run founder-scout contact-resolve contact.json
# Only after reviewing the researched identity and outreach draft:
uv run founder-scout contact-resolve contact.json --queue
```

This command does not change automated discovery, invoke Claude Code, touch the founder
database, or send Gmail directly. Without `--queue`, it only contacts Reacher and
prints JSON; an optional `outreach` produces a tracker-ready immediate-dispatch
`payload`, not a send. Omit `outreach` for early contact research while application
documents are being generated. With `--queue`, a unique safe match is posted to
the tracker for immediate dispatch, not a next-morning slot.

Exact manifest structure (the identity excerpt below was fetched from the linked
public Apple leadership page; this is a **format example, not an outreach recommendation**).
Replace the entire researched contact and draft with your own verified, relevant
recipient. Do not run `--queue` on this example:

```json
{
  "contact": {
    "name": "Anna Schmidt",
    "domain": "company.example",
    "source_url": "https://company.example/team",
    "source_excerpt": "Anna Schmidt leads the engineering team at Example Company.",
    "company": "Example Company",
    "role": "Engineering Manager",
    "location": "Berlin"
  },
  "emails": [],
  "outreach": {
    "subject": "Engineering enquiry",
    "body": "I am researching engineering opportunities and would appreciate being pointed to the appropriate hiring contact. If this is not relevant, please let me know and I will not contact you again.",
    "purpose": "Application-contact enquiry",
    "campaign": "tailor-contact-fallback"
  }
}
```

The manifest above is illustrative, not real contact evidence. Replace its
identity and excerpt with information fetched from the employer's live site.

The contact fields may also be at the manifest's top level. `name`, `domain`,
`source_url`, `source_excerpt`, `company`, and `role` are required; `location` is
optional. The source must be an official company-domain/subdomain HTTP(S) page
with genuine public identity/employment evidence. Copy plain verbatim visible page
text, not Markdown decoration; the excerpt must literally include the full name
and be at most 1,000 characters. Never invent an excerpt or replace it with the
guessed address. The tracker independently re-fetches the source and rejects
unconfirmed identity evidence.

`emails` is an optional list of complete researched personal addresses at the
company domain, checked first. It is not a claim that an address is published.
The resolver also guesses `first.last`, `firstlast`, `f.last`, `flast`, `first`,
`first_last`, and `last.first`, normalizing accents and removing name punctuation.
Guesses require at least two name parts. Candidates are deduplicated and bounded
to eight total (researched addresses take precedence); generic inboxes are never
selected. Even an address actually published in the excerpt must pass Reacher in
this command.

Set `REACHER_URL` to a trusted deployment of
[reacherhq/check-if-email-exists](https://github.com/reacherhq/check-if-email-exists),
such as `http://127.0.0.1:8080` (base URL, not an endpoint). Requests go to
`POST /v0/check_email` with `{"to_email": "<candidate>"}` and a 30-second timeout per
candidate. Optional `REACHER_API_KEY` is sent as `x-reacher-secret`. The resolver
also reads `~/.config/tailor-tracker/reacher-url` and `reacher-key`; nonempty
environment values override these files. Keep the service key private. `--queue` also
requires `TAILOR_TRACKER_URL` and `TAILOR_TRACKER_TOKEN`, or the existing
`~/.config/tailor-tracker/{url,token}` files. The tracker must have its own trusted
Reacher settings: it rechecks the selected address and never trusts the CLI result.

Every candidate is checked. A match requires the exact returned `input`,
`is_reachable="safe"`, valid syntax, accepting MX, SMTP connectivity and
deliverability, and explicitly false catch-all, disabled, full-inbox, disposable,
and role-account flags. Missing flags, unknown results, timeouts, and service
errors fail closed.
A unique safe mailbox is delivery evidence, not proof of who reads it. Current
identity/employment and role relevance still come from the researched company
page; no candidate is selected when those facts are unsupported.

| JSON `decision` | Meaning |
|---|---|
| `verified` | Exactly one safe match; `selected_address` is set, with `payload` if outreach was supplied |
| `ambiguous` | Multiple safe mailboxes; ownership is unknown, so no selection or queue |
| `verification_failed` | No safe candidate, or an unknown/malformed result prevents a confident unique selection |
| `verification_unavailable` | Reacher is missing, errored, or timed out; no queue, even if another candidate was safe |
| `validation_error` | Invalid manifest or `--queue` without outreach; CLI exits 2 |
| `queue_unavailable` / `queue_error` | Tracker configuration/request failure; CLI exits 1 |
| Tracker decision, e.g. `queued`, `missing_evidence`, `already_contacted` | Returned only after an actual tracker request; `tracker` contains its response |

Each output includes `candidates` with raw Reacher `result`, status, and rejection
`reasons`. `outreach` is optional unless using `--queue`, and requires `subject`,
`body`, and `purpose`; `campaign` defaults to `tailor-contact-fallback`.
A unique match's payload sets `send: true` and omits `send_at`; application-contact
fallback outreach is dispatched immediately rather than scheduled for morning.
The tracker still independently verifies identity and mailbox safety and enforces
deduplication and shared sending caps. Founder campaign morning scheduling is
unchanged.
The request goes only to `POST /api/outreach`, with
`email_verification={"method":"reacher","name":"<researched name>","domain":"<company domain>"}`.


## Automatic runs

Each automatic run takes the next sector and city pair from a fixed rotation. The rotation covers 12 German cities first, then 18 other European cities, then 18 cities outside Europe, and then starts over. The position is stored in the database, so a restart continues where the last run stopped. `uv run founder-scout next 10` prints the upcoming pairs.

## Running as a service

The bot runs as the systemd user unit `founder-scout.service`, which restarts it on failure and starts it at boot. The unit retries every 60 seconds until the project drive is mounted.

```sh
systemctl --user status founder-scout
journalctl --user -u founder-scout -f
uv run founder-scout pause    # or resume; then: systemctl --user restart founder-scout
```

The separately installed `founder-scout-reacher.service` runs the digest-pinned
Reacher Docker backend on `127.0.0.1:18081`. The existing Cloudflare Tunnel exposes
only its `/v0/check_email` route at `https://reacher.mahesh.lol`, protected by the
`x-reacher-secret` header. The service definition is
`deploy/founder-scout-reacher.service`; its private environment lives at
`~/.config/tailor-tracker/reacher.env`, and the CLI reads `reacher-url`/`reacher-key`
from the same directory. Never commit these secret files.
The container uses host networking but binds the API to loopback only. This
avoids Docker bridge DNS timeouts observed on this workstation; port 25 must
remain reachable for SMTP checks.

```sh
systemctl --user status founder-scout-reacher
systemctl --user restart founder-scout-reacher
journalctl --user -u founder-scout-reacher -f
```

This verification endpoint depends on this machine, Docker, and the tunnel staying
online. If they stop or verification times out, both the resolver and tracker
fail closed rather than queueing an unverified address.


## Cost

Each scraped page costs about 5 Firecrawl credits with JSON extraction, plus 1 credit per company-site check. A default run scrapes 8 pages, which uses about 40–60 credits. Set `MAX_PAGES_PER_RUN` and `AUTO_INTERVAL_MINUTES` to control spending.

## Data protection

The bot collects personal data. Under GDPR, cold outreach to EU residents needs a legitimate interest, a clear opt-out, and deletion on request. Keep `email_source` so you can show where each address came from.
