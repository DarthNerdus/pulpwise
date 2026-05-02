"""Bulk-import subscriptions from external services.

Importers are a different architectural shape from Sources: they are one-shot,
interactive, and write to `config.toml`. Sources are recurring, programmatic,
and read from the loaded Config. Each importer is self-contained in its own
module - the core layer doesn't know which importers exist.
"""
