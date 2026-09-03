# Everything here runs offline. No GCP credentials, no live project.

# `test-all` pipes pytest through tee and needs the pipeline's exit status, not
# tee's. /bin/sh is dash on Debian and has no `pipefail`, so ask for bash.
SHELL := /bin/bash

# Settings a person may pass on the make command line. Make does not put
# command-line variables into a recipe's environment unless they are exported,
# so without this `make gmail-smoke CINEMA_TOKEN_BACKEND=secret-manager` is
# accepted and silently ignored — worse than being rejected.
# Conditional, because a bare `export VAR` on an undefined variable exports it
# as the empty string — and pydantic-settings rejects "" for an enum field, so
# every target that builds Settings() dies. `make e2e` caught that.
ifdef CINEMA_TOKEN_BACKEND
export CINEMA_TOKEN_BACKEND
endif
ifdef CINEMA_GCP_PROJECT
export CINEMA_GCP_PROJECT
endif

# Cloud resource names, shared by the deploy targets so they cannot drift.
REGION ?= us-central1
APPROVALS_SERVICE ?= cinema-approvals
API_SERVICE ?= cinema-api

.PHONY: help setup fmt lint types guard test test-all rules-test check emulator e2e image clean
.PHONY: gcp-setup deploy-rules deploy verify-deploy redirect-uri check-research require-firebase require-gcloud require-project
.PHONY: web-dev web-build deploy-web seed gmail-smoke

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

# The direct answer to "is web search actually working", which was otherwise a
# choice between a full deploy and reading price bands for signs of invention.
# Tests the key the DEPLOYED tick holds, not the one in your shell — this
# project ran for days on the literal word `your-key` while every local check
# passed.
check-research: require-gcloud require-project ## Make one real Parallel search and report
	uv run python scripts/check_research.py --from-deployment --project-id $(PROJECT_ID)

# Needed before any producer can connect a mailbox, and needed again whenever
# somebody asks "what was that URL". Read-only, so it costs nothing to re-run.
redirect-uri: require-gcloud require-project ## Print the OAuth redirect URI to register, and where
	PROJECT_ID=$(PROJECT_ID) ./scripts/oauth_redirect_uri.sh

# Keyed on the lockfile, not on any binary.
#
# An earlier version depended on web/node_modules/.bin/vite, which fixed the
# empty-clone case and nothing else: vite being present says nothing about
# whether the *dependencies* are current. Adding Tailwind and the router left
# that target satisfied, npm ci never re-ran, and the next deploy died on
# `Cannot find package '@tailwindcss/vite'`.
#
# npm writes node_modules/.package-lock.json on every install, so it is an
# honest timestamp for what is actually installed. Newer lockfile means
# reinstall; unchanged lockfile is still a no-op.
web/node_modules/.package-lock.json: web/package-lock.json web/package.json
	cd web && npm ci

# The panel reads Firestore from the browser, so it needs an emulator with
# something in it. `make e2e` fills one with a project, four items and twelve
# negotiations — run that first, then this, and the screen has content from the
# first paint. Drop VITE_USE_EMULATOR to point at the real project instead.
web-dev: web/node_modules/.package-lock.json ## Serve the instrument panel against the local emulators
	cd web && VITE_USE_EMULATOR=1 npm run dev

web-build: web/node_modules/.package-lock.json ## Build the panel into web/dist, which firebase.json serves
	cd web && npm run build

