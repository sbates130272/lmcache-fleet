# LMCache Fleet Demo — see scripts/gen_compose.py
COMPOSE := docker compose -f docker/docker-compose.yml
# Built from docker/Dockerfile, not pulled. `make image` builds it and every
# target that needs it depends on that, so a fresh clone needs no extra step.
LMCACHE_IMAGE ?= lmcache-fleet-demo/lmcache:0.5.8-cpu
NUM ?= 3
SEQ ?= 8
SHOT ?= grafana.png
SETTLE ?= 8
WINDOW ?= 15m

define TARGETS_PY
import json, sys
for t in json.load(sys.stdin)["data"]["activeTargets"]:
    print(f'  {t["labels"].get("job","?"):22s} {t["scrapeUrl"]:44s} {t["health"]}')
endef
export TARGETS_PY

.DEFAULT_GOAL := help
.PHONY: help image gen up stim restore trace trace-deps trace-synth trace-exgentic trace-analyze trace-report run dash targets report check shot test-plugins test-released dist publish-precheck publish down clean logs env-vars

# One publishable distribution per plugins/<dir>/pyproject.toml, each built and
# uploaded on its own. Discovered by wildcard rather than listed, so a new
# plugin directory needs no Makefile change. PLUGIN=<dir> narrows to one.
PLUGIN_DIRS := $(patsubst %/,%,$(dir $(wildcard plugins/$(if $(PLUGIN),$(PLUGIN),*)/pyproject.toml)))

# `build` has to be importable by the python3 that runs `python3 -m build`, so
# pipx is no help: it isolates the module in its own venv and exposes only the
# pyproject-build entry point. `pip install --user` is no help either on a
# stock Debian or Ubuntu, whose python3 is externally managed and refuses it.
# A venv is the one answer that works everywhere.
PKG_TOOLS_HINT := get them with  python3 -m venv .venv && . .venv/bin/activate && pip install build twine

##@ Fleet Lifecycle

image: ## Build the CPU-only lmcache image (no-op if it already exists)
	@docker image inspect $(LMCACHE_IMAGE) >/dev/null 2>&1 || { \
		echo "building $(LMCACHE_IMAGE) from docker/Dockerfile..."; \
		docker build -f docker/Dockerfile -t $(LMCACHE_IMAGE) . ; }

gen: ## Regenerate docker-compose.yml for NUM servers
	NUM_SERVERS=$(NUM) SEQ=$(SEQ) python3 scripts/gen_compose.py

up: image gen ## Regenerate, then start the fleet plus prometheus and grafana
	$(COMPOSE) up -d
	@echo "waiting for servers to be healthy..."
	@$(COMPOSE) ps --format '{{.Name}} {{.Status}}'
	@$(MAKE) --no-print-directory dash

stim: image gen ## Run the one-shot stimulators to store KV chunks
	$(COMPOSE) --profile stim up --detach $(shell seq 1 $(NUM) | sed 's/^/stim-/')

# The read side, and separate on purpose: the stimulators only store, so what
# gets fetched back, how fast and in what order is this target's alone. Run it
# after `stim` to pull blocks back from L1 and L2, or alongside it. Regenerating
# changes only the stim and restore services, so the servers are left running.
restore: image gen ## Run the one-shot restore stimulators to fetch KV chunks back
	$(COMPOSE) --profile restore up --detach $(shell seq 1 $(NUM) | sed 's/^/restore-/')

# Replays a token trace instead of the synthetic windows: every trace-N computes
# the same schedule and keeps the requests routed to its own server. Pass the
# variables the fleet was started with, plus TRACE and any TRACE_* knobs.
trace: image gen ## Replay the token trace TRACE (a directory under data/traces)
	@test -n "$(TRACE)" || { echo "set TRACE=<dir under data/traces>, e.g. TRACE=synth"; exit 1; }
	$(COMPOSE) --profile trace up --detach $(shell seq 1 $(NUM) | sed 's/^/trace-/')

# `up` and `stim` stay separate so the stimulators can be re-run against a warm
# fleet, or the fleet inspected before anything is stored. For a plain run,
# this does both. The stimulators wait on the servers' healthchecks either way.
run: up stim ## Start the fleet and immediately run the stimulators
	@echo "fleet running; 'make report NUM=$(NUM)' or 'make dash' to watch"

