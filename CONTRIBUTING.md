# Contributing to auto-healing-agent

Thank you for your interest in contributing to `auto-healing-agent`!

## Development Setup

Requirements:
- Python >= 3.10
- `uv` (recommended) or standard Python `venv` + `pip`

Clone and install dependencies:
```bash
git clone https://github.com/restrok/auto-healing-agent.git
cd auto-healing-agent
uv sync --all-extras --dev
```

## Running Linting and Formatting

We use [Ruff](https://astral.sh/ruff) for fast linting and code formatting:

```bash
uv run ruff check .
uv run ruff format --check .
```

To automatically format:
```bash
uv run ruff format .
```

## Running Tests

Run the full pytest suite with coverage:
```bash
uv run pytest
```

Ensure all tests pass before opening a pull request.

## Pull Request Guidelines

- Write clean, well-tested code.
- Ensure that no secrets, internal IPs, private hostnames, or credentials are added to the codebase.
- Maintain update sparsity and pre-flight validation constraints when modifying remediation logic.
