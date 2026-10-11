"""Command line entry point.

    founder-scout bot                      run the Discord bot
    founder-scout run "AI agents" Berlin   one scouting run, printed to stdout
    founder-scout list [text]              show stored founders
    founder-scout pause | resume           stop or restart automatic runs (restart the service to apply)
    founder-scout next [n]                 show the next n automatic topic/region pairs
    founder-scout outreach-preview <id>    draft the email for one founder; sends nothing
    founder-scout outreach-send <id>       draft and queue it in the tailor tracker
    founder-scout outreach-run [n]         process up to n pending founders within the daily limit
"""

from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv

from .scout import Scout, Settings, Store


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
        from .scout import ROTATION
        i = int(Store(Settings.from_env().db_path).get("rotation_index", "0"))
        for k in range(int(args[1]) if len(args) > 1 else 10):
            print(*ROTATION[(i + k) % len(ROTATION)], sep=" / ")
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