test-plugins: image ## Run the plugin test suites inside the lmcache image
	@docker run --rm \
		-v "$(CURDIR)/plugins:/plugins:ro" \
		-e HOME=/tmp -e PYTHONPATH=/plugins/lmcache-l2-sim \
		-e LMCACHE_DISABLE_BANNER=1 \
		--entrypoint "" --user "$(shell id -u):$(shell id -g)" \
		$(LMCACHE_IMAGE) \
		sh -c '/opt/venv/bin/python -m pytest /plugins/*/tests -q -p no:cacheprovider'

##@ Traces

# Everything here runs scripts/traces.py inside the lmcache image, which already
# has transformers and tokenizers. Downloads (tokenizer files, pyarrow) land
# under data/traces/cache, so they survive between runs and `make clean`.
# HF_TOKEN is passed by name only: docker takes the value from the environment,
# so it is never expanded into a command line that make echoes or `ps` shows.
TRACE_PY = docker run --rm --entrypoint "" --user "$(shell id -u):$(shell id -g)" \
	-e HOME=/tmp -e HF_HOME=/traces/cache/hf -e PYTHONPATH=/traces/cache/site \
	-e HF_HUB_DISABLE_PROGRESS_BARS=1 -e HF_TOKEN \
	-v "$(CURDIR)/data/traces:/traces" -v "$(CURDIR)/data/trace-runs:/runs" \
	-v "$(CURDIR)/scripts:/scripts:ro" \
	$(LMCACHE_IMAGE) /opt/venv/bin/python -u /scripts/traces.py
EXGENTIC_URL := https://huggingface.co/datasets/Exgentic/agent-llm-traces-v2/resolve/main/data/train
SHARDS ?= 0000
SESSIONS ?= 12
TOKENIZER ?= Qwen/Qwen2.5-7B-Instruct
MAX_INPUT_TOKENS ?= 130000

trace-deps: image ## Install pyarrow under data/traces/cache (only `trace-exgentic` needs it)
	@mkdir -p data/traces/cache/site data/traces/cache/hf data/trace-runs
	@test -d data/traces/cache/site/pyarrow || docker run --rm --entrypoint "" \
		--user "$(shell id -u):$(shell id -g)" -e HOME=/tmp \
		-v "$(CURDIR)/data/traces:/traces" $(LMCACHE_IMAGE) \
		/opt/venv/bin/pip install -q --target /traces/cache/site pyarrow

trace-synth: image ## Generate an artificial token trace into data/traces/NAME (SYNTH_ARGS=...)
	@mkdir -p data/traces data/trace-runs
	$(TRACE_PY) synth /traces/$(or $(NAME),synth) $(SYNTH_ARGS)

# Real agent sessions with their text: Exgentic/agent-llm-traces-v2. Converted
# to aiperf's mooncake_trace JSONL first, so the same file replays against a
# real engine with `aiperf profile --custom-dataset-type mooncake_trace`, then
# tokenized with the chat template of the model you would serve.
trace-exgentic: trace-deps ## Download, convert and tokenize SESSIONS real agent sessions
	@mkdir -p data/traces/raw/exgentic-v2
	@for s in $(SHARDS); do f=data/traces/raw/exgentic-v2/$$s.parquet; \
		test -f $$f || { echo "fetching $$s.parquet"; curl -fsSL -o $$f $(EXGENTIC_URL)/$$s.parquet || exit 1; }; done
	$(TRACE_PY) convert /traces/raw/exgentic-v2 /traces/$(or $(NAME),exgentic) \
		--sessions $(SESSIONS) --max-input-tokens $(MAX_INPUT_TOKENS) $(CONVERT_ARGS)
	$(TRACE_PY) tokenize /traces/$(or $(NAME),exgentic) /traces/$(or $(NAME),exgentic)/tokens \
		--tokenizer $(TOKENIZER)
	@echo "replay it with: make trace TRACE=$(or $(NAME),exgentic)/tokens"

trace-analyze: image ## What an unbounded cache could reuse from TRACE, by routing and sharing
	@test -n "$(TRACE)" || { echo "set TRACE=<dir under data/traces>"; exit 1; }
	@mkdir -p data/trace-runs
	$(TRACE_PY) analyze /traces/$(TRACE) $(ANALYZE_ARGS)

