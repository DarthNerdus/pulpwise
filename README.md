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

# Batch - any mix of feeds and articles, each classified independently
pulp add https://a.example/feed https://b.example/article https://c.example/post

# Paste a list of tab URLs from your clipboard
pbpaste | xargs pulp add

# Sync new items from all subscriptions
pulp sync

# List subscriptions and last-sync status
pulp list

# Remove a subscription (does not delete already-written EPUBs)
pulp remove simon-willison-s-weblog
```

`pulp add` auto-classifies each URL: real feeds get subscribed, single articles get one-shot. Use `--once` or `--feed` to override for the whole call. Errors on one URL do not abort the rest of the batch.

## TUI

For a more interactive view of your library and ingestion stats:

```bash
pulp tui
```

Two tabs:

- **Library**: every ingested item, filterable by title or URL. `/` focuses
  the filter, `enter` opens the highlighted file in your OS default app
  (Preview / xdg-open / Explorer).
- **Stats**: total / 7-day / 30-day / 1-year counts, per-source bars,
  per-format bars (EPUB vs PDF), 30-day daily activity bars.

`tab` cycles tabs, `r` refreshes data, `q` quits. Adding a new view is one
file in `src/pulpline/tui/views/` plus an entry in `views/__init__.VIEWS`.

## arXiv papers

Pulpline ships an `arxiv` source that downloads the actual PDF instead of
extracting the abstract page. Math papers' figures and equations live in the
PDF; running them through trafilatura would destroy that.

```bash
# Single paper one-shot - paste any arxiv.org URL
pulp add https://arxiv.org/abs/2401.12345

# Subscribe to a category feed (cs.AI papers, latest 25, sorted by submission)
pulp add 'http://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=25'

# Or by author
pulp add 'http://export.arxiv.org/api/query?search_query=au:Hinton&max_results=20'

# Or by keyword
pulp add 'http://export.arxiv.org/api/query?search_query=all:transformers&max_results=20'
```

URLs on `arxiv.org` / `export.arxiv.org` auto-route to the arXiv source - no
flag needed. The output is a `.pdf` (not `.epub`) and lands in the same
configured `output_dir` as everything else, where Boox readers index it
natively.

For URL formats, see arXiv's [API user manual](https://info.arxiv.org/help/api/user-manual.html#query_details).
The `search_query` field accepts category codes (`cat:cs.AI`), authors
(`au:lastname`), keyword search (`all:phrase`, `ti:title`, `abs:abstract`),
and Boolean combinations.

## Bulk import from a feed reader (OPML)

Most feed readers (Reeder, NetNewsWire, Inoreader, Feedly, ...) export
your subscriptions as OPML. Pulpline reads that file directly:

```bash
pulp import opml ~/Downloads/subscriptions.opml
# shows a numbered list of every feed in the file
# pick which to import: 'all', 'none', or '1,3,5-7'
```

Existing subscription names are silently skipped, so re-importing the same
file is safe. Folders/categories from the OPML are preserved as labels in
the prompt but don't change pulpline's flat config.

## Substack with paid subscriptions

Substack's public `/feed` URLs only carry free posts. To bring paid posts into
pulpline, export your logged-in session cookies and use the bulk-import
command:

```bash
# 1. Export cookies from your browser. A "cookies.json" extension that exports
#    the JSON-array format works (each entry has at least `name` and `value`).
#    Save the file somewhere private, e.g. ~/.config/pulpline/substack-cookies.json

# 2. Import all your subscriptions in one shot:
pulp import substack <your-substack-handle> --cookies ~/.config/pulpline/substack-cookies.json
```

The command:

1. Reads your cookies file
2. Calls Substack's user-profile endpoint to list every publication you follow
3. Shows them as a numbered list and prompts: `1,3,5-7` / `all` / `none`
4. Writes selected ones to `config.toml` as `source = "substack"` subscriptions
5. Records the cookies path under `[auth.substack]` so future syncs use it

After import, `pulp sync` calls Substack's authenticated post API for each
subscription, which returns full body HTML for paid posts you have access to.
Cookies expire after a few weeks; when they do, re-export and update the
`auth.substack.cookies_path` (or just re-run `pulp import substack`).

Cookie files contain session credentials - treat them like passwords. They
sit in `~/.config/pulpline/` by convention, which is `chmod 600`-able.

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
