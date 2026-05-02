# pulpline

Local-first content pipeline for e-readers. Pulls articles, papers, and newsletters from heterogeneous sources, converts them to e-reader-friendly formats, and writes them to a synced folder. No cloud service. No subscription. No manual file shuffling.

## Status

Pre-alpha. See [SPEC.md](SPEC.md) for the design and implementation plan.

## Quickstart (placeholder, not functional yet)

```bash
pipx install pulpline
pulp add https://stratechery.com/feed
pulp sync
```

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
make sync         # install runtime + dev deps
make check        # lint + typecheck + test
make test
make typecheck
make build        # produce wheel + sdist in dist/
```

The `Makefile` strips a known-conflicting `UV_INDEX` / `PYX_API_KEY` env-var pair
before invoking `uv`, so the project resolves cleanly against PyPI even when the
user has a non-PyPI registry set globally. `direnv` users can `direnv allow` and
get the same effect via the bundled `.envrc`.

## License

MIT
