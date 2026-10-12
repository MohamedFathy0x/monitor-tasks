# monitor-tasks

Personal feed watchers with Telegram alerts. Standard library only.

## How it works

`watch.py` checks the feeds configured in `config.json` on a fixed cadence and
sends a Telegram message whenever a new item shows up on a feed. Seen items are
recorded in `state.json` so every alert is sent exactly once. A heartbeat file
keeps the scheduling host active.

## Usage

    python watch.py                 # one monitoring cycle
    python watch.py --bootstrap     # record current items as known, send nothing
    python watch.py --dry-run       # print alerts without sending
    python watch.py --hello         # verify the Telegram configuration

Credentials are provided via environment variables (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`) or directly inside `config.json`.

## Stand-down and re-arming

With `"stop_after_alert": true` in `config.json`, the watcher fires the alert
exactly once, records the items, and then stands down: the runner loop stops
and the scheduled workflow disables itself so nothing re-fires. State is
persisted base64-encoded so no feed identifiers appear in the repository.

To re-arm for a new target later:

1. point the `FEED_URL` repository variable at the new feed,
2. run the workflow once in `bootstrap` mode (re-baselines items, clears the
   stand-down flag),
3. enable the workflow and dispatch a `normal` run.
