.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install:  ## Sync the Python environment
	$(UV) sync --all-extras

.PHONY: fmt
fmt:  ## Format
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

.PHONY: lint
lint:  ## Lint + typecheck
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

.PHONY: test
test:  ## Run the offline test suite
	$(UV) run pytest

.PHONY: test-live
test-live:  ## Run tests that hit the real model (needs ANTHROPIC_API_KEY)
	$(UV) run pytest -m ai_live

.PHONY: serve
serve:  ## Run the API with reload
	$(UV) run uvicorn ledger.api.app:app --reload --port 8000

.PHONY: fetch
fetch:  ## Download the taxi dataset
	$(UV) run python -m scripts.fetch_data

.PHONY: types
types:  ## Regenerate the TypeScript SSE types
	$(UV) run python scripts/gen_types.py

.PHONY: clean
clean:  ## Remove caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