trace-report: image ## Hit rates of the last trace replay, per client and for the fleet
	$(TRACE_PY) report /runs/$(or $(TRACE_RUN),latest) $(REPORT_ARGS)

##@ Packaging

# `make test-plugins` tests the working tree; this tests what people actually
# install. It pulls each distribution from PyPI into the image and runs the
# repo's own suite against that copy, so a module missing from the wheel, or a
# release that drifted from the source it was cut from, fails here. Only the
# tests directory is mounted — the source package never enters the container,
# so there is nothing for the installed copy to be shadowed by.
#
# The distribution name is the directory name, which is the convention
# plugins/README.md sets for adding one. VERSION=x.y.z pins a release;
# unset takes whatever PyPI currently calls latest.
test-released: image ## Run the plugin tests against the PyPI release, not the working tree
	@test -n "$(PLUGIN_DIRS)" || { echo "no plugin matching '$(PLUGIN)' under plugins/"; exit 1; }
	@for d in $(PLUGIN_DIRS); do \
		name=$$(basename "$$d"); \
		test -d "$$d/tests" || { echo "==> $$name: no tests/, skipping"; continue; }; \
		echo "==> $$name$(if $(VERSION),==$(VERSION), (latest)) from PyPI"; \
		docker run --rm \
			-v "$(CURDIR)/$$d/tests:/tests:ro" \
			-e HOME=/tmp -e LMCACHE_DISABLE_BANNER=1 \
			-e PKG_NAME="$$name" -e PKG_SPEC="$$name$(if $(VERSION),==$(VERSION),)" \
			--entrypoint "" --user "$(shell id -u):$(shell id -g)" \
			$(LMCACHE_IMAGE) sh -ec '\
				/opt/venv/bin/pip install -q --target /tmp/site "$$PKG_SPEC"; \
				export PYTHONPATH=/tmp/site; \
				/opt/venv/bin/python -c "import os, importlib.metadata as m; \
					print(\"    installed\", m.version(os.environ[\"PKG_NAME\"]), \"from PyPI\")"; \
				/opt/venv/bin/python -m pytest /tests -q -p no:cacheprovider' || exit 1; \
	done

dist: ## Build and validate sdist+wheel for every plugin (PLUGIN=<dir> for one)
	@test -n "$(PLUGIN_DIRS)" || { echo "no plugin matching '$(PLUGIN)' under plugins/"; exit 1; }
	@python3 -c 'import build' 2>/dev/null \
		|| { echo "missing build; $(PKG_TOOLS_HINT)"; exit 1; }
	@command -v twine >/dev/null \
		|| { echo "missing twine; $(PKG_TOOLS_HINT)"; exit 1; }
	@for d in $(PLUGIN_DIRS); do \
		echo "==> $$d"; \
		rm -rf "$$d/dist" "$$d"/*.egg-info; \
		( cd "$$d" && python3 -m build ) || exit 1; \
		twine check "$$d"/dist/* || exit 1; \
	done

# A wheel that builds and passes `twine check` can still be unimportable once
# installed — a wrong `packages =`, a module left out of the distribution. Only
# the top-level packages are imported here: the adapter module itself needs
# LMCache, which `make test-plugins` covers inside the image.
publish-precheck: dist ## Install each built wheel into a throwaway uv venv and import it
	@command -v uv >/dev/null || { echo "missing uv; see https://docs.astral.sh/uv/"; exit 1; }
	@root=$$(mktemp -d) || exit 1; \
	trap 'rm -rf "$$root"' EXIT; \
	trap 'exit 130' INT; trap 'exit 143' TERM HUP; \
	for d in $(PLUGIN_DIRS); do \
		for whl in "$$d"/dist/*.whl; do \
			echo "==> $$whl"; \
			mods=$$(unzip -p "$$whl" '*/top_level.txt' 2>/dev/null | tr -d '\r'); \
			test -n "$$mods" || { \
				echo "    no top_level.txt in $$whl: nothing to import, refusing to pass" >&2; \
				exit 1; }; \
			venv="$$root/$$(basename "$$whl" .whl)"; \
			uv venv -q "$$venv" || exit 1; \
			VIRTUAL_ENV="$$venv" uv pip install -q "$$whl" || exit 1; \
			for m in $$mods; do \
				"$$venv"/bin/python -c "import $$m" || exit 1; \
				echo "    import $$m: ok"; \
			done; \
			rm -rf "$$venv"; \
		done; \
	done

