.PHONY: test lint fix build clean cov

PYTEST    := .venv/bin/python -m pytest
RUFF      := .venv/bin/ruff
COVERAGE  := .venv/bin/python -m coverage

# ─── Default ───────────────────────────────────────────────
all: lint test

# ─── Test ──────────────────────────────────────────────────
test:
	$(PYTEST) tests/unit/ -q

cov:
	$(COVERAGE) run -m pytest tests/unit/ -q
	$(COVERAGE) report --show-missing --skip-covered

ci:
	$(PYTEST) tests/unit/ tests/integration/ -q

# Real-API integration matrix (v1.2.0): env MICROAGENT_TEST_* wins,
# otherwise falls back to ~/.microagent/config.yaml — zero plumbing on
# a machine with a configured endpoint. Skips cleanly when neither.
integration:
	$(PYTEST) tests/integration/ -v --timeout=300

# ─── Lint & Format ─────────────────────────────────────────
lint:
	$(RUFF) check src/ tests/

fix:
	$(RUFF) check --fix src/ tests/
	$(RUFF) format src/ tests/

# ─── Build ─────────────────────────────────────────────────
build:
	uv build

# ─── Clean ─────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name *.egg-info -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ .coverage htmlcov/ 2>/dev/null || true
