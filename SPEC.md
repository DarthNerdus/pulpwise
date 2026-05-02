# Pulpline Specification

## 1. Context & Scope

**Vision:** A local-first content pipeline for e-readers. Pulls articles, papers, newsletters, and (eventually) manga from heterogeneous sources, converts them to e-reader-friendly formats, and writes them to a synced folder. No cloud service, no subscription, no manual file shuffling.

**Primary user:** myself. Technical user (CLI, TOML, Python). Reads on a Boox Palma 2.

**Secondary user (post-v0.1):** the e-ink enthusiast community on r/Onyx_Boox, r/eink, HN. Same technical baseline (assumes user can `pipx install`, edit a config file, and run things on a schedule). Not aimed at non-technical readers - Calibre and Readwise serve them.

**Non-goals (explicit, permanent):**
- Team / multi-user setups
- Hosted SaaS or web service
- Mobile-first or GUI configuration
- Windows-first support (macOS + Linux are primary)
- Sync transport: Syncthing is the user's responsibility. Pulpline writes files to a path, that is the entire contract.
- Built-in scheduling daemon: use cron / launchd / systemd timers
- Cloud sinks (BooxDrop, Dropbox, etc.) for v0.1; reconsiderable post-MVP, but never the default

**Smell test for "done":** I personally use it for a week to read Substack and saved articles on my Palma 2 without touching Calibre or copying files manually. If I reach for the manual flow even once, MVP is not done.

## 2. Technical Architecture

### Stack
- **Language:** Python 3.14+
- **Package / env manager:** uv (everything: `uv run`, `uv pip`, `uv venv`, `uv sync`)
- **Build backend:** hatchling
- **Distribution:** PyPI wheel, installable via `pipx install pulpline`
- **CLI framework:** typer
- **HTTP:** httpx (sync; async deferrable)
- **RSS / Atom:** feedparser
- **HTML to readable content:** trafilatura
- **EPUB generation:** ebooklib
- **Config:** TOML via stdlib `tomllib` (read), `tomli-w` (write)
- **State:** SQLite via stdlib `sqlite3`
- **Type checking:** mypy strict
- **Tests:** pytest, target >=70% coverage on `pipeline` + `sources/` + `renderers/`
- **License:** MIT

### Project layout (src/ layout)
```
pulpline/
  pyproject.toml           # hatchling, two console_scripts entry points: pulpline, pulp
  README.md
  LICENSE
  src/pulpline/
    __init__.py
    cli.py                 # typer app: add, sync, list, remove
    config.py              # TOML load/save, schema, defaults, auto-creation
    state.py               # SQLite schema, dedup queries
    pipeline.py            # add/sync orchestration; the only place sources/renderers/sinks meet
    sources/
      __init__.py          # in-tree registry: dict[str, type[Source]]
      base.py              # Source ABC: discover() -> Iterable[ItemRef]; fetch(item) -> RawArticle
      rss.py
      url.py
    renderers/
      __init__.py
      base.py              # Renderer ABC (single impl in v0.1; abstraction kept thin)
      epub.py
    sinks/
      __init__.py
      filesystem.py
    util/
      http.py              # shared httpx client (UA, timeouts)
      logging.py
      slugify.py           # filename sanitization
  tests/
    ...
```

### Filesystem layout (XDG-style, auto-created on first invocation)
- Config: `~/.config/pulpline/config.toml`
- State DB: `~/.local/share/pulpline/state.db`
- Logs: stderr only for v0.1; file logging deferred to post-MVP

### Config schema (`~/.config/pulpline/config.toml`)
```toml
[paths]
output_dir = "~/Sync/Pulpline"          # global default; ~ expansion supported

[[subscriptions]]
name = "stratechery"                    # unique key; used as OPF dc:subject tag and dedup namespace
source = "rss"
url = "https://stratechery.com/feed"
# inherits paths.output_dir

[[subscriptions]]
name = "berserk"                        # post-MVP example
source = "mangadex"
url = "..."
output_dir = "~/Sync/Manga/Berserk"     # per-subscription override
```

