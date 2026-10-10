#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feed watcher: checks configured public feeds and sends a Telegram alert when
new items appear. One check per invocation; scheduling is handled externally.

Feeds live in config.json. Python 3.8+, standard library only.

Usage:
    python watch.py                 # one monitoring cycle
    python watch.py --bootstrap     # record current items as known, send nothing
    python watch.py --dry-run       # don't send to Telegram, print messages
    python watch.py --hello         # send a test message to verify Telegram config
"""

import argparse
import csv
import html
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
HEARTBEAT_PATH = os.path.join(BASE_DIR, "HEARTBEAT.md")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 30
HEARTBEAT_INTERVAL = 20 * 3600  # keep the host repository active


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}] {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    # environment overrides (set in CI)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    for feed in cfg.get("feeds", []):
        if os.environ.get("FEED_URL"):
            feed["url"] = os.environ["FEED_URL"]
    return cfg


def load_state():
    state = {"feeds": {}, "last_heartbeat": 0}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            state.update(loaded)
    except Exception as exc:
        log(f"state file unreadable ({exc}); starting fresh")
    state.setdefault("feeds", {})
    return state


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, ensure_ascii=False)


def maybe_update_heartbeat(state):
    now = time.time()
    if now - state.get("last_heartbeat", 0) < HEARTBEAT_INTERVAL:
        return False
    state["last_heartbeat"] = now
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with open(HEARTBEAT_PATH, "w", encoding="utf-8") as fh:
        fh.write(f"last cycle: {stamp}\n")
    return True


def send_telegram(cfg, text):
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    if not token or not chat_id:
        raise RuntimeError("telegram_bot_token / telegram_chat_id are not configured")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": bool(cfg.get("disable_link_preview", True)),
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(f"telegram send failed: {out}")


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- feed types

def parse_scope_csv(url):
    """CSV feed: every data row is an item keyed by its identifier column."""
    body = http_get(url)
    if not body.lstrip().lower().startswith("identifier"):
        raise RuntimeError(f"unexpected CSV payload from {url}")
    items = {}
    for row in csv.DictReader(io.StringIO(body)):
        ident = (row.get("identifier") or "").strip()
        if not ident:
            continue
        itype = (row.get("asset_type") or "").strip()
        items[ident] = {"type": itype}
    return items


FEED_PARSERS = {
    "scope_csv": parse_scope_csv,
}


# ---------------------------------------------------------------- main cycle

def check_feed(cfg, feed, state, notify):
    name = feed.get("name", "feed")
    url = feed.get("url") or ""
    ftype = feed.get("type", "scope_csv")
    parser = FEED_PARSERS.get(ftype)
    if not url or parser is None:
        log(f"feed '{name}' skipped (missing url or unknown type '{ftype}')")
        return

    items = parser(url)
    feed_state = state["feeds"].setdefault(name, {"items": {}})
    known = feed_state["items"]

    new_items = {k: v for k, v in items.items() if k not in known}

    if notify and new_items:
        lines = [f"\u2022 {html.escape(k)}"
                 + (f" ({html.escape(v['type'])})" if v.get("type") else "")
                 for k, v in sorted(new_items.items())]
        text = (f"\U0001F195 <b>New items on feed</b> \u2018{html.escape(name)}\u2019:\n"
                + "\n".join(lines))
        send_telegram(cfg, text)          # raises on failure -> items stay unknown
        log(f"alert sent for feed '{name}': {len(new_items)} new item(s)")

    if new_items:
        known.update(new_items)           # only after a successful alert

    removed = [k for k in list(known) if k not in items]
    if removed and cfg.get("notify_removed", False):
        text = (f"\u26D4 <b>Items removed from feed</b> \u2018{html.escape(name)}\u2019:\n"
                + "\n".join(html.escape(k) for k in sorted(removed)))
        send_telegram(cfg, text)
        log(f"removal alert sent for feed '{name}'")
    for k in removed:
        known.pop(k, None)

    log(f"feed '{name}': {len(items)} item(s) known, {len(new_items)} new")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", action="store_true",
                    help="record current items as known without notifying")
    ap.add_argument("--dry-run", action="store_true",
                    help="print alerts instead of sending them")
    ap.add_argument("--hello", action="store_true",
                    help="send a test message to verify the Telegram setup")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()

    if args.hello:
        send_telegram(cfg, "\u2705 watch.py is configured correctly.")
        log("test message sent")
        return

    for feed in cfg.get("feeds", []):
        if not feed.get("enabled", True):
            log(f"feed '{feed.get('name')}' disabled, skipping")
            continue
        check_feed(cfg, feed, state, notify=not (args.bootstrap or args.dry_run))

    if args.dry_run or args.bootstrap:
        save_state(state)

    if maybe_update_heartbeat(state):
        save_state(state)
        log("heartbeat updated")


if __name__ == "__main__":
    main()
