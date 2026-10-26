"""Command line entry point.

    founder-scout bot                      run the Discord bot
    founder-scout run "AI agents" Berlin   one scouting run, printed to stdout
    founder-scout list [text]              show stored founders
    founder-scout pause | resume           stop or restart automatic runs (restart the service to apply)
    founder-scout next [n]                 show the next n automatic topic/region pairs
    founder-scout outreach-preview <id>    draft the email for one founder; sends nothing
    founder-scout outreach-send <id>       draft it, show it, and queue it for sending only after you type y
    founder-scout outreach-run [n]         draft emails for up to n pending founders into the tracker; sends nothing
    founder-scout contact-resolve <json>   research without sending; --queue dispatches a unique match immediately
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from dotenv import load_dotenv

from .scout import Scout, Settings, Store


async def _confirm_on_terminal(r) -> bool:
    """Show an LLM-drafted email and ask before queueing it. Non-interactive runs never send."""
    print(f"To: {r.founder.name} <{r.founder.email}>\nSubject: {r.subject}\n\n{r.body}\n", file=sys.stderr)
    if not sys.stdin.isatty():
        print("stdin is not a terminal; not sending.", file=sys.stderr)
        return False
    answer = await asyncio.to_thread(input, "Send this email? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    cmd = args[0] if args else "bot"
    if cmd == "bot":
        from .bot import run
        run()
    elif cmd == "run":
        topic = args[1] if len(args) > 1 else None
        region = args[2] if len(args) > 2 else None
        pages = int(args[3]) if len(args) > 3 else None
        settings = Settings.from_env()
        scout = Scout(settings, Store(settings.db_path), log=lambda m: print(m, file=sys.stderr, flush=True))

        async def go():
            try:
                return await scout.run(topic, region, pages)
            finally:
                await scout.close()

        for f in asyncio.run(go()):
            print(json.dumps({k: v for k, v in f.__dict__.items() if v}, ensure_ascii=False))
    elif cmd == "list":
        store = Store(Settings.from_env().db_path)
        for f in store.search(args[1] if len(args) > 1 else "", 50):
            print(f"{f.name:<28} {f.company[:24]:<24} {f.email or '-':<36} {f.source_url}")
    elif cmd in ("pause", "resume"):
        Store(Settings.from_env().db_path).set("auto_enabled", "true" if cmd == "resume" else "false")
        print(f"auto-scout {'resumed' if cmd == 'resume' else 'paused'}")
    elif cmd == "next":
        from .geo import ROTATION
        i = int(Store(Settings.from_env().db_path).get("rotation_index", "0"))
        for k in range(int(args[1]) if len(args) > 1 else 10):
            print(*ROTATION[(i + k) % len(ROTATION)], sep=" / ")
    elif cmd == "contact-resolve":
        from pathlib import Path
        from .contact_resolver import resolve_contact

        parser = argparse.ArgumentParser(
            prog="founder-scout contact-resolve",
            description="Verify application contacts without sending; run early alongside document generation.",
        )
        parser.add_argument("manifest", help="JSON manifest with researched identity evidence")
        parser.add_argument("--queue", action="store_true", help="request immediate dispatch of the unique verified contact via the tracker")
        options = parser.parse_args(args[1:])
        try:
            manifest = json.loads(Path(options.manifest).read_text(encoding="utf-8"))
            result = asyncio.run(resolve_contact(manifest, options.queue))
        except (OSError, UnicodeError, ValueError) as exc:
            print(json.dumps({"decision": "validation_error", "reason": str(exc),
                              "selected_address": None, "candidates": []}, ensure_ascii=False))
            sys.exit(2)
        print(json.dumps(result, ensure_ascii=False))
        if result["decision"] in ("queue_error", "queue_unavailable"):
            sys.exit(1)
    elif cmd.startswith("outreach-"):
        from .outreach import Outreacher
        settings = Settings.from_env()
        store = Store(settings.db_path)
        log = lambda m: print(m, file=sys.stderr, flush=True)
        scout = Scout(settings, store, log=log)
        out = Outreacher(store, scout, log=log)

        async def go():
            try:
                if cmd == "outreach-run":
                    return await out.run(int(args[1]) if len(args) > 1 else None)
                f = out.founder(int(args[1]))
                if not f:
                    raise SystemExit(f"no founder #{args[1]}")
                if cmd == "outreach-send":
                    return [await out.process(f, confirm=_confirm_on_terminal)]
                return [await out.process(f, dry_run=cmd == "outreach-preview")]
            finally:
                await scout.close()

        for r in asyncio.run(go()):
            print(f"\n#{r.founder.id} {r.founder.name} <{r.founder.email}> track={r.track} fit={r.fit} projects={r.projects} -> {r.decision} {r.url}")
            if r.reason:
                print(f"why/reason: {r.reason}")
            if r.subject:
                print(f"Subject: {r.subject}\n\n{r.body}")
    else:
        print(__doc__)
        sys.exit(2)