First-run behavior: if `config.toml` does not exist, `pulp` creates it with sensible defaults and commented examples. No `init` subcommand needed.

### State schema (SQLite)
TOML stays the source of truth for subscriptions. SQLite holds only the dedup ledger and per-subscription run cache.

```sql
CREATE TABLE subscription_state (
  name TEXT PRIMARY KEY,
  last_synced_at TEXT,
  last_status TEXT,                     -- 'ok' | 'error'
  last_error TEXT
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_name TEXT,               -- NULL for `--once` one-shots
  source_url TEXT NOT NULL,             -- feed URL, or article URL for one-shots (recorded for analytics, not dedup)
  dedup_key TEXT NOT NULL UNIQUE,       -- sha256(normalize_url(article_url))
  canonical_url TEXT NOT NULL,
  title TEXT,
  pub_date TEXT,                        -- original publication date (ISO 8601)
  ingested_at TEXT NOT NULL,            -- ISO 8601
  output_path TEXT                      -- where the file was written
);

CREATE INDEX idx_items_subscription ON items(subscription_name);
```

Dedup is **global** by `dedup_key`. An article appearing in two subscribed feeds is ingested once - whichever feed delivers it first wins, and `subscription_name` records which one.

`dedup_key = sha256(normalize_url(article_url))`, where `normalize_url` lowercases scheme + host, strips tracking query params (`utm_*`, `fbclid`, `mc_*`, `ref`), drops the URL fragment, and trims trailing slashes from the path. Deterministic, no extra HTTP call needed. Trade-off: a feed re-publishing the same article under a different canonical URL will be ingested twice; this is acceptable and probably what you want.

### EPUB metadata (Dublin Core via OPF manifest)
```xml
<dc:title>The End of the Beginning</dc:title>
<dc:creator>Ben Thompson</dc:creator>
<dc:publisher>Stratechery</dc:publisher>
<dc:date>2026-04-28</dc:date>                                  <!-- original publication -->
<dc:identifier>https://stratechery.com/2026/...</dc:identifier>
<dc:source>https://stratechery.com/feed</dc:source>            <!-- 'direct' for --once -->
<dc:subject>pulpline:stratechery</dc:subject>                  <!-- subscription_name as tag -->
<meta name="calibre:timestamp">2026-05-02T14:00:00Z</meta>     <!-- ingestion time -->
```

### Filename convention
- Pattern: `{Title}.epub`, sanitized for cross-platform filesystems (no `/`, `:`, etc.)
- All structured metadata lives in the OPF, not the filename
- Collisions: if `{Title}.epub` already exists with a different `dedup_key`, append ` (2)`, ` (3)`, ...

### Concurrency & politeness
- `sync` is sequential: one feed at a time, one item at a time. Polite to servers; matches expected v0.1 scale (<= ~30 feeds).
- Shared httpx client sets a User-Agent of `pulpline/{version} (+https://github.com/...)`.

### Error handling
- Feed-level failure (network error, malformed XML, HTTP 4xx/5xx): skip + log + continue. Persist to `subscription_state.last_error`.
- Item-level failure (one article in a feed fails to fetch or convert): skip + log + continue with the next item.
- No retries in v0.1. Wait for next `pulp sync`.

## 3. Core Features (MVP - v0.1.0)

