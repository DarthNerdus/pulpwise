# Pulp Wise Specification

## 1. Context & Scope

**Vision:** A local content pipeline for Readwise Reader and Shiori. It pulls articles, papers, and newsletters from heterogeneous sources (RSS, Substack, arXiv, IMAP mailboxes, bare URLs), filters and dedups them locally, then routes each subscription to its selected destination. Readwise can receive URLs or authenticated HTML captures; Shiori receives source URLs only. Subscriptions, filtering rules, and ingestion history stay local and portable.

**Lineage:** forked from pulpline, which renders content to e-reader files (EPUB/PDF/CBZ) in a synced folder. Pulp Wise removes the entire file pipeline and replaces the sink. The two tools coexist deliberately: package `pulpwise`, CLI `pulpwise`/`pw`, config `~/.config/pulpwise/`, state `~/.local/share/pulpwise/`, env vars `PULPWISE_*` - nothing collides with a mainline pulpline install.

**Primary user:** myself. Technical user (CLI, TOML, Python). Reads in Readwise Reader.

**Secondary user:** anyone already paying for Readwise who wants curated, filtered, cookie-authenticated content flowing into it. Same technical baseline (assumes user can `pipx install`, edit a config file, and run things on a schedule).

**Non-goals (explicit, permanent):**
- Local file output of any kind - no EPUB/PDF/CBZ rendering, no output directory, no Syncthing. That's mainline pulpline's job; use it if you want files.
- Manga, books, and binaries (MangaDex, Anna's Archive, emailed ebook attachments) - also mainline pulpline's job. Reader is for articles and papers.
- Team / multi-user setups
- Hosted SaaS or web service
- Mobile-first or GUI configuration
- Windows-first support (macOS + Linux are primary)
- Built-in scheduling daemon: use cron / launchd / systemd timers

**Smell test for "done":** I read RSS, paid Substack, and email newsletters in Reader for a week without once manually saving a URL, forwarding an email to Reader's address, or seeing a duplicate/resurrected document. If I reach for the manual flow even once, it's not done.

## 2. Technical Architecture

### Stack
- **Language:** Python 3.14+
- **Package / env manager:** uv (everything: `uv run`, `uv pip`, `uv venv`, `uv sync`)
- **Build backend:** hatchling
- **Distribution:** PyPI wheel, installable via `pipx install pulpwise`; console scripts `pulpwise` and `pw`
- **CLI framework:** typer
- **HTTP:** httpx (sync; async deferrable)
- **RSS / Atom:** feedparser
- **HTML handling:** lxml (email cleaning, extraction helpers). No trafilatura, no ebooklib - public pages are extracted by Reader server-side, and nothing is rendered locally.
- **TUI:** textual
- **IMAP:** imap-tools (quarantined behind `util/imap.py`)
- **Config:** TOML via stdlib `tomllib` (read), `tomli-w` (write)
- **State:** SQLite via stdlib `sqlite3`
- **Type checking:** mypy strict
- **Tests:** pytest, >=70% coverage gate
- **License:** MIT

### Project layout (src/ layout)
```
pulpwise/
  pyproject.toml           # hatchling, console_scripts: pulpwise, pw
  README.md
  SPEC.md
  LICENSE
  src/pulpwise/
    cli.py                 # typer app: add, sync, list, remove, backfill, import, tui
    config.py              # TOML load/save, schema, defaults, auto-creation
    state.py               # SQLite schema, dedup queries, tombstones
    pipeline.py            # add/sync/backfill orchestration; the only place sources + sink meet
    models.py              # ItemRef, RawArticle, ReaderSubmission, error types
    sources/               # in-tree registry: rss, url, substack (+saved), arxiv, email
    sinks/
      readwise.py          # URL/content saves: POST /api/v3/save/
      shiori.py            # URL-only saves: POST /api/links
    importers/             # opml, substack bulk import
    tui/                   # textual app: Library / Subscriptions / Sync / Stats views
    util/                  # http (retry transport + breaker), dedup, email_clean, imap, logging
  tests/
```

### Filesystem layout (XDG-style, auto-created on first invocation)
- Config: `~/.config/pulpwise/config.toml` (`PULPWISE_CONFIG_PATH` override)
- State DB: `~/.local/share/pulpwise/state.db` (`PULPWISE_STATE_PATH` override)
- Logs: `~/.local/state/pulpwise/log/pulpwise.log`, rotated (`PULPWISE_LOG_DIR` override)

### Pipeline shape

```
Source.discover() -> ItemRef -> [dedup check] -> ReaderSubmission -> destination sink -> ledger record -> Source.ack()
```

The ordering is the crash-safety contract: the remote push strictly precedes the ledger write (a lost ledger write means one harmless re-push next run - Reader answers 200 for a URL it already has), and the ledger write strictly precedes the ack (so a source never destroys its own re-discovery path - moving an email, say - before the item is durably owned). Acks also fire on ledger-skips so a crash between record and ack heals on the next sync.

### Source contract

- `discover(target_url) -> Iterable[ItemRef]` - cheap listing, no body downloads. For one-shot URLs, yields exactly one ref.
- `fetch_needed: ClassVar[bool]` - **False** for sources whose discovered URLs are publicly fetchable as-is (rss, url, arxiv): the pipeline skips `fetch` entirely and calls `submission_for_ref(ref)`, producing a bare-URL save that Reader extracts server-side. **True** for sources that must fetch each item first (substack, email) - to resolve gated URLs, learn paywall status, or capture content Reader can't reach: `fetch(ref) -> RawArticle`, then `submission_for_article(article)`.
- `submission_for_article` routes on `RawArticle.content_gated`: ungated articles become bare-URL saves of their canonical URL (Reader re-extracts server-side - fresher and fixable on their end); gated articles - or any article whose canonical URL isn't http(s), like email's `mid:` scheme - become HTML content submissions with explicit title/author/date, under a deterministic synthetic URL when no real one exists.
- `rate_limit_scope: ClassVar[str | None]` - names a shared breaker bucket when the source's requests all land on one provider's limiter regardless of hostname (Substack). None = per-host buckets.
- `ack(ref)` - duck-typed, optional; called after an item is recorded *or* skipped. Email uses it for mark-read/move. Best-effort: failures are logged and swallowed.
- `discover_backwards(target_url)` - duck-typed, optional; enables `backfill`. Substack (publications by offset, saves by save time) and email (whole folder newest-first) implement it.
- `matches_url` / `is_subscribable` - one-shot URL routing and add-time auto-detection (arXiv claims `arxiv.org`; URLSource is the fallback; RSS is detected by fetch-and-parse).

### Readwise sink

The default sink. `POST https://readwise.io/api/v3/save/` with `Authorization: Token XXX` (token from https://readwise.io/access_token; resolution order: `PULPWISE_READWISE_TOKEN` env > `[auth.readwise].token` > `[auth.readwise].token_path`). Verified against readwise.io/reader_api:

- **201** = document created; **200** = a document with that exact URL already existed (Reader bumps it, creates nothing). The status *is* the dedup signal - no pre-flight existence check.
- URL saves send just the URL (plus optional title/date/category); content saves send `html` + `should_clean_html` + explicit metadata. `category = "pdf"` is sent for arXiv so Reader's type guesser doesn't have to work from a suffix-less URL.
- Save rate limit is 50/min per token. The sink self-paces at 45/min so a long backfill never trips the limiter in the first place. On 429 it honors `Retry-After` once (bounded at 120s), retries once, then raises `RateLimited` and arms a fail-fast breaker.
- Reader never re-parses saved content: content submissions are write-once. Fixing content = delete + re-save, which loses highlights.
- One sink instance per run (sync/backfill/one-shot) so the pacing clock and breaker span all subscriptions.

### Shiori sink

`options.location = "shiori"` routes a subscription to `POST https://www.shiori.sh/api/links` with `Authorization: Bearer XXX`. Key resolution is `PULPWISE_SHIORI_TOKEN` env > `[auth.shiori].token` > `[auth.shiori].token_path`.

- The request body is exactly `{ "url": ref.url }`, using the discovered source URL. Pulp Wise does not fetch the article body for a Shiori route; HTML, title, author, summary, and Reader tags are never forwarded, including for authenticated/gated sources.
- Only public HTTP(S) URLs are accepted. Synthetic `pulpwise.invalid` identities, non-web source IDs, credential-bearing URLs, localhost names, and non-global IP literals fail at item level rather than creating an unusable or unsafe Shiori entry.
- A 200 JSON response with `success: true` and `linkId` is success; `duplicate: true` means Shiori already had the URL and bumped it.
- Link creation is limited to 30/minute. The sink paces at 25/minute and converts 429 + `Retry-After` into the pipeline's normal rate-limited deferral, blocking later Shiori pushes during that cooldown.
- Shiori does not document a stable per-link application URL, so the ledger opens the public source URL for Shiori-routed items.
- Only providers used by enabled subscriptions are initialized for a run. A Shiori-only config does not require a Readwise token; mixed runs reuse one sink instance per provider.

### Config schema (`~/.config/pulpwise/config.toml`)
```toml
[auth.readwise]
token = "XXX"                                # or:
token_path = "~/.config/pulpwise/readwise_token"

[auth.shiori]
token_path = "~/.config/pulpwise/shiori_token"

[auth.substack]
cookies_path = "~/.config/pulpwise/substack-cookies.json"
extra_cookies_paths = ["~/.config/pulpwise/acx-cookies.json"]

[auth.email]
username = "you@gmail.com"
password_path = "~/.config/pulpwise/email_password"

[[subscriptions]]
name = "stratechery"                    # unique key; dedup namespace
source = "rss"
url = "https://stratechery.com/feed"

[subscriptions.options]                 # values are str | int
location = "new"                        # feed | new | later | archive | shiori; default feed
tags = "tech, essays"                   # Reader-only tags; ignored by Shiori
# categories / exclude_categories       # rss-owned
# since_days / mark_read / ...          # email-owned; plugins own their shapes
```

There is no `[paths]` table and no `output_dir` key - Pulp Wise has no filesystem output. Legacy pulpline keys are accepted and ignored on load (a copied-over config must not crash) and dropped on the next save. `[auth]` stays generic: `{source: {key: str | list[str]}}`; each consumer owns the shape of its own subtable.

First-run behavior: if `config.toml` does not exist, it's created with a commented example. No `init` subcommand.

### State schema (SQLite)
TOML stays the source of truth for subscriptions. SQLite holds the dedup ledger and per-subscription run cache - and the ledger is load-bearing in a way it wasn't when output was files: Reader dedupes by exact URL only *while the document exists*, so the local rows are the only guard against re-creating documents the user deleted.

```sql
CREATE TABLE subscription_state (
  name TEXT PRIMARY KEY,
  last_synced_at TEXT,
  last_status TEXT,                     -- 'ok' | 'error'
  last_error TEXT,
  total_items INTEGER                   -- source-reported total, when known
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_name TEXT,               -- NULL for one-shots
  source_url TEXT NOT NULL,             -- feed URL, or article URL for one-shots
  dedup_key TEXT NOT NULL UNIQUE,       -- sha256(normalize_url(article_url))
  canonical_url TEXT NOT NULL,
  title TEXT,
  pub_date TEXT,
  ingested_at TEXT NOT NULL,
  readwise_id TEXT,                     -- Reader document id
  readwise_url TEXT,                    -- Reader app URL (what the TUI opens)
  submission_kind TEXT,                 -- 'url' | 'html'; Shiori always records 'url'
  destination TEXT NOT NULL DEFAULT 'readwise',
  deleted_at TEXT                       -- tombstone; liveness = deleted_at IS NULL
);

CREATE INDEX idx_items_subscription ON items(subscription_name);
```

- **Liveness** is `deleted_at IS NULL`. Deleting an item tombstones the row only - the Reader document is left alone (push-only contract); sync's skip check (`was_ingested`) matches *any* row, live or tombstoned, so deleted articles are never re-pushed. `pulpwise add <url>` on a tombstoned article re-pushes deliberately via an upsert that clears `deleted_at`.
- **Legacy pulpline databases migrate in place**: `ALTER TABLE ... ADD COLUMN` statements are tried on connect with `OperationalError` swallowed (SQLite has no `IF NOT EXISTS` for columns), and `deleted_at` is backfilled for rows that were soft-deleted under the old scheme (liveness there was `output_path IS NOT NULL`). Old file-era items keep `submission_kind` NULL (surfaced as `file` in stats) and are never re-pushed - any existing row counts as ingested.
- Dedup is **global** by `dedup_key = sha256(normalize_url(article_url))`, where `normalize_url` lowercases scheme + host, strips tracking query params (`utm_*`, `fbclid`, `mc_*`, `ref`), drops the fragment, and trims trailing slashes. An article appearing in two subscribed feeds is pushed once; whichever feed delivers it first wins.

### Concurrency & politeness
- `sync` is sequential: one subscription at a time, one item at a time. Polite to servers, and the Readwise save budget (50/min) makes parallel pushes pointless anyway.
- Shared httpx client sets `User-Agent: pulpwise/{version}`.

### Error handling
- Feed-level failure (network error, malformed XML, HTTP 4xx/5xx): skip + log + continue. Persist to `subscription_state.last_error`.
- Item-level failure: skip + log + continue with the next item.
- Missing/rejected Readwise token: fail once, loudly - every subscription would fail identically. A missing token raises before any work at all; a rejected token (HTTP 401) raises on its first use and aborts the run instead of logging one auth error per item.
- Paywalled items (Substack entitlement failures) are a separate visible bucket, not errors: the fix is refreshing cookies, not debugging the pipeline.
- Rate limiting on **source** traffic (GET): retried in the shared `RetryTransport` with exponential backoff, honoring `Retry-After`. Breaker state is keyed per host - or per provider scope when a source declares one (all Substack traffic shares one `substack` bucket). On the first exhausted 429 the transport waits out one bounded cooldown (<= 2 min) and resumes, pacing that bucket's requests ~1/s. If the bucket trips again with no success in between, the current subscription stops early (remaining items defer to the next sync) and further requests fail fast.
- Rate limiting on the **Readwise save** (POST): handled inside the sink, not the transport - `RetryTransport` only retries GET/HEAD. Same shape: one waited-out `Retry-After`, then `RateLimited` + a sink-level fail-fast breaker, so a sync run with 30 subscriptions doesn't stack thirty Retry-After sleeps.

## 3. Command surface (shipped)

- [x] **`pulpwise add <url>...`** - auto-classifies each URL: real feeds subscribe, single articles one-shot. `--once` / `--feed` override for the whole call, `--name` names the subscription. Batch-friendly: errors on one URL don't abort the rest. One-shots print the Reader document URL; re-adding an already-pushed URL is an idempotent no-op that prints the existing one.
- [x] **`pulpwise sync`** - sequentially runs all subscriptions, pushes not-yet-seen items to Reader.
- [x] **`pulpwise list`** - configured subscriptions with last-sync status.
- [x] **`pulpwise remove <name>`** - removes a subscription from config (documents already in Reader stay).
- [x] **`pulpwise backfill <name>`** - walks a subscription's archive backwards (Substack publications/saves, email folders), skipping already-pushed items, capped per run against the save budget; resumable via the ledger.
- [x] **`pulpwise import opml <file>`** / **`pulpwise import substack [--auto]`** - bulk subscription importers, interactive checklist selection; `--auto` reconciles the Substack follow list non-interactively from cron.
- [x] **`pulpwise tui`** - Library / Subscriptions / Sync / Stats. Subscriptions offers Feed / Inbox / Later / Shiori; edits are persisted to `options.location` for later sync/backfill jobs.

Removed relative to pulpline: `mangadex`, `search` (Anna's Archive), `migrate` (`--rebuild` re-rendered files; there are no files, and Reader never re-parses content submissions anyway). No `--output-dir` flag anywhere.

## 4. Roadmap

None are commitments.

- **Reconcile against Reader** - detect documents deleted remotely via the list/export API and tombstone them locally (today a Reader-side delete is only guarded once the item is in the ledger; reconcile would close the loop for items deleted before their subscription window ages out).
- **Plaintext→HTML fallback for text-only emails** - small addition if real senders turn out to need it.
- **Per-subscription email auth overrides** - a second IMAP *account* (not just a second folder) needs auth beyond the single `[auth.email]` block.
- **X (Twitter) bookmarks** - feasibility-contingent, inherited from pulpline: paid API tier, official export, or session-cookie scrape. Pick a path before designing.
- **Source plugin entry points** - promote the in-tree registry to a `pulpwise.sources` entry-point group when the first third-party plugin ships.

## 5. Current State

- **Last Updated:** 2026-07-17
- **Status:** Readwise and Shiori destinations ship. Readwise accepts URL or HTML submissions; Shiori receives source URLs only. Package renamed `pulpwise` (fork of pulpline v0.1.0); all file output, rendering, MangaDex, Anna's Archive, and email-attachment code removed. Sources shipped: rss (with category filtering), url, substack + substack-saved, arxiv, email. OPML + Substack importers, backfill, TUI (Library/Subscriptions/Sync/Stats, including destination management), durable file logging. GitHub Actions CI configured. Not yet published to PyPI.
- **Working directory:** `/Users/jread/Developer/pulpwise`

### Locked decisions (recorded so they don't get re-litigated)

Inherited from pulpline, still binding:

- CLI framework: `typer`. HTTP client: `httpx`. RSS parsing: `feedparser`. Project layout: `src/`.
- Dedup: global, by URL-normalized hash. Auto-detection of feed vs page in `add`: fetch-and-parse via feedparser; the double-fetch cost is accepted as simpler than threading prefetched bytes through.
- Substack import: append + interactive checklist for selection; existing entries preserved.
- Substack talks to private, unofficial endpoints (profile, post API, saved list). Churn there is an accepted maintenance cost of the feature - when it breaks, we fix it; we don't pretend it's a stable contract.
- Email dedup keys on `mid:<Message-ID>`, never UIDVALIDITY+UID (Gmail UIDs are per-folder; UIDVALIDITY resets would mass-re-ingest). IMAP `\Seen` is never load-bearing: discovery lists a SINCE window (`since_days`, default 60), and `mark_read` ("seen"/"move"/"none") is cosmetic, applied via the pipeline's post-record `ack()` hook. Acks also fire on ledger-skips so a crash between record and mark heals on the next sync.
- Email HTML uses a dedicated cleaner (`util/email_clean.py`), not a web-article extractor - email markup is table-soup that article extraction guts.
- IMAP access via `imap-tools`, quarantined behind `util/imap.py` (the only module importing it; swap boundary if XOAUTH2 becomes necessary). App-password auth only. Three imap-tools behaviors are deliberately routed around: TLS contexts are passed explicitly (`ssl.create_default_context()` - imaplib's default is an *unverified* context), mark-seen goes through raw `UID STORE` (imap-tools' `flag()` EXPUNGEs the whole folder afterwards), and move requires the server-side MOVE capability (the client-side COPY+delete fallback also EXPUNGEs).
- RSS category filtering is per-subscription options (`categories` allow-list / `exclude_categories` drop-list, comma-separated strings because option values are `str | int`), matched case-insensitively against feedparser's normalized `entry.tags[].term` and applied in `discover()` - filtered entries never reach the ledger or Reader, so loosening the filter later pushes whatever is still in the feed. An allow-list skips untagged entries; exclusion wins over inclusion.
- 429 breaker design in `util/http.py`: per-host buckets by default, provider `scope` override for limiters that span hostnames (Substack); one bounded wait-through grace period per bucket, restored by a subsequent success; fail-fast during cooldown; pacing (~1/s) on any bucket that has tripped.

New with the Readwise refactor:

- **The 201/200 status pair is the dedup signal.** No pre-flight "does Reader have this URL?" call - push and read the answer. Reader's URL match is byte-exact, so submissions carry normalized URLs.
- **The sink self-paces at 45 saves/min** (limit is 50) so long runs never trip the limiter at all, rather than tripping it and recovering. Cheaper than the retry dance and kinder to the API.
- **POST 429 handling lives in the sink**, not `RetryTransport` - the transport only retries GET/HEAD (retrying a non-idempotent POST blind is how you double-save). Same recovery shape as the transport: honor one bounded `Retry-After`, retry once, then raise `RateLimited`.
- **The sink arms a fail-fast breaker** once a 429 persists: later pushes through the same sink raise immediately (no network, no sleep) until the cooldown passes. Without it, a sync run with N subscriptions would stack N Retry-After sleeps for a limiter that isn't going to relent mid-run. One sink instance per run so the breaker (and the pacing clock) spans subscriptions.
- **URL-vs-HTML routing is a property of the article, expressed as `RawArticle.content_gated`**: True when a third party (Reader's server-side fetcher, which carries no cookies) could not retrieve the content from its canonical URL. Gated → HTML content submission; ungated → bare URL save. Non-http(s) canonical URLs (email's `mid:` scheme) force the gated path. Sources set the flag; the routing lives once, in `Source.submission_for_article`.
- **Synthetic URLs for URL-less content**: `https://pulpwise.invalid/<sha256(seed)[:32]>`, seeded from the item's own stable identity (e.g. the email's `mid:` dedup URL). Reader requires a URL on every save and uses it as the server-side dedup key, so the value must be deterministic across runs - a crash-retry of the same item has to hit the 200 duplicate path, not create a second document. `.invalid` is the RFC 2606 reserved TLD: it can never resolve, which is the point.
- **Saves default to Reader's Feed section** (`DEFAULT_LOCATION = "feed"` in the pipeline). Pulp Wise acts as a feed reader in front of Reader, so pushed items behave like native RSS - Feed's Seen/Unseen flow - instead of flooding the inbox. Per-subscription `options.location` and `add --location` override; there is no "account default" pass-through anymore (that ambiguity is what the default replaces).
- **The Subscriptions TUI exposes Feed, Inbox, Later, and Shiori as destination choices.** Inbox is the user-facing label for canonical Readwise value `new`; unset/feed displays as Feed, and manually authored `inbox` still displays as Inbox. Legacy `archive` is displayed but never silently rewritten. A saved change applies only to later sync/backfill jobs; running jobs keep their startup snapshot, and existing remote items are never moved.
- **Pulp Wise is push-only: it never deletes (or otherwise mutates) documents in Readwise.** Removing a document is the user's call, made in Reader. Deleting in the TUI is a local tombstone only (row kept, `deleted_at` set). The tombstones are still the only guard against resurrecting user-deleted Reader documents - Reader's dedup lives only as long as the document does, so a re-submitted URL would be happily recreated; sync's skip check matches tombstoned rows to prevent exactly that. `add` clears a tombstone deliberately - that's the "actually I want it back" path.
- **Substack follow-list reconciliation never runs implicitly.** `pulpwise import substack` (and `--auto` for cron) is the explicit path; the TUI's convenience pass before sync is gated behind `[auth.substack].auto_reconcile = true` and defaults OFF. Rationale: reconcile adds every followed publication missing from config - on a fresh config that's the user's entire follow list, a mass side effect that must not fall out of pressing `s`. (TOML booleans in `[auth.*]` are normalized to "true"/"false" strings by the loader so the flag can be written naturally.)
- **Email messages with ebook attachments are skipped entirely** (mainline pulpline ingests those); newsletter title = Subject, author = sender.
- **arXiv submits `/pdf/<id>` URLs with `category="pdf"`** - Reader ingests PDFs by URL, and the explicit category spares its type guesser since arXiv PDF URLs carry no `.pdf` suffix. The abs page is just the abstract; the PDF is the paper.

### Known issues

- **`data:`-URI images in emailed newsletters are unverified in Reader.** The email cleaner can inline images as `data:` URIs; whether Reader's HTML pipeline preserves them in content submissions hasn't been confirmed end-to-end.
- **No documented payload size limit on `/save/`.** A very large newsletter body could plausibly 413; that's currently handled as a generic save error, not specially.
- **Substack private-API churn risk** (see locked decisions) - not a bug, but the most likely thing to break first.
- **Text-only emails are rejected** (item-level error) - there's no HTML to push. Plaintext→HTML fallback is on the roadmap.
- **`pulpwise remove <name>` leaves orphaned items in the ledger** with `subscription_name` pointing at a deleted subscription. Dedup still works via `dedup_key`, so re-subscribing under the same name skips previously-pushed items on the first sync. Probably desired, but non-obvious.
- **Crash between push and ledger-record re-pushes one item next run.** Harmless by design: Reader answers 200 for the URL it already has (and synthetic URLs are deterministic precisely so this holds for content submissions too).
