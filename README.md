# pulpline

Local-first content pipeline for e-readers. Pulls articles, papers, and newsletters from RSS feeds and arbitrary URLs, converts them to EPUBs with full Dublin Core metadata, and writes them to a folder you sync to your reader. No cloud service. No subscription. No manual file shuffling.

## Status

Pre-alpha (`0.1.0.dev0`). The MVP command surface (`add`, `sync`, `list`, `remove`) works end-to-end. See [SPEC.md](SPEC.md) for the design and roadmap.

## Install

```bash
# Once published to PyPI:
pipx install pulpline
```

Until then, build and install from source:

```bash
git clone <repo>
cd pulpline
make build
uv tool install --force ./dist/pulpline-*.whl
```

Requires Python 3.14+. macOS and Linux are first-class targets; Windows is unsupported.

## Quick start

```bash
# Subscribe to a feed (auto-detected as feed)
pulp add https://simonwillison.net/atom/everything/

# Fetch a single article (auto-detected as article)
pulp add https://stratechery.com/2026/the-end-of-the-beginning/

# Sync new items from all subscriptions
pulp sync

# List subscriptions and last-sync status
pulp list

# Remove a subscription (does not delete already-written EPUBs)
pulp remove simon-willison-s-weblog
```

`pulp add` auto-classifies the URL: real feeds get subscribed, single articles get one-shot. Use `--once` or `--feed` to override when detection guesses wrong.

## Configuration

`~/.config/pulpline/config.toml` is created on first run. Hand-editable:

```toml
[paths]
output_dir = "~/Sync/Pulpline"           # where EPUBs land

[[subscriptions]]
name = "stratechery"
source = "rss"
url = "https://stratechery.com/feed"

[[subscriptions]]
name = "berserk"
source = "mangadex"                      # post-MVP, not yet shipped
url = "..."
output_dir = "~/Sync/Manga/Berserk"      # per-subscription override
```

State (the dedup ledger and per-subscription run state) lives in `~/.local/share/pulpline/state.db`. Removing it forces a full re-sync of every subscription.

## Scheduling

pulpline has no built-in scheduler - point your OS at it. macOS:

```
# ~/Library/LaunchAgents/com.pulpline.sync.plist (excerpt)
<key>StartCalendarInterval</key>
<dict><key>Minute</key><integer>0</integer></dict>
<key>ProgramArguments</key>
<array>
  <string>/usr/local/bin/pulp</string>
  <string>sync</string>
</array>
```

Linux (cron):

```
0 * * * *  /home/you/.local/bin/pulp sync
```

systemd users can write a simple `.timer` unit pointing at `pulp sync`.

## How it gets to the reader

pulpline writes EPUBs to a folder; getting them onto the device is your problem. The intended pairing is [Syncthing](https://syncthing.net/) - it syncs `~/Sync/Pulpline` to a folder on the device, and Boox / Kindle / Kobo readers index whatever shows up. Other paths work too: USB drag-and-drop, Send-to-Kindle, BooxDrop, etc.

## Development

```bash
make sync          # uv sync runtime + dev deps
make check         # ruff + mypy strict + 80 pytests + coverage gate
make test
make typecheck
make build         # wheel + sdist into dist/
```

Pulpline pins PyPI as its default index. If your shell exports a non-PyPI `UV_INDEX`, the bundled `Makefile` strips it before invoking `uv`. Direnv users can `direnv allow` to get the same effect via `.envrc`.

## License

MIT