- [ ] **CLI: `pulp add <url>`** subscribes by default. Auto-detects feed vs single page (Content-Type / well-known feed shape). `--once` forces one-shot fetch with no config persistence.
- [ ] **CLI: `pulp sync`** sequentially re-runs all subscriptions, fetches new items only, writes EPUBs.
- [ ] **CLI: `pulp list`** prints configured subscriptions with last-sync timestamps.
- [ ] **CLI: `pulp remove <name>`** removes a subscription from config (does not delete already-written EPUBs).
- [ ] **Source plugin: `rss`** - feedparser-based, handles RSS + Atom; uses trafilatura on the linked page when feed only provides summaries.
- [ ] **Source plugin: `url`** - single-page fetch + trafilatura extraction.
- [ ] **Renderer: EPUB** - ebooklib, full Dublin Core OPF metadata.
- [ ] **Sink: filesystem** - write to `output_dir` (global or per-subscription override).
- [ ] **Config auto-creation** on first run.
- [ ] **State auto-creation** on first run.
- [ ] **Error handling**: skip + log + continue on every failure boundary.
- [ ] **Quality gates**: mypy strict passes; pytest >=70% coverage on core modules; README quickstart works on a fresh machine; `pipx install pulpline` works.

## 4. Implementation Plan (Milestones)

- [ ] **Phase 0: Skeleton**
  - `uv init`, `pyproject.toml` with hatchling, two console_scripts (`pulpline`, `pulp`)
  - `mypy` strict config, `pytest` config, MIT `LICENSE`, README stub
  - `pulp --help` works

- [ ] **Phase 1: Core pipeline (URL -> EPUB, no persistence)**
  - `sources.base.Source` ABC, `renderers.base.Renderer` ABC, `sinks.filesystem`
  - `sources.url` plugin (trafilatura)
  - `renderers.epub` (ebooklib) with full OPF metadata
  - `pulp add <article-url> --once` works end-to-end on a real article

- [ ] **Phase 2: Persistence + RSS**
  - `config.py` (TOML load/save with auto-creation)
  - `state.py` (SQLite schema + dedup queries)
  - `sources.rss` plugin
  - `pulp add <feed-url>` (default subscribe) writes to TOML
  - `pulp sync` iterates subscriptions, dedups via SQLite, writes new items only
  - `pulp list`, `pulp remove`

- [ ] **Phase 3: Polish + ship**
  - Auto-detection in `add` (feed vs page)
  - Error-handling pass on every boundary
  - README quickstart verified on a fresh macOS + fresh Linux box
  - `pipx install pulpline` smoke test
  - Personal dogfooding: 1 week on Palma 2, no manual flow -> tag v0.1.0 -> publish to PyPI

## 5. Post-MVP Roadmap

Captured here to prevent v0.1 scope creep. None are commitments. Grouped by architectural shape, because not all of these are the same kind of thing.

### Recurring sources (extend the source plugin pattern)
- **MangaDex** - list new chapters per followed series; render as CBZ
- **arXiv** - subscribe to author / category / saved-search; render as reflowed PDF
- **X (Twitter) bookmarks** - feasibility-contingent. X public API was largely closed in 2023; viable paths are (a) paid API tier, (b) official bookmark CSV export if available, (c) session-cookie based scrape. Pick a path before designing.

### Bulk subscription importers (new architectural shape)
A pulpline subscription today is one entry in `[[subscriptions]]`. Importers *populate* that list from external services so the user never hand-edits TOML for routine feed adds.

- **`pulp import substack`** - discover the user's Substack subscriptions and emit `[[subscriptions]]` blocks pointing at each newsletter's RSS feed (`<sub>.substack.com/feed`). Discovery paths: session-cookie based against `substack.com/account`, or OPML export if/when Substack ships one. Discovered list is presented as an **interactive checklist** (space toggles, enter confirms) so the user can opt out of importing all - selected entries are appended to `config.toml`, existing manual entries are preserved, no overwrites.
- **`pulp import opml <file>`** - generic OPML import. Covers Feedly/Inoreader/etc. exports as a side-benefit and is the obvious fallback if Substack ever ships an OPML export.

### One-shot fetchers (new architectural shape)
Not subscriptions; ad-hoc lookups. User invokes, picks a result, downloads once.

- **`pulp search anna "<title> [author]"`** - query Anna's Archive, list candidates with size / format / quality, fetch the chosen one to `output_dir`. EPUB preferred, PDF acceptable. User is responsible for legal compliance in their jurisdiction. Item recorded in the items ledger so re-fetches are deduped.

