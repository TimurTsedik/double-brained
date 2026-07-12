# Second Brain

Python implementation of Second Brain.

## Local setup

```bash
uv sync --python 3.13
```

## Quality checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## Run locally

```bash
uv run uvicorn second_brain.bootstrap.main:app --host 127.0.0.1 --port 8000
```
