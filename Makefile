# Everything here runs offline. No GCP credentials, no live project.

# `test-all` pipes pytest through tee and needs the pipeline's exit status, not
# tee's. /bin/sh is dash on Debian and has no `pipefail`, so ask for bash.
SHELL := /bin/bash

.PHONY: help setup fmt lint types guard test test-all rules-test check emulator e2e image clean
.PHONY: gcp-setup deploy-rules deploy verify-deploy require-firebase require-gcloud require-project
.PHONY: web-dev web-build deploy-web

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install the Python workspace and the web dependencies
	uv sync
	cd web && npm install

fmt: ## Format Python
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint without changing anything
	uv run ruff check .
	uv run ruff format --check .
	# The shell scripts stand up real infrastructure and neither can be run
	# from CI, so a linter is the only automated check they will ever get.
	uvx --from shellcheck-py shellcheck scripts/*.sh

types: ## Type-check
	uv run basedpyright

guard: ## Fail if anything outside the clock module reads real time
	uv run python scripts/check_no_wallclock.py

test: ## Unit tests, fast. Skips anything needing an emulator.
	uv run pytest -q

# What CI runs, and what `check` runs. The emulators are booted here rather
# than assumed, and a skip is a failure: the emulator-backed tests skip
# themselves when nothing is listening, which is right for the inner loop above
# and unacceptable in a gate. A green run that quietly skipped every guardrail
# test looks like coverage.
test-all: ## Every test, with the emulators up. Fails if any test skips.
	@set -o pipefail; \
	firebase emulators:exec --only firestore,auth --project demo-cinema \
		"uv run pytest -q" 2>&1 | tee .pytest-all.out; \
	if grep -qE "[0-9]+ skipped" .pytest-all.out; then \
		echo "FAIL: tests skipped — an emulator did not come up."; \
		rm -f .pytest-all.out; exit 1; \
	fi; \
	rm -f .pytest-all.out

# Runs the rules files themselves. The Python suite goes through the admin SDK,
# which bypasses security rules entirely, so this is the only thing in the repo
# that executes them. It boots its own emulator because it loads both rules
# files by path — firebase-tools does not read them from the multi-database
# form in firebase.json, which is why the emulator otherwise runs wide open.
rules-test: ## Execute firestore.rules and firestore.orders.rules
	firebase emulators:exec --only firestore --project demo-cinema \
		"cd web && npm test"

check: lint types guard test-all rules-test ## Everything that must pass before a merge

emulator: ## Start the Firestore + Auth emulators (leave running while you work)
	firebase emulators:start --only firestore,auth --project demo-cinema

e2e: ## The daily ten-minute habit: boot the emulators, run the loop end to end
	firebase emulators:exec --only firestore,auth --project demo-cinema \
		"uv run python scripts/run_e2e.py"

gcp-setup: require-gcloud require-project ## Stand up the Google Cloud project (run once)
	PROJECT_ID=$(PROJECT_ID) ./scripts/gcp_setup.sh

# Rules and indexes need firebase-tools; the Cloud Run half needs gcloud. Both
# are pre-installed in Cloud Shell, which is why docs/deploy-runbook.md assumes
# it. Checked here because `make: firebase: No such file or directory` tells you
# nothing about what to do next.
require-firebase:
	@command -v firebase >/dev/null || { \
	  echo "firebase CLI not found."; \
	  echo "  Cloud Shell has it already — see docs/deploy-runbook.md."; \
	  echo "  Locally:  npm install -g firebase-tools && firebase login"; \
	  exit 2; }

require-gcloud:
	@command -v gcloud >/dev/null || { \
	  echo "gcloud not found."; \
	  echo "  Cloud Shell has it already — see docs/deploy-runbook.md."; \
	  echo "  Locally:  https://cloud.google.com/sdk/docs/install"; \
	  exit 2; }

require-project:
	@[ -n "$(PROJECT_ID)" ] || { \
	  echo "PROJECT_ID is not set."; \
	  echo "  make $(MAKECMDGOALS) PROJECT_ID=your-project-id"; \
	  exit 2; }

deploy-rules: require-firebase require-project ## Push both rules files and the indexes to the real project
	firebase deploy --only firestore --project $(PROJECT_ID)

deploy: require-gcloud require-project ## Deploy both Cloud Run services and the Scheduler job
	PROJECT_ID=$(PROJECT_ID) ./scripts/deploy.sh

verify-deploy: require-gcloud require-project ## Check a deployment — read-only, and the real definition of done
	PROJECT_ID=$(PROJECT_ID) ./scripts/verify_deploy.sh

# The panel reads Firestore from the browser, so it needs an emulator with
# something in it. `make e2e` fills one with a project, four items and twelve
# negotiations — run that first, then this, and the screen has content from the
# first paint. Drop VITE_USE_EMULATOR to point at the real project instead.
web-dev: ## Serve the instrument panel against the local emulators
	cd web && VITE_USE_EMULATOR=1 npm run dev

web-build: ## Build the panel into web/dist, which firebase.json serves
	cd web && npm run build

deploy-web: require-firebase require-project web-build ## Build and publish to Firebase Hosting
	firebase deploy --only hosting --project $(PROJECT_ID)

image: ## Build the Cloud Run image (context is the repo root, deliberately)
	docker build -t agentic-cinema:local .

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ web/dist