### Renderers
- **CBZ** for manga (driven by MangaDex)
- **Reflowed PDF** for papers with figures (driven by arXiv)
- **Renderer abstraction:** promote when the second renderer lands. Premature now.

### UX
- **TUI** (`pulp tui`) using `textual`. Two distinct views:
  - **Sync view** - live per-feed progress while `sync` runs (or against a running `sync` if we add IPC); items fetched, items skipped (already seen), failures.
  - **Library view** - per-source stats over time (items ingested last 7 / 30 / 90 days, top sources by volume, error rate). All derived from the `items` and `subscription_state` tables.
- **Reading stats — two tiers:**
  - **Ingestion stats (easy):** how much pulpline wrote out, by source, over time. Lives in the items ledger; available for free.
  - **Device-side reading stats (research item):** what was actually opened / finished on the Palma 2. Requires Boox library introspection (their SQLite, sync-folder metadata, or KOReader history if installed). Significant scope. Mark as stretch.

### Plumbing
- **GitHub Actions CI:** lint + type + test + build on PR; publish to PyPI on tag
- **Source plugin entry points:** promote in-tree registry to a `pulpline.sources` entry-point group when the first third-party plugin ships
- **Retries / parallelism:** add when real-world feeds justify it
- **File logging + rotation** (`~/.local/state/pulpline/log/`)

## 6. Current State

- **Last Updated:** 2026-05-02
- **Status:** Phases 0 + 1 + 2 + 3 complete. `pulp add` auto-classifies feed vs article (overridable with `--once`/`--feed`); `sync`/`list`/`remove` functional. Wheel installs cleanly via `uv tool install` / `pipx install` and runs end-to-end. `make check` green: 80 tests, 88.4% coverage, mypy strict + ruff clean. Pending user actions: 1-week personal dogfooding on Palma 2, version bump 0.1.0.dev0 -> 0.1.0, PyPI publish.
- **Working directory:** `/Users/egorkonovalov/pulpline`

### Locked decisions (recorded so they don't get re-litigated)
- CLI framework: `typer`
- HTTP client: `httpx`
- RSS parsing: `feedparser`
- Project layout: `src/`
- Dedup: global, by URL-normalized hash
- Auto-detection of feed vs page in `pulp add`: implementation detail, not spec'd
- Phase 1 deliverable: `pulp add <url> --once` only (no SQLite, no TOML); persistence in Phase 2
- Substack import (post-MVP): append + interactive checklist for selection

### Known issues / Phase 3 inheritance
- **Trafilatura's date extraction picks the first date it finds on a page** (was: Phase 2 inheritance). RSS source now uses `entry.published_parsed` from feedparser instead, so feed items get correct dates. URLSource (one-shot path) still inherits trafilatura's guess - acceptable because one-shot dates are advisory.
- **`<?xml version="1.0"?>` declaration in EPUB chapter content silently produces empty output** in `ebooklib`. Renderer omits the declaration; DOCTYPE is fine. Documented inline in `renderers/epub.py:_wrap_html`.
- ~~**`pulp add <url>` (subscribe path) doesn't validate the URL is actually a feed**~~ - **resolved in Phase 3.** `pulp add` now auto-classifies via feedparser; URLs without parseable feed entries fall through to one-shot. `--once` and `--feed` flags override detection. Detection is "fetch-and-parse"; we accept the double-fetch cost (detection + actual ingestion) as simpler than threading prefetched bytes through.
- **`pulp remove <name>` leaves orphaned items in the SQLite ledger** with `subscription_name` pointing to a deleted subscription. Dedup still works via `dedup_key`, so re-subscribing under the same name will skip previously-ingested items in the first sync. That's probably desired ("don't re-fetch what I've already read") but is a non-obvious interaction - a future `--purge` flag could explicitly clean up if needed.
