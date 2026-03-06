# Contributing

Thanks for contributing.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .[dev]
```

If you want to run the full CLI locally, install `ffmpeg` first:

```bash
brew install ffmpeg
```

## Development Workflow

1. Create a branch for your change.
2. Keep PRs focused and small.
3. Add or update tests for behavior changes.
4. Run lint and tests before opening a PR.

## Checks

```bash
python3 -m ruff check .
python3 -m pytest
```

## Pull Request Checklist

- The change solves one clear problem.
- CLI behavior is documented in `README.md` when needed.
- Tests cover new parsing or formatting logic.
- Error handling remains explicit and user-facing.
