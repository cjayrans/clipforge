# clipforge operator and developer commands (targets listed in CLAUDE.md).
#
# lint/typecheck/test run on the host with uv. Operational targets run `cf` inside the worker container so the
# SQLite file is only ever opened from inside Docker (host + container locking on a bind mount is not safe).

UV ?= uv
COMPOSE ?= docker compose
SHELL := /bin/bash

# Run `cf $(1)` in the running worker, or in a one-off container when it is not up.
define cf
	@if [ -n "$$($(COMPOSE) ps --status running -q worker 2>/dev/null)" ]; then \
		$(COMPOSE) exec worker cf $(1); \
	else \
		$(COMPOSE) run --rm --no-deps worker cf $(1); \
	fi
endef

.PHONY: help up down logs status build test test-slow lint typecheck fmt costs resume backup weekly-report \
	export-reconcile import-reconcile fixtures

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

# --- Docker lifecycle ---------------------------------------------------------------------------------

up: ## Build and start worker + bot in the background
	$(COMPOSE) up -d --build

down: ## Stop both services
	$(COMPOSE) down

logs: ## Follow service logs
	$(COMPOSE) logs -f --tail=200

status: ## Container health + cf status
	$(COMPOSE) ps
	$(call cf,status)

build: ## Build the image
	$(COMPOSE) build

# --- Development -----------------------------------------------------------------------------------------

test: ## Run the test suite
	$(UV) run pytest -q

test-slow: ## Run only the slow tests (real models / large media)
	$(UV) run pytest -q -m slow

lint: ## Lint and format check
	$(UV) run ruff check . && $(UV) run ruff format --check .

typecheck: ## mypy --strict on the contract and compliance modules
	$(UV) run mypy src/clipforge/schemas.py src/clipforge/compliance.py

fmt: ## Auto-format and fix lint
	$(UV) run ruff format . && $(UV) run ruff check --fix .

fixtures: ## Regenerate tests/fixtures (video via ffmpeg inside the image)
	$(UV) run python scripts/make_fixture_transcripts.py
	docker run --rm --platform linux/amd64 --user "$$(id -u)" -v "$(CURDIR)/tests/fixtures:/out" \
		-v "$(CURDIR)/scripts:/app/scripts:ro" clipforge:latest python /app/scripts/make_fixture_video.py /out/synthetic_90s.mp4

# --- Operations (run inside Docker) ---------------------------------------------------------------------

costs: ## Month-to-date API spend
	$(call cf,costs)

resume: ## Clear the spend-cap pause
	$(call cf,resume)

backup: ## Back up the database now (data/backups/)
	$(call cf,backup)

weekly-report: ## Write the weekly digest to data/reports/
	$(call cf,report weekly)

export-reconcile: ## Export open submissions to data/reconcile.csv
	$(call cf,export-reconcile --out data/reconcile.csv)

import-reconcile: ## Import an edited CSV: make import-reconcile FILE=data/reconcile.csv
	@test -n "$(FILE)" || { echo "usage: make import-reconcile FILE=data/<file>.csv"; exit 2; }
	@case "$(FILE)" in data/*) ;; *) echo "FILE must be inside data/ (the container only sees ./data)"; exit 2;; esac
	$(call cf,import-reconcile $(FILE))
