"""Discord bot that posts founder profiles and answers slash commands."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks

from .outreach import Outreacher, Result
from .scout import Founder, Scout, Settings, Store

log = logging.getLogger("founder-scout")

EMAIL_BADGE = {"personal": "personal", "generic": "company inbox", "other": "listed"}


def embed_for(f: Founder) -> discord.Embed:
    title = f"{f.name} — {f.role or 'Founder'}" + (f" @ {f.company}" if f.company else "")
    color = 0x2ECC71 if f.email_type == "personal" else 0x3498DB if f.email else 0x95A5A6
    e = discord.Embed(title=title[:256], url=f.source_url or None, description=(f.bio or f.company_description)[:1000] or None, color=color)
    if f.email:
        e.add_field(name=f"Email ({EMAIL_BADGE.get(f.email_type, 'listed')})", value=f"`{f.email}`\n[source]({f.email_source})", inline=False)
    else:
        e.add_field(name="Email", value="not publicly listed", inline=False)
    if f.location:
        e.add_field(name="Location", value=f.location[:100], inline=True)
    if f.company and f.company_description and f.bio:
        e.add_field(name="Company", value=f.company_description[:300], inline=False)
    links = [f"[{label}]({url})" for label, url in (("Website", f.company_website), ("LinkedIn", f.linkedin), ("X", f.twitter), ("Personal", f.personal_website)) if url]
    if links:
        e.add_field(name="Links", value=" · ".join(links), inline=False)
    e.set_footer(text=f"#{f.id or '?'} · via {f.found_via} · {f.found_at[:10]}")
    return e


DECISION_LABEL = {
    "queued": ("sent", 0x2ECC71), "preview": ("preview, not sent", 0x3498DB), "draft_requested": ("saved as draft", 0xF1C40F),
    "missing_evidence": ("draft: address not verified", 0xF1C40F), "auto_send_off": ("draft: auto-send off", 0xF1C40F),
    "already_contacted": ("already contacted", 0x95A5A6), "skipped": ("skipped", 0x95A5A6), "error": ("error", 0xE74C3C),
}


def outreach_embed(r: Result, full: bool = False) -> discord.Embed:
    label, color = DECISION_LABEL.get(r.decision, (r.decision, 0x95A5A6))
    f = r.founder
    track = "Working student" if r.track == "working_student" else "Remote"
    e = discord.Embed(title=f"✉ {f.name} — {f.company or f.role}"[:256], url=r.url or None, color=color,
                      description=(r.subject and f"**{r.subject}**") or None)
    e.add_field(name="Status", value=label, inline=True)
    e.add_field(name="Track", value=track, inline=True)
    e.add_field(name="Fit", value=(", ".join(r.projects) if r.projects else "curiosity fallback") if r.fit else "—", inline=True)
    if r.reason:
        e.add_field(name="Why them" if r.subject else "Reason", value=r.reason[:1000], inline=False)
    if full and r.body:
        e.add_field(name="Email", value=r.body[:1024], inline=False)
    e.set_footer(text=f"#{f.id} · {f.email}")
    return e


class FounderBot(discord.Client):
    def __init__(self, settings: Settings, channel_id: int, guild_id: int | None, interval: int):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = Store(settings.db_path)
        self.scout = Scout(settings, self.store, log=log.info)
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.interval = interval
        self.lock = asyncio.Lock()  # one scouting run at a time keeps credit use predictable
        # Pause state lives in the database so it survives restarts.
        self.auto_enabled = interval > 0 and self.store.get("auto_enabled", "true") == "true"
        self.outreach = Outreacher(self.store, self.scout, log=log.info)
        # Persistent like auto_enabled: after each automatic run, email new founders.
        self.outreach_auto = self.store.get("outreach_auto", "true") == "true"
        self._register()

    async def setup_hook(self) -> None:
        if self.guild_id:
            await self._sync_guild(discord.Object(id=self.guild_id))
        if self.interval > 0:
            self.auto_loop.change_interval(minutes=self.interval)
            self.auto_loop.start()

    async def on_ready(self) -> None:
        log.info("logged in as %s; posting to channel %s; servers: %s", self.user, self.channel_id, [g.name for g in self.guilds])
        if not self.guilds:
            log.warning("bot is in no server yet; invite it with https://discord.com/oauth2/authorize?client_id=%s&scope=bot+applications.commands&permissions=117760", self.user.id)
        for g in self.guilds:
            await self._sync_guild(g)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("joined server %s", guild.name)
        await self._sync_guild(guild)
        if self.auto_enabled and guild.get_channel(self.channel_id):
            self.auto_loop.restart()  # first post right away instead of waiting a full interval

    async def _sync_guild(self, guild: discord.abc.Snowflake) -> None:
        """Guild-scoped commands appear instantly; global ones can take an hour."""
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self.scout.close()
        await super().close()

    async def post(self, channel: discord.abc.Messageable, founders: list[Founder]) -> None:
        for i in range(0, len(founders), 10):  # Discord allows 10 embeds per message
            await channel.send(embeds=[embed_for(f) for f in founders[i:i + 10]])

    async def scout_and_post(self, channel: discord.abc.Messageable, topic: str | None, region: str | None, pages: int | None) -> list[Founder]:
        async with self.lock:
            founders = await self.scout.run(topic, region, pages)
        # Profiles with emails first, personal emails before company inboxes.
        founders.sort(key=lambda f: {"personal": 0, "generic": 1, "other": 2}.get(f.email_type, 3))
        await self.post(channel, founders)
        return founders

    async def outreach_and_post(self, channel: discord.abc.Messageable, founders: list[Founder] | None = None, limit: int | None = None) -> list[Result]:
        """Email founders (the given ones, or the pending backlog) within the daily limit and report each result."""
        async with self.lock:
            if founders is None:
                results = await self.outreach.run(limit)
            else:
                ids = {f.id for f in founders}
                todo = [f for f in self.outreach.pending(500) if f.id in ids][: self.outreach.room()]
                results = [await self.outreach.process(f) for f in todo]
        for i in range(0, len(results), 10):
            await channel.send(embeds=[outreach_embed(r) for r in results[i:i + 10]])
        return results

    @tasks.loop(minutes=60)
    async def auto_loop(self) -> None:
        if not self.auto_enabled:
            return
        try:
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            found = await self.scout_and_post(channel, None, None, None)
            if self.outreach_auto and any(f.email for f in found):
                await self.outreach_and_post(channel, [f for f in found if f.email])
        except discord.Forbidden:
            log.warning("no access to channel %s; is the bot invited to that server?", self.channel_id)
        except Exception:
            log.exception("automatic run failed")

    @auto_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    def _register(self) -> None:
        tree = self.tree

        @tree.command(description="Find founders now, e.g. topic='AI agents' region='Berlin'")
        @app_commands.describe(topic="Sector or keywords", region="City or country (optional)", pages="Pages to scrape, 1-20 (about 5 credits each)")
        async def scout(inter: discord.Interaction, topic: str, region: str | None = None, pages: app_commands.Range[int, 1, 20] | None = None):
            await inter.response.defer(thinking=True)
            if self.lock.locked():
                await inter.followup.send("A scouting run is in progress; this one starts when it finishes.")
            try:
                found = await self.scout_and_post(inter.channel, topic, region, pages)
            except Exception as e:
                log.exception("scout failed")
                await inter.followup.send(f"Scouting failed: {e}")
                return
            n_mail = sum(1 for f in found if f.email)
            await inter.followup.send(f"Done: {len(found)} new founders for **{topic}**{f' in {region}' if region else ''}, {n_mail} with a public email.")

        @tree.command(description="Search founders already collected")
        @app_commands.describe(text="Name, company, location, or keyword", with_email="Only show founders with an email")
        async def founders(inter: discord.Interaction, text: str = "", with_email: bool = False, limit: app_commands.Range[int, 1, 30] = 10):
            rows = self.store.search(text, limit, with_email)
            if not rows:
                await inter.response.send_message("No matches.", ephemeral=True)
                return
            await inter.response.send_message(embeds=[embed_for(f) for f in rows[:10]])
            for i in range(10, len(rows), 10):
                await inter.followup.send(embeds=[embed_for(f) for f in rows[i:i + 10]])

        @tree.command(description="Download every collected founder as CSV")
        async def export(inter: discord.Interaction):
            rows = self.store.all()
            buf = io.StringIO()
            fields = [k for k in Founder.__dataclass_fields__]
            w = csv.DictWriter(buf, fieldnames=fields)
            w.writeheader()
            for f in rows:
                w.writerow(f.__dict__)
            await inter.response.send_message(f"{len(rows)} founders", file=discord.File(io.BytesIO(buf.getvalue().encode()), "founders.csv"), ephemeral=True)

        @tree.command(description="Database totals")
        async def stats(inter: discord.Interaction):
            s = self.store.stats()
            auto = f"every {self.interval} min" if self.auto_enabled and self.interval else "off"
            await inter.response.send_message(
                f"Founders: **{s['founders']}** · with email: **{s['with_email']}** (personal: {s['personal_email']}) · pages scraped: {s['pages_scraped']} · auto-scout: {auto}\n"
                f"Outreach: {self.outreach.sent_last_24h()}/{self.outreach.daily_limit} sent in 24 h · auto-outreach: {'on' if self.outreach_auto else 'off'}"
            )

        @tree.command(name="outreach_preview", description="Draft the outreach email for one founder (#id from the card footer); sends nothing")
        async def outreach_preview(inter: discord.Interaction, founder_id: int):
            f = self.outreach.founder(founder_id)
            if not f or not f.email:
                await inter.response.send_message("No founder with that id and a public email.", ephemeral=True)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            r = await self.outreach.process(f, dry_run=True)
            await inter.followup.send(embed=outreach_embed(r, full=True), ephemeral=True)

        @tree.command(name="outreach_send", description="Draft and send the outreach email for one founder (#id)")
        async def outreach_send(inter: discord.Interaction, founder_id: int):
            f = self.outreach.founder(founder_id)
            if not f or not f.email:
                await inter.response.send_message("No founder with that id and a public email.", ephemeral=True)
                return
            await inter.response.defer(thinking=True)
            async with self.lock:
                r = await self.outreach.process(f)
            await inter.followup.send(embed=outreach_embed(r, full=True))

        @tree.command(name="outreach_run", description="Email pending founders now, within the daily limit")
        async def outreach_run(inter: discord.Interaction, count: app_commands.Range[int, 1, 20] = 5):
            await inter.response.defer(thinking=True)
            results = await self.outreach_and_post(inter.channel, None, count)
            queued = sum(r.decision == "queued" for r in results)
            await inter.followup.send(f"Outreach: {queued} queued, {len(results) - queued} skipped or drafted. {self.outreach.room()} left in today's limit of {self.outreach.daily_limit}.")

        @tree.command(name="outreach_auto", description="Email new founders automatically after each scouting run")
        async def outreach_auto(inter: discord.Interaction, enabled: bool):
            self.outreach_auto = enabled
            self.store.set("outreach_auto", "true" if enabled else "false")
            await inter.response.send_message(f"Automatic outreach {'on' if enabled else 'off'} (limit {self.outreach.daily_limit} per 24 h).")

        @tree.command(name="autoscout", description="Turn automatic scouting on or off")
        async def autoscout(inter: discord.Interaction, enabled: bool):
            self.auto_enabled = enabled
            self.store.set("auto_enabled", "true" if enabled else "false")
            if enabled and not self.auto_loop.is_running():
                self.interval = self.interval or 60
                self.auto_loop.change_interval(minutes=self.interval)
                self.auto_loop.start()
            await inter.response.send_message(f"Auto-scout {'on' if enabled else 'off'}.")


def run() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    channel = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel:
        raise SystemExit("Set DISCORD_TOKEN and DISCORD_CHANNEL_ID in .env")
    guild = os.environ.get("DISCORD_GUILD_ID")
    bot = FounderBot(Settings.from_env(), int(channel), int(guild) if guild else None, int(os.environ.get("AUTO_INTERVAL_MINUTES", "60")))
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot.run(token, log_handler=None)
