# Pulp Wise

[![CI](https://github.com/DarthNerdus/pulpwise/actions/workflows/ci.yml/badge.svg)](https://github.com/DarthNerdus/pulpwise/actions/workflows/ci.yml)

Content pipeline whose only output is [Readwise Reader](https://readwise.io/read). Pulls articles, papers, and newsletters from RSS feeds, Substack (paid + free + your saved-for-later list), arXiv, an IMAP mailbox, and arbitrary URLs - filters and dedups them locally, then pushes each one into your Reader account via the Reader API. One reading queue, curated before anything reaches it.

Pulp Wise is a fork of [pulpline](https://github.com/wtfnukee/pulpline) with the file pipeline removed. Where pulpline converts content to EPUB/PDF/CBZ and writes files you sync to an e-reader, Pulp Wise ends at `POST /api/v3/save/`. No output folder, no Syncthing, no rendering. Manga (MangaDex), books (Anna's Archive), and emailed ebook attachments remain mainline pulpline's job. The two tools coexist: separate package (`pulpwise`), commands (`pulpwise` / `pw`), config (`~/.config/pulpwise/`), state (`~/.local/share/pulpwise/`), and env vars (`PULPWISE_*`).

## Status

`0.1.0` (beta). The full command surface ships: `add`, `sync`, `list`, `remove`, `backfill`, `import` (substack/opml), and an interactive `tui`. See [SPEC.md](SPEC.md) for the design.

## Why not just use Reader's own RSS and email addresses?

Reader can subscribe to RSS feeds itself, and gives you an email address newsletters can be delivered to. If that covers you, use it directly - it's less machinery. Pulp Wise earns its place when you want:

- **Category filtering on feeds** - subscribe to a mixed feed but only push entries tagged "Recommended Reading", or drop "Sponsored" and "Podcast".
- **Cookie-authenticated Substack** - entitled paid posts pushed as full HTML, and your Substack saved-for-later list treated as a subscription.
- **A dedup ledger you own, with delete-tombstones** - Reader's server-side dedup only lives as long as a document exists; delete a document there and a naive re-submitter recreates it on the next sync. Pulp Wise remembers what you deleted and never re-pushes it.
- **Local subscription management** - subscriptions in one TOML file you can version, OPML import from any feed reader, and cron-driven auto-reconcile of your Substack follows.

If you don't need any of those, Reader alone is the right tool.

## Install

```bash
# Once published to PyPI:
pipx install pulpwise
```

Until then, build and install from source:

```bash
git clone <repo>
cd pulpwise
make build
uv tool install --force ./dist/pulpwise-*.whl
```

Requires Python 3.14+. macOS and Linux are first-class targets; Windows is unsupported.

## Setup: the Readwise token

Everything Pulp Wise does ends in a Reader API call, so the one required piece of setup is an access token. Get one at [readwise.io/access_token](https://readwise.io/access_token), then either put it in config:

```toml
# ~/.config/pulpwise/config.toml
[auth.readwise]
token = "XXX"
```

or - better - point at a file holding just the token:

```bash
printf '%s' 'XXX' > ~/.config/pulpwise/readwise_token
chmod 600 ~/.config/pulpwise/readwise_token
```

```toml
[auth.readwise]
token_path = "~/.config/pulpwise/readwise_token"
```

The `PULPWISE_READWISE_TOKEN` environment variable wins over both - useful for keeping secrets out of files entirely.

## Quick start

```bash
# Subscribe to a feed (auto-detected as feed)
pulpwise add https://simonwillison.net/atom/everything/

# Push a single article into Reader (auto-detected as one-shot;
# prints the Reader document URL)
pulpwise add https://stratechery.com/2026/the-end-of-the-beginning/

# Batch - any mix of feeds and articles, each classified independently
pulpwise add https://a.example/feed https://b.example/article https://c.example/post

# Paste a list of tab URLs from your clipboard
pbpaste | xargs pulpwise add

# Push new items from all subscriptions into Reader
pulpwise sync

# List subscriptions and last-sync status
pulpwise list

# Remove a subscription (does not touch documents already in Reader)
pulpwise remove simon-willison-s-weblog
```

`pw` is the short alias for all of these. `pulpwise add` auto-classifies each URL: real feeds get subscribed, single articles get one-shot pushed. Use `--once` or `--feed` to override for the whole call, `--name` to name the subscription. Errors on one URL do not abort the rest of the batch, and re-adding a URL that's already in Reader is a no-op that prints the existing document URL.

### How a save happens

Pulp Wise submits documents to Reader in one of two modes, chosen per item:

- **URL saves** for public content (RSS entries, arXiv PDFs, free Substack posts, plain web pages): the bare URL is submitted and Reader fetches + parses it server-side. Reader owns the extraction, which stays fixable on their end.
- **HTML content submissions** for content Reader's fetcher can't reach (entitled paid Substack posts fetched with your session cookies, email newsletter bodies): Pulp Wise pushes the HTML itself, with explicit title/author/date and `should_clean_html` so Reader normalizes it. Emails without a web permalink get a deterministic synthetic URL (`https://pulpwise.invalid/<hash>`), since Reader requires a URL on every save.

Reader dedupes by exact URL: the save endpoint answers 201 for a new document and 200 when it already had that URL. Reader never re-parses saved content, so content submissions are effectively write-once - fixing a broken body means delete + re-save, which loses highlights.

The save endpoint is rate-limited at 50 saves/minute per token. Pulp Wise paces itself at 45/minute and honors `Retry-After` on 429; if the limiter stays angry, the current subscription stops early and the remaining items defer to the next sync (nothing is lost - unpushed items aren't in the ledger yet).

### Filtering a feed by category

Feeds that mix content types often tag entries with categories (RSS
`<category>`, Atom `<category term=...>`, `<dc:subject>`). Two per-subscription
options filter on them — edit the subscription in `config.toml`:

```toml
[[subscriptions]]
name = "badlogic-links"
source = "rss"
url = "https://badlogic-list.lakebed.app/rss"

[subscriptions.options]
categories = "Recommended Reading"          # only entries with a listed category
# exclude_categories = "Sponsored, Podcast" # drop entries with a listed category
```

Both take comma-separated category names, matched case-insensitively against
each entry's full category list. `categories` is an allow-list: entries
without any listed category are skipped, including untagged entries.
`exclude_categories` is a drop-list: it only removes matches, so untagged
entries still come through — and it wins when both options match the same
entry. Filtering happens at discovery, so skipped entries are never pushed
and never enter the dedup ledger; if you loosen the filter later, previously
skipped entries still in the feed are picked up on the next sync.

This is the point of putting Pulp Wise in front of Reader's own RSS support:
manage and filter subscriptions here, and only the keepers land in your
Reader account.

### Choosing where saves land

Two more per-subscription options control Reader-side routing:

```toml
[subscriptions.options]
location = "new"         # new | later | archive | feed (default: feed)
tags = "tech, essays"    # comma-separated Reader tags
```

`location` is where this subscription's saves land in Reader. **Unset, it
defaults to `feed`** - Pulp Wise acts as a feed reader in front of Reader,
so pushed items join the Feed section like native RSS instead of flooding
your inbox; set `location = "new"` on the subscriptions whose picks you
want in the triage flow. One-shot `pulpwise add <url>` takes the same
choice via `--location` (also defaulting to `feed`). Note that Reader
silently falls back to your account default if you target a location
you've disabled in your Reader settings - there's no error to catch.
`tags` applies the listed Reader tags to every document the subscription
pushes.

## TUI

For a more interactive view of your library and push stats:

```bash
pulpwise tui
```

Four tabs:

- **Library**: every pushed item, grouped by subscription, filterable by
  title or URL. `/` focuses the filter, `enter` opens the highlighted
  document in Reader in your browser, `d` deletes it locally (tombstoned;
  the Reader document is left alone - Pulp Wise never deletes from
  Readwise), `D` toggles a view of recently deleted items. Collapse state on the group nodes survives
  refreshes, so deleting an item doesn't blow open every group you'd
  closed.
- **Subscriptions**: name / source / item count / last sync / status / URL,
  with the last error surfaced for any failing feed. `d` removes the
  highlighted subscription (config-only - documents already in Reader stay).
- **Sync**: per-subscription pipeline status as one merged list. `s`
  (from any tab) kicks off a sync; rows morph from "last synced at"
  timestamps into live `12/16  Title` progress while a sub is running,
  then settle on `+3 new` / `2 paywalled` / `no change` once finished.
  Below: Readwise token status, so an expired or missing token is visible
  before a sync fails on it.
- **Stats**: total / 7-day / 30-day / 1-year counts, per-source bars,
  per-kind bars (`url` = URL saves, `html` = content submissions, `file` =
  legacy pulpline items from a dropped-in database), 30-day daily
  added/deleted activity bars.

Switch tabs with the arrow keys on the tab bar (or the mouse); `s` runs a
sync, `r` refreshes data, `q` quits. Adding
a new view is one file in `src/pulpwise/tui/views/` plus an entry in
`views/__init__.VIEWS`.

## arXiv papers

Papers are submitted to Reader as the `/pdf/<id>` URL with an explicit
`pdf` category hint - Reader ingests PDFs by URL, and the PDF preserves
the figures and equations that any text-extraction pipeline destroys.

```bash
# Single paper one-shot - paste any arxiv.org URL (/abs/ or /pdf/)
pulpwise add https://arxiv.org/abs/2401.12345

# Subscribe to a category feed (cs.AI papers, latest 25, sorted by submission)
pulpwise add 'http://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=25'

# Or by author
pulpwise add 'http://export.arxiv.org/api/query?search_query=au:Hinton&max_results=20'

# Or by keyword
pulpwise add 'http://export.arxiv.org/api/query?search_query=all:transformers&max_results=20'
```

URLs on `arxiv.org` / `export.arxiv.org` auto-route to the arXiv source - no
flag needed. `api/query` URLs subscribe; `/abs/` and `/pdf/` links one-shot.
A one-shot does one metadata round-trip first so the ledger and Reader get a
clean title and date instead of whatever the PDF parse guesses.

For URL formats, see arXiv's [API user manual](https://info.arxiv.org/help/api/user-manual.html#query_details).
The `search_query` field accepts category codes (`cat:cs.AI`), authors
(`au:lastname`), keyword search (`all:phrase`, `ti:title`, `abs:abstract`),
and Boolean combinations.

## Bulk import from a feed reader (OPML)

Most feed readers (Reeder, NetNewsWire, Inoreader, Feedly, ...) export
your subscriptions as OPML. Pulp Wise reads that file directly:

```bash
pulpwise import opml ~/Downloads/subscriptions.opml
# shows a numbered list of every feed in the file
# pick which to import: 'all', 'none', or '1,3,5-7'
```

Existing subscription names are silently skipped, so re-importing the same
file is safe. Folders/categories from the OPML are preserved as labels in
the prompt but don't change Pulp Wise's flat config.

## Substack with paid subscriptions

Substack's public `/feed` URLs only carry free posts. To bring paid posts into
Reader, export your logged-in session cookies and use the bulk-import
command:

```bash
# 1. Export cookies from your browser. A "cookies.json" extension that exports
#    the JSON-array format works (each entry has at least `name` and `value`).
#    Save the file somewhere private, e.g. ~/.config/pulpwise/substack-cookies.json

# 2. Import all your subscriptions in one shot:
pulpwise import substack <your-substack-handle> --cookies ~/.config/pulpwise/substack-cookies.json
```

The command:

1. Reads your cookies file
2. Calls Substack's user-profile endpoint to list every publication you follow
3. Shows them as a numbered list and prompts: `1,3,5-7` / `all` / `none`
4. Writes selected ones to `config.toml` as `source = "substack"` subscriptions
5. Records the cookies path under `[auth.substack]` so future syncs use it

After import, `pulpwise sync` calls Substack's authenticated post API for each
subscription. Free posts are pushed as bare URL saves (Reader fetches the
public page itself); paid posts you're entitled to are fetched with your
cookies and pushed as HTML content submissions, since Reader's own fetcher
carries no cookies and would only see the paywall. Cookies expire after a
few weeks; when they do, re-export and update
`auth.substack.cookies_path` (or just re-run `pulpwise import substack`).

Cookie files contain session credentials - treat them like passwords. They
sit in `~/.config/pulpwise/` by convention, which is `chmod 600`-able.

Worth saying plainly: the post and profile endpoints involved are Substack's
private, unofficial API. They can change without notice, and keeping up with
that churn is an accepted maintenance cost of this feature.

### Auto-reconcile from cron

Once `[auth.substack]` is set, `pulpwise import substack --auto` runs
non-interactively against the username and cookies file already in
config: it fetches your current follow list, adds any new publications
as subscriptions, and is a no-op for ones you already have. Pair it
with `pulpwise sync` in cron / launchd to pick up newly-followed
publications without ever opening a prompt:

```
*/30 * * * *  pulpwise import substack --auto && pulpwise sync
```

Reconciliation only ever runs when you invoke it. The TUI can also run it
automatically before each sync, but that is **opt-in** - on a fresh config
it would silently add your entire Substack follow list, so it's off unless
you ask for it:

```toml
[auth.substack]
auto_reconcile = true    # TUI sync also reconciles follows first
```

### Custom-domain publications (ACX, etc.)

Some Substack publications run on their own domain (e.g.
`astralcodexten.com`). The cookies you exported from `substack.com`
**don't authenticate against those domains**, so paid posts come back
empty and Pulp Wise reports them as `paywalled` rather than pushing
a stub. Export a separate cookies file from the custom domain while
logged in there, and point Pulp Wise at both:

```toml
[auth.substack]
cookies_path = "~/.config/pulpwise/substack-cookies.json"
extra_cookies_paths = [
  "~/.config/pulpwise/astralcodexten-cookies.json",
]
```

Pulp Wise attaches each cookie under its source domain, so the right
one is sent to the right host.

When sync runs into a paywall, the per-subscription summary line
shows `2 paywalled` instead of bumping the error count. That's a hint
to refresh the cookies for that host, not a sign the pipeline is
broken.

### Saved-for-later posts

Substack lets you "save for later" while scrolling - that's a per-account
list across publications, not a feed. Pulp Wise picks it up as a single
subscription:

```bash
pulpwise add https://substack.com/inbox/saved
```

The next `pulpwise sync` (and every one after) pushes every newly-saved
post into Reader: free saves as URL saves, entitled paid saves as HTML
content submissions, and paid saves you're not entitled to reported as
paywalled (fix the cookies for that host). Same `[auth.substack].cookies_path`
covers it - if `pulpwise import substack` already wrote that, you're done.
If not, save your cookies first and add a `[auth.substack]` block to
`config.toml`.

Re-running with no new saves is a no-op (deduped via the items ledger).
Re-saving a post you already pushed is also a no-op for the same reason.

## Email newsletters

Point Pulp Wise at an IMAP mailbox and it treats the mailbox as a feed of
newsletters. Each HTML email is cleaned (tracking pixels, hidden preview
text, and layout tables stripped) and pushed to Reader as a content
submission - title from the Subject line, author from the sender. Emails
without a "view in browser" permalink get a deterministic synthetic URL
(`https://pulpwise.invalid/<hash>`), since Reader requires a URL on every
save.

Two kinds of message are not pushed:

- **Messages with ebook attachments** (epub/pdf/mobi/...) are skipped
  entirely. Book deliveries - Calibre's "share by email", fanfic delivery
  bots - are mainline pulpline's job; Reader is for articles.
- **Text-only emails** are skipped with an error; there's no HTML to push.

The intended setup is a **dedicated mailbox** (a separate account, or a
folder that a mail filter routes into) so everything in it is meant for
Reader.

### Setup

```bash
# 1. Create an app password for the account (Gmail: enable 2FA, then
#    https://myaccount.google.com/apppasswords). Save it to a file:
mkdir -p ~/.config/pulpwise
printf '%s' 'abcd efgh ijkl mnop' > ~/.config/pulpwise/email_password
chmod 600 ~/.config/pulpwise/email_password

# 2. Add the auth block to ~/.config/pulpwise/config.toml:
#    [auth.email]
#    username = "you@gmail.com"
#    password_path = "~/.config/pulpwise/email_password"

# 3. Subscribe. The URL is imaps://<host>/<folder> - folder omitted = INBOX.
pulpwise add imaps://imap.gmail.com/Pulpwise

# 4. Sync as usual.
pulpwise sync
```

A ready-to-copy config block with every option lives in
[`examples/email.toml`](examples/email.toml).

The password never goes in `config.toml` - only the *path* to it (or set
`PULPWISE_EMAIL_PASSWORD` in the environment, which wins over the file).
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
| `prefer_web`  | `0`      | `1` = when a "view in browser" permalink is found, submit that permalink as a bare URL save (Reader fetches the web version - usually better typography) instead of pushing the cleaned email body. Falls back to the email body when no permalink exists. |

### How it stays idempotent

Already-processed messages are tracked in Pulp Wise's own ledger, keyed on
the email's `Message-ID` - **not** on the IMAP read flag. Your phone
marking the mailbox read (or a crash halfway through a sync) never causes
missed or duplicated pushes: anything not yet in the ledger is picked up
again on the next sync. `mark_read` runs only *after* an item is safely
recorded, purely so the mailbox reflects progress when you look at it.

Two consequences worth knowing:

- Deleting an item from the library (TUI `d`) won't resurrect it on the
  next sync, same as every other source.
- A message that fails repeatedly (say, a malformed newsletter) stays
  unread in the mailbox and retries each sync until it ages out of the
  `since_days` window. `pulpwise backfill <name>` reaches past the window -
  it walks the entire folder newest-first, which is also the way to
  ingest a mailbox's whole history on day one.

## Backfilling archives

`pulpwise sync` only looks at the newest page of each subscription. To walk
an archive backwards:

```bash
pulpwise backfill <subscription-name>
```

Substack publications walk their archive by offset, the Substack saves list
walks by save time, and email walks the whole folder newest-first. RSS and
arXiv feeds only expose their current window, so they can't backfill.

Already-pushed items (including deleted ones) are skipped but don't stop
the walk, so sparse holes get filled. The walk stops after a bounded number
of new saves per run (50 by default - deliberate, given the 50-saves/minute
API budget); re-running resumes where it left off via the dedup ledger.

## Deleting items

In the TUI Library view, `d` deletes the highlighted item:

- The ledger row is kept but tombstoned (`deleted_at` set)
- The Reader document is **not** touched - Pulp Wise is push-only and never
  deletes from Readwise; remove the document in Reader yourself if you want
  it gone there
- The next `pulpwise sync` will *not* re-push the deleted article

The tombstone is load-bearing: Reader's dedup only knows about documents
that exist, so without it, anything you deleted (in the TUI *or* in Reader
itself, once the item ages back into a feed) would be quietly recreated on
the next sync. Re-running `pulpwise add <url>` on a deleted article
re-pushes it deliberately - the tombstone is cleared in place. So `d` is
"I'm done with this" and `pulpwise add` is "actually I want it back."

## Configuration

`~/.config/pulpwise/config.toml` is created on first run. Hand-editable:

```toml
[auth.readwise]
token_path = "~/.config/pulpwise/readwise_token"

[[subscriptions]]
name = "stratechery"
source = "rss"
url = "https://stratechery.com/feed"

[[subscriptions]]
name = "badlogic-links"
source = "rss"
url = "https://badlogic-list.lakebed.app/rss"

[subscriptions.options]
categories = "Recommended Reading"
location = "feed"
tags = "links"
```

There is no `[paths]` section and no `output_dir` anywhere - Pulp Wise
writes no files. Legacy pulpline keys are tolerated and ignored on load
(so a copied-over config doesn't crash) and dropped on the next save.

State (the dedup ledger and per-subscription run state) lives in
`~/.local/share/pulpwise/state.db`. Each row records what was pushed
(`readwise_id`, `readwise_url`, and whether it was a URL save or an HTML
submission) plus the tombstones described above. A legacy pulpline
`state.db` can be dropped in directly: the columns migrate in place, and
items you already read as files are never re-pushed to Reader - any
existing row counts as ingested.

Deleting `state.db` does **not** give you a clean slate the way it did in
pulpline: the next sync re-pushes whatever is still in your feeds,
including documents you deleted in Reader, because the tombstones are gone.

## Scheduling

Pulp Wise has no built-in scheduler - point your OS at it. macOS:

```
# ~/Library/LaunchAgents/com.pulpwise.sync.plist (excerpt)
<key>StartCalendarInterval</key>
<dict><key>Minute</key><integer>0</integer></dict>
<key>ProgramArguments</key>
<array>
  <string>/usr/local/bin/pulpwise</string>
  <string>sync</string>
</array>
```

Linux (cron):

```
0 * * * *  /home/you/.local/bin/pulpwise sync
```

systemd users can write a simple `.timer` unit pointing at `pulpwise sync`.

Cron and launchd often capture stderr into mail or `/dev/null`; Pulp Wise
writes a durable log to `~/.local/state/pulpwise/log/pulpwise.log`
(rotated at 1 MB, 5 backups) so you can postmortem failed syncs without
relying on the scheduler's stderr handling. Override the directory with
`PULPWISE_LOG_DIR=/path/to/log/dir`. Use `pulpwise -v <cmd>` to mirror
DEBUG-level logs to stderr in real time.

## Development

```bash
make sync          # uv sync runtime + dev deps
make check         # ruff + mypy strict + pytest + coverage gate
make test
make typecheck
make build         # wheel + sdist into dist/
```

[mise](https://mise.jdx.dev) users get the same targets as tasks: `mise
install` provisions Python 3.14 + uv, then `mise run check` (or `sync`,
`test`, `lint`, `format`, `typecheck`, `build`). Per-machine overrides go
in `mise.local.toml`, which is gitignored.

Pulp Wise pins PyPI as its default index. If your shell exports a non-PyPI
`UV_INDEX`, the bundled `Makefile` strips it before invoking `uv`; the mise
config blanks it for anything run through mise; and direnv users can
`direnv allow` to get the same effect via `.envrc`.

## License

MIT