publish: publish-precheck ## Upload to PyPI — irreversible, needs CONFIRM=1
	@test "$(CONFIRM)" = "1" || { \
		echo "refusing: a version uploaded to PyPI can never be replaced, only yanked."; \
		echo "prechecks above passed; to upload: make publish CONFIRM=1"; exit 1; }
	@for d in $(PLUGIN_DIRS); do twine upload "$$d"/dist/*; done

##@ Inspection

dash: ## Print the Grafana URL (compose picks a free port from the range)
	@port=$$($(COMPOSE) port grafana 3000 2>/dev/null | cut -d: -f2); \
	if [ -n "$$port" ]; then echo "Grafana: http://localhost:$$port  (anonymous, opens on the fleet dashboard)"; \
	else echo "grafana is not running — try 'make up'"; exit 1; fi

targets: ## Show what Prometheus is scraping and whether it is up
	@$(COMPOSE) exec -T prometheus wget -qO- \
		'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null \
		| python3 -c "$$TARGETS_PY" \
		|| echo "prometheus is not running — try 'make up'"

report: ## Print the key directory, per-server L1 usage and L2 files vs disk
	scripts/report.sh $(NUM)

# With L2_CAPACITY_GIB set, as for the run being checked, it also requires every
# server to have evicted from L2 and to sit within that capacity: a capacity
# that is configured but never enforced fills both tiers just the same.
check: ## Assert every server registered and holds both L1 and L2 placements
	python3 scripts/check_fleet.py $(NUM) $(if $(filter-out 0 0.0,$(L2_CAPACITY_GIB)),--l2-eviction)

shot: ## Screenshot the Grafana dashboard to SHOT (needs playwright chromium)
	@port=$$($(COMPOSE) port grafana 3000 2>/dev/null | cut -d: -f2); \
	if [ -z "$$port" ]; then echo "grafana is not running — try 'make up'"; exit 1; fi; \
	python3 scripts/grafana_shot.py "http://localhost:$$port" -o $(SHOT) \
		--settle $(SETTLE) --window $(WINDOW)

# Both profiles named explicitly: a service behind a profile is invisible to
# `compose logs` unless its profile is active, so the plain form silently
# omitted the stimulators -- the one place a run's own summary line is printed.
logs: ## Follow logs from every service, stimulators included
	$(COMPOSE) --profile stim --profile restore --profile trace logs -f --tail=100

##@ Teardown

down: ## Stop and remove the fleet containers
	$(COMPOSE) --profile stim --profile restore --profile trace down --remove-orphans

clean: down ## Stop the fleet and delete cached L2 blobs plus metric history
	@echo "removing bind-mount contents under ./data/"
	rm -rf ./data/l2-server-* ./data/l2-shared-* ./data/prometheus ./data/grafana ./data/exporter ./data/coordinator

##@ Help

env-vars: ## List every environment variable, grouped, with its default
	@python3 scripts/gen_compose.py --env-vars

# Colour only on a terminal and only without NO_COLOR, matching --env-vars, so
# piping or redirecting either listing gives plain text. The %-17s padding sits
# inside the colour so the escapes never count toward the column width.
help: ## Show this help
	@tty=0; [ -t 1 ] && [ -z "$$NO_COLOR" ] && tty=1; \
	awk -v c=$$tty 'BEGIN { \
		FS = ":.*##"; \
		if (c) { B = "\033[1m"; C = "\033[36m"; D = "\033[2m"; O = "\033[0m" } \
		printf "\n%sLMCache Fleet Demo%s\n\n", B, O; \
		printf "  %susage:%s make %s<target>%s %s[NUM=<servers>] [SEQ=<sequences>]%s\n", D, O, C, O, D, O; \
	} \
	/^##@/ { printf "\n%s%s%s\n", B, substr($$0, 5), O } \
	/^[a-zA-Z0-9_-]+:.*##/ { printf "  %s%-17s%s %s\n", C, $$1, O, $$2 } \
	END { printf "\n  %scurrent: NUM=$(NUM) SEQ=$(SEQ)%s\n\n", D, O }' $(MAKEFILE_LIST)
