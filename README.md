# founder-scout

A Discord bot that finds startup founders worldwide and posts their profiles, including any email address the founder publishes on a public page.

## How it works

1. **Discovery.** Firecrawl search runs queries against YC company pages, Product Hunt, Indie Hackers, Wellfound, and company "about" pages. If `EXA_API_KEY` is set, Exa semantic search adds founders' personal sites, company team pages, and founder blog posts.
2. **Extraction.** Firecrawl scrapes each page and returns structured founder data (name, role, company, location, LinkedIn, X, bio).
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

After each automatic run, the bot emails new founders who have a verified public address, through the tailor tracker (`/api/outreach`), and posts a card per email. It drafts each email with `codex exec` and the `$outreach` skill (`~/.codex/skills/outreach/SKILL.md` and the tailor references), in a read-only sandbox, so the bot and the skill share one set of rules. Set `CODEX_BIN` if `codex` is not on the service's `PATH`, and `CODEX_EFFORT` to change the reasoning effort (default `medium`).

- **Track:** founders in Germany get a working-student enquiry; everyone else a remote-role enquiry.
- **Fit:** the draft cites one or two catalog projects that overlap with what the company builds. When none fit honestly, it falls back to a curiosity-led email about learning a field from first principles.
- **Checks:** before drafting, the bot re-fetches the page the address came from and skips the founder if the address is gone. The tracker refuses to auto-send without that page excerpt and never emails an address twice.
- **Schedule:** each email is booked into the recipient's weekday morning, 08:00 to 11:00 in their time zone (worked out from their location, else their domain, else Berlin), in 5-minute slots, up to 3 days ahead. The tailor tracker sends it when the slot arrives, at least 4 minutes after the previous scheduled email.
- **Limits:** `OUTREACH_DAILY_LIMIT` (default 30) emails per day, inside the tracker's shared cap of 40.

| Command | Effect |
|---|---|
| `/outreach_preview founder_id` | Drafts the email and shows it to you only; sends nothing |
| `/outreach_send founder_id` | Drafts and sends one email |
| `/outreach_run [count]` | Emails pending founders now, within the daily limit |
| `/outreach_auto enabled` | Toggles outreach after automatic runs |

CLI equivalents: `uv run founder-scout outreach-preview <id>`, `outreach-send <id>`, `outreach-run [n]`. The founder id is the `#n` in each card's footer.

## Automatic runs

Each automatic run takes the next sector and city pair from a fixed rotation. The rotation covers 12 German cities first, then 18 other European cities, then 18 cities outside Europe, and then starts over. The position is stored in the database, so a restart continues where the last run stopped. `uv run founder-scout next 10` prints the upcoming pairs.

## Running as a service

The bot runs as the systemd user unit `founder-scout.service`, which restarts it on failure and starts it at boot. The unit retries every 60 seconds until the project drive is mounted.

```sh
systemctl --user status founder-scout
journalctl --user -u founder-scout -f
uv run founder-scout pause    # or resume; then: systemctl --user restart founder-scout
```

## Cost

Each scraped page costs about 5 Firecrawl credits with JSON extraction, plus 1 credit per company-site check. A default run scrapes 8 pages, which uses about 40–60 credits. Set `MAX_PAGES_PER_RUN` and `AUTO_INTERVAL_MINUTES` to control spending.

## Data protection

The bot collects personal data. Under GDPR, cold outreach to EU residents needs a legitimate interest, a clear opt-out, and deletion on request. Keep `email_source` so you can show where each address came from.
