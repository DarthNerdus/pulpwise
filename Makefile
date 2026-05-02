.PHONY: sync test lint format typecheck check build clean

# Strip the user's global pyx.dev UV_INDEX so uv resolves against PyPI.
# Harmless if those vars are unset (e.g. under direnv with the project .envrc).
UV := env -u UV_INDEX -u PYX_API_KEY uv

sync:
	$(UV) sync

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format:
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

typecheck:
	$(UV) run mypy

check: lint typecheck test

build:
	$(UV) build

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
