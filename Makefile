PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON = $(VENV)/bin/python
DB ?= var/incidents.db
PAYLOADS ?= examples/alertmanager
POLICY ?= policy/policy.yaml

.PHONY: help setup test lint format demo serve replay postmortem metrics audit clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## create .venv and install runtime + dev requirements
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install -U pip
	$(VENV_PYTHON) -m pip install -r requirements-dev.txt

test: ## run the suite with coverage
	@test -x $(VENV_PYTHON) || { echo "no $(VENV_PYTHON): run 'make setup' first"; exit 1; }
	$(VENV_PYTHON) -m pytest -q --cov=incidentd --cov-report=term-missing

lint: ## ruff check + format check
	@test -x $(VENV_PYTHON) || { echo "no $(VENV_PYTHON): run 'make setup' first"; exit 1; }
	$(VENV_PYTHON) -m ruff check .
	$(VENV_PYTHON) -m ruff format --check .

format: ## rewrite files with ruff format
	$(VENV_PYTHON) -m ruff format .

demo: ## boot the API, deliver the example payloads, print the postmortem
	PYTHON=$(VENV_PYTHON) bash scripts/demo.sh

serve: ## run the API on 127.0.0.1:8080
	$(VENV_PYTHON) -m incidentd serve --db $(DB) --policy $(POLICY)

replay: ## apply the stored payloads offline
	$(VENV_PYTHON) -m incidentd replay --dir $(PAYLOADS) --db $(DB) --policy $(POLICY)

postmortem: ## print a postmortem draft, e.g. make postmortem ID=INC-2026-0001
	$(VENV_PYTHON) -m incidentd postmortem --id $(ID) --db $(DB)

metrics: ## print the Prometheus exposition for the current database
	$(VENV_PYTHON) -m incidentd metrics --db $(DB)

audit: ## weekly extended checks: payload schema + 1000-alert idempotency run
	$(VENV_PYTHON) scripts/validate_payloads.py --dir $(PAYLOADS)
	$(VENV_PYTHON) scripts/idempotency_audit.py --alerts 1000 --out reports/weekly-audit.md

clean: ## drop caches and generated state
	rm -rf $(VENV) .pytest_cache .ruff_cache htmlcov .coverage var reports/weekly-audit.md
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