# The Approve button posts to the approvals service, and Vite inlines that URL
# at build time — so the panel is built against one specific deployment rather
# than discovering it at runtime. Nobody should have to type it: it is the same
# lookup verify_deploy.sh does. Missing is a hard failure, because publishing a
# panel whose one irreversible action is inert is worse than not publishing.
deploy-web: require-firebase require-gcloud require-project web/node_modules/.package-lock.json ## Build and publish to Firebase Hosting
	@url=$$(gcloud run services describe $(APPROVALS_SERVICE) \
	          --region=$(REGION) --project=$(PROJECT_ID) \
	          --format='value(status.url)' 2>/dev/null); \
	  api=$$(gcloud run services describe $(API_SERVICE) \
	          --region=$(REGION) --project=$(PROJECT_ID) \
	          --format='value(status.url)' 2>/dev/null); \
	  if [ -z "$$url" ] || [ -z "$$api" ]; then \
	    echo "could not find $(APPROVALS_SERVICE) and $(API_SERVICE) in $(PROJECT_ID)."; \
	    echo "  deploy them first:  make deploy PROJECT_ID=$(PROJECT_ID)"; \
	    exit 2; \
	  fi; \
	  echo "  approvals: $$url"; \
	  echo "  api:       $$api"; \
	  cd web && VITE_APPROVALS_URL="$$url" VITE_API_URL="$$api" npm run build
	firebase deploy --only hosting --project $(PROJECT_ID)

# Puts a screenplay into a DEPLOYED project, so the hosted panel has something
# on it. Goes through the deployed service's own HTTP API rather than writing
# to Firestore, so a successful seed is also evidence the deployment works.
# It refuses to run against a deployment that sends real email.
# The one thing in this repo that hands a message to Google. Deliberately not
# part of any automated target: it sends real email to a real inbox.
#
#   make gmail-smoke TO=seller@example.com   # send one
#   make gmail-smoke POLL=1                  # read the reply
#   make gmail-smoke INSPECT=1               # show the thread, change nothing
#   make gmail-smoke RECENT=1                # unread anywhere, with thread ids
#   make gmail-smoke FIND=1                  # all mail from the seller, spam too
#   make gmail-smoke REARM=1                 # make our thread's replies unread
#
# INSPECT exists because POLL can only say "nothing unread", which is the same
# answer for a reply that never arrived and a reply that was opened in Gmail
# before the poll saw it — opening a message clears UNREAD. RECENT answers the
# one INSPECT cannot: a reply that is not in our thread is somewhere, and where
# tells you whether Gmail is slow or whether it started its own conversation —
# the second being the failure the loop can never recover from. FIND goes one
# further and looks in SPAM and TRASH, which Gmail hides from every listing by
# default — so a filtered reply is somewhere nothing else here can see. REARM
# resets the check against replies we already have, rather than depending on
# somebody sending another one and then not opening their own inbox.
#
# Add CINEMA_TOKEN_BACKEND=secret-manager CINEMA_GCP_PROJECT=... when the token
# was bootstrapped into Secret Manager rather than a local file.
gmail-smoke: ## Send one real email and read the reply (needs a bootstrapped token)
	@if [ -n "$(REARM)" ]; then \
	  uv run python scripts/gmail_smoke.py --rearm; \
	elif [ -n "$(FIND)" ]; then \
	  if [ "$(FIND)" = "1" ]; then \
	    uv run python scripts/gmail_smoke.py --find; \
	  else \
	    uv run python scripts/gmail_smoke.py --find "$(FIND)"; \
	  fi; \
	elif [ -n "$(RECENT)" ]; then \
	  uv run python scripts/gmail_smoke.py --recent; \
	elif [ -n "$(INSPECT)" ]; then \
	  uv run python scripts/gmail_smoke.py --inspect; \
	elif [ -n "$(POLL)" ]; then \
	  uv run python scripts/gmail_smoke.py --poll; \
	else \
	  [ -n "$(TO)" ] || { echo "make gmail-smoke TO=seller@example.com"; exit 2; }; \
	  uv run python scripts/gmail_smoke.py --to "$(TO)"; \
	fi

seed: require-gcloud require-project ## Seed a deployed project with a screenplay
	uv run python scripts/seed_project.py --project-id $(PROJECT_ID) $(ARGS)

image: ## Build the Cloud Run image (context is the repo root, deliberately)
	docker build -t greenlit:local .

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ web/dist
