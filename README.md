# pulpline

[![CI](https://github.com/wtfnukee/pulpline/actions/workflows/ci.yml/badge.svg)](https://github.com/wtfnukee/pulpline/actions/workflows/ci.yml)

Local-first content pipeline for e-readers. Pulls articles, papers, newsletters, and manga from RSS feeds, Substack (paid + free), arXiv, MangaDex, and arbitrary URLs - converts each to a format your reader handles natively (EPUB / PDF / CBZ), and writes them to a folder you sync to your device. No cloud service. No subscription. No manual file shuffling.

## Status

`0.1.0` (beta). The full command surface ships: `add`, `sync`, `list`, `remove`, `migrate`, `import` (substack/opml), `mangadex` (add/extend), `search anna`, and an interactive `tui`. See [SPEC.md](SPEC.md) for the design and roadmap.

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

Five tabs:

- **Library**: every ingested item, grouped by subscription, filterable by
  title or URL. `/` focuses the filter, `enter` opens the highlighted file
  in your OS default app (Preview / xdg-open / Explorer), `d` soft-deletes
  it (file removed, ledger row kept so the next sync won't re-fetch).
  Collapse state on the group nodes survives refreshes, so deleting an
  item doesn't blow open every group you'd closed.
- **Subscriptions**: name / source / item count / last sync / status / URL,
  with the last error surfaced for any failing feed. `d` removes the
  highlighted subscription (config-only - already-written EPUBs stay).
- **Search**: type a query, hit `enter`, browse Anna's Archive results
  inline. `space` toggles a row, `a` toggles all, `d` downloads picked
  rows (or the cursor row if none picked). Quota left and per-download
  status surface in the line below the table. Same backend as
  `pulp search anna` from the CLI.
- **Sync**: per-subscription pipeline status as one merged list. `s`
  (from any tab) kicks off a sync; rows morph from "last synced at"
  timestamps into live `12/16  Title` progress while a sub is running,
  then settle on `+3 new` / `2 paywalled` / `no change` once finished.
  Below: Syncthing delivery status (devices online, folders, daemon
  version). When Syncthing isn't installed, the lower section says so
  and pulpline still shows its own state.
- **Stats**: total / 7-day / 30-day / 1-year counts, per-source bars,
  per-format bars (EPUB vs PDF), 30-day daily activity bars.

`tab` cycles tabs, `s` runs a sync, `r` refreshes data, `q` quits. Adding
a new view is one file in `src/pulpline/tui/views/` plus an entry in
`views/__init__.VIEWS`.

## Manga (MangaDex)

```bash
# Subscribe to a manga - new chapters land in `<output_dir>/<manga-slug>/` as CBZ
pulp add https://mangadex.org/title/abc123-uuid/berserk

# One-shot a single chapter
pulp add https://mangadex.org/chapter/xyz-uuid
```

Pulpline talks to MangaDex's public API directly (no auth needed), pulls
chapter images from MangaDex's at-home CDN, and zips them into CBZ files
that Boox / KOReader / Calibre handle natively. Default language is English
(`en`); the most recent 25 chapters per language are tracked per `pulp sync`.

To pick a different translation language or chapter window, use the
`pulp mangadex add` subcommand. It accepts `--language` (or `-l`) for any
MangaDex language code (`ja`, `ru`, `es`, `fr`, `de`, `zh`, `ko`, `pt-br`,
...) and `--max-chapters` to cap how many chapters are tracked:

```bash
pulp mangadex add 'https://mangadex.org/title/.../berserk' --language ja
```

Or edit `~/.config/pulpline/config.toml` directly. Source-specific knobs
live under `[subscriptions.options]`:

```toml
[[subscriptions]]
name = "berserk"
source = "mangadex"
url = "https://mangadex.org/title/.../berserk"

[subscriptions.options]
language = "ja"
```

One-shot chapter URLs (`/chapter/<id>`) don't need a language hint - the
chapter ID already names a specific translated chapter.

URLs on `mangadex.org` auto-route to the MangaDex source - no flag needed.
The output is a `.cbz` (rather than `.epub` / `.pdf`) and ends up in the
configured `output_dir` like every other source.

### Read-from-start workflow

For long-running manga where you want to read from chapter 1, subscribe with
`--from-start` (sets `order=asc`) and a small `--max-chapters`:

```bash
pulp mangadex add 'https://mangadex.org/title/.../berserk' -l ru --from-start --max-chapters 10
pulp sync                                 # downloads chapters 1-10

# read those, then bump
pulp mangadex extend berserk --by 10      # chapters 11-20 next sync
pulp mangadex extend berserk --to 50      # set absolute target
pulp mangadex extend berserk --all        # remove the cap entirely (paginate to end)
```

The TUI Library view shows `name (downloaded/total)` for MangaDex
subscriptions once a sync has reported the total - so `berserk (10/358)`
tells you at a glance how far you are.

### Default vs from-start

```bash
# Default: latest 25 chapters, ongoing-feed style (same as `pulp add <url>`)
pulp mangadex add 'https://mangadex.org/title/.../berserk' -l ru

# Latest 100 chapters
pulp mangadex add 'https://mangadex.org/title/.../berserk' -l ru --max-chapters 100

# All chapters (heavy: ~20 pages × N chapters of bandwidth)
pulp mangadex add 'https://mangadex.org/title/.../berserk' -l ru --max-chapters 0
```

**First-sync is heavy.** A 25-chapter sync downloads ~tens of MB; an
unbounded sync of a long-running manga downloads gigabytes. Subsequent
syncs only fetch new chapters.

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

## Search Anna's Archive

```bash
pulp search anna "Designing Data-Intensive Applications"
pulp search anna "DDIA" --ext epub --lang en --limit 10
pulp search anna "transformers" --content paper
```

Pulpline shows a numbered list of hits with title, authors, year, language,
format, and size. Pick a number to download; the file lands in
`<output_dir>/oneshots/` and gets recorded in the items ledger so re-runs
are deduped.

Filters: `--content` (`book` default, `paper`, `comic`, `magazine`),
`--ext` (`epub`, `pdf`, `mobi`, ...), `--lang` (ISO codes: `en`, `ru`,
`ja`, ...), `--limit` (max results, default 20).

### Why a donation key is required

Anna's Archive funds itself through donations and offers a fast,
CAPTCHA-free download API only to donors. Pulpline uses that API
(`fast_download.json`) for every download - no CAPTCHA breaking, no
mirror-roulette, no torrent fallback. **Get a key by donating at
[annas-archive.li/donate](https://annas-archive.li/donate)**, then put it
in your config (never in a git repo):

```toml
[auth.annas]
api_key = "..."                              # required
mirrors = ["gl", "pk", "gd"]                 # optional; if unset, pulpline asks SLUM
```

Or set `PULPLINE_ANNAS_API_KEY` in the environment if you'd rather keep it
out of files.

When `[auth.annas].mirrors` is unset, pulpline queries [SLUM](https://open-slum.org/)
(the Shadow Library Uptime Monitor) on first use to learn which mirrors
are currently up, and caches the answer at `~/.cache/pulpline/slum.json`
for 24h. So when a domain rotates, pulpline notices within a day rather
than failing silently. SLUM going down falls back to the same hardcoded
list as the example above.

Search itself does not need a key - it scrapes the same HTML the web UI
serves, with a real browser User-Agent so DDoS-Guard doesn't 403 us. Anna
[explicitly tells programmatic clients](https://annas-archive.li/llms.txt)
that there's no search API even for donors and points at the multi-TB
`aa_derived_mirror_metadata` torrent for offline indexing - which is not
laptop-scale, so we accept the HTML path with the etiquette of running one
query per user-typed command (no parallelism, no background scraping).

URLs on `annas-archive.{gl,pk,gd}/md5/<hash>` also work directly: `pulp
add https://annas-archive.gl/md5/abc...` is a one-shot download. The
`search` command is sugar that resolves a query to one of those URLs.

You are responsible for legal compliance in your jurisdiction.

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

### Auto-reconcile from cron

Once `[auth.substack]` is set, `pulp import substack --auto` runs
non-interactively against the username and cookies file already in
config: it fetches your current follow list, adds any new publications
as subscriptions, and is a no-op for ones you already have. Pair it
with `pulp sync` in cron / launchd to pick up newly-followed
publications without ever opening a prompt:

```
*/30 * * * *  pulp import substack --auto && pulp sync
```

### Custom-domain publications (ACX, etc.)

Some Substack publications run on their own domain (e.g.
`astralcodexten.com`). The cookies you exported from `substack.com`
**don't authenticate against those domains**, so paid posts come back
empty and pulpline reports them as `paywalled` rather than fetching
them. Export a separate cookies file from the custom domain while
logged in there, and point pulpline at both:

```toml
[auth.substack]
cookies_path = "~/.config/pulpline/substack-cookies.json"
extra_cookies_paths = [
  "~/.config/pulpline/astralcodexten-cookies.json",
]
```

Pulpline attaches each cookie under its source domain, so the right
one is sent to the right host.

When sync runs into a paywall, the per-subscription summary line
shows `2 paywalled` instead of bumping the error count. That's a hint
to refresh the cookies for that host, not a sign the pipeline is
broken.

### Saved-for-later posts

Substack lets you "save for later" while scrolling - that's a per-account
list across publications, not a feed. Pulpline picks it up as a single
subscription:

```bash
pulp add https://substack.com/inbox/saved
```

The next `pulp sync` (and every one after) fetches every newly-saved
post as an EPUB. Same `[auth.substack].cookies_path` covers it - if
`pulp import substack` already wrote that, you're done. If not, save
your cookies first and add a `[auth.substack]` block to `config.toml`.

Re-running with no new saves is a no-op (deduped via the items ledger).
Re-saving a post you already ingested is also a no-op for the same
reason.

Saved-post EPUBs are named `Title - Author.epub` so the saves folder
on the Boox is scannable at a glance (saves come from many publications,
so the bare title isn't always enough to recognize a piece). The EPUB
renderer also downloads and embeds inline `<img>` content, so articles
read without internet on the device - if the post had figures, they're
in the file.

## Email: ePub attachments + newsletters

Point pulpline at an IMAP mailbox and it treats the mailbox as a feed.
Every message is dispatched on its own merits:

- **Message has an ebook attachment** (epub/pdf/mobi/azw/cbz/...): the
  attachment is saved verbatim, one file per attachment. The email body is
  treated as a delivery envelope and ignored. This covers Calibre's
  "share by email", fanfic-site delivery bots, and forwarding books to
  yourself.
- **Anything else**: the HTML body is cleaned (tracking pixels, hidden
  preview text, and layout tables stripped; inline images embedded) and
  rendered to EPUB - so newsletters read like articles on the device.
  Text-only emails are skipped with an error; there's nothing to render.

The intended setup is a **dedicated mailbox** (a separate account, or a
folder that a mail filter routes into) so everything in it is meant for
the reader.

### Setup

```bash
# 1. Create an app password for the account (Gmail: enable 2FA, then
#    https://myaccount.google.com/apppasswords). Save it to a file:
mkdir -p ~/.config/pulpline
printf '%s' 'abcd efgh ijkl mnop' > ~/.config/pulpline/email_password
chmod 600 ~/.config/pulpline/email_password

# 2. Add the auth block to ~/.config/pulpline/config.toml:
#    [auth.email]
#    username = "you@gmail.com"
#    password_path = "~/.config/pulpline/email_password"

# 3. Subscribe. The URL is imaps://<host>/<folder> - folder omitted = INBOX.
pulp add imaps://imap.gmail.com/Pulpline

# 4. Sync as usual.
pulp sync
```

A ready-to-copy config block with every option lives in
[`examples/email.toml`](examples/email.toml).

The password never goes in `config.toml` - only the *path* to it (or set
`PULPLINE_EMAIL_PASSWORD` in the environment, which wins over the file).
`imaps://` means verified TLS on port 993; `imap://host` means STARTTLS
on 143.

### Options

Set under `[subscriptions.options]`:

| option        | default  | meaning                                                                 |
| ------------- | -------- | ----------------------------------------------------------------------- |
| `since_days`  | `60`     | how far back discovery looks. `0` = the whole folder every sync.        |
| `mark_read`   | `"seen"` | `"seen"` marks processed mail read, `"move"` moves it to `move_to`, `"none"` leaves the mailbox untouched. |
| `move_to`     | -        | destination folder, required with `mark_read = "move"`. Must exist.     |
| `unseen_only` | `0`      | `1` = only look at unread mail. A prefilter, not the dedup mechanism.   |
| `prefer_web`  | `0`      | `1` = follow the newsletter's "view in browser" link and extract the web version (better typography, but fetches over HTTP). |

### How it stays idempotent

Already-processed messages are tracked in pulpline's own ledger, keyed on
the email's `Message-ID` - **not** on the IMAP read flag. Your phone
marking the mailbox read (or a crash halfway through a sync) never causes
missed or duplicated books: anything not yet in the ledger is picked up
again on the next sync. `mark_read` runs only *after* an item is safely
recorded, purely so the mailbox reflects progress when you look at it.

Two consequences worth knowing:

- Deleting an EPUB from the library (TUI `d`) won't resurrect it on the
  next sync, same as every other source.
- A message that fails repeatedly (say, a malformed newsletter) stays
  unread in the mailbox and retries each sync until it ages out of the
  `since_days` window. `pulp backfill <name>` reaches past the window -
  it walks the entire folder newest-first, which is also the way to
  ingest a mailbox's whole history on day one.

Newsletter EPUBs are titled `Subject - Sender`. When two issues share a
subject line (looking at you, "Money Stuff"), the file gets the issue's
date appended (`Money Stuff (2026-07-03).epub`) instead of overwriting
the previous one.

## File organization + deletion

Pulpline lays out items in subfolders under your `output_dir`:

```
~/Sync/Pulpline/
├── samkriss/               # one folder per subscription (auto-named after sub)
│   ├── How to live without your phone.epub
│   └── ...
├── arxiv-cs-ai/
│   └── 2401.12345v1.pdf
├── etymology/
│   └── ...
└── oneshots/               # `pulp add <url>` items without a subscription
    └── What You Can't Say.epub
```

The folder name comes from the subscription's `name` automatically. To put a
specific subscription somewhere else, set `output_dir` on that subscription
in `~/.config/pulpline/config.toml`:

```toml
[[subscriptions]]
name = "berserk"
source = "mangadex"
url = "..."
output_dir = "~/Sync/Manga/Berserk"   # explicit override; no auto-subfolder
```

### Deleting read items

In the TUI Library view, `d` deletes the highlighted file. Pulpline does a
**soft delete**:

- The file is removed from disk (and Syncthing pushes the deletion to your reader)
- The item's row in the dedup ledger is kept, with `output_path` cleared
- The next `pulp sync` will *not* re-fetch the deleted article

Re-running `pulp add <url>` on a soft-deleted article re-ingests it - the
dedup row is updated in place. So `d` is "I'm done with this" and `pulp add`
is "actually I want it back."

The Library and Stats views only count currently-extant items; deleted
articles drop out of the stats once they're gone from disk.

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

## Re-rendering existing items

When the renderer improves (e.g., it gains image embedding, or you
flip on a new EPUB option), older items stay in the form they were
first written. To re-render them in place against the current code:

```bash
pulp migrate --rebuild                  # every item, every subscription
pulp migrate --rebuild --name samkriss  # just one subscription
```

`--rebuild` re-fetches each item from its source, runs the current
renderer over it, and overwrites the file at the same path the ledger
records. It does *not* re-discover new items - that's `pulp sync`'s
job. Use it when you want yesterday's saves to pick up today's
renderer behavior.

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

Cron and launchd often capture stderr into mail or `/dev/null`; pulpline
writes a durable log to `~/.local/state/pulpline/log/pulpline.log`
(rotated at 1 MB, 5 backups) so you can postmortem failed syncs without
relying on the scheduler's stderr handling. Override the directory with
`PULPLINE_LOG_DIR=/path/to/log/dir`. Use `pulp -v <cmd>` to mirror
DEBUG-level logs to stderr in real time.

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
