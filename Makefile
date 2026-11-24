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
.PHONY: help image gen up stim run dash targets report check shot test-plugins test-released dist publish-precheck publish down clean logs env-vars

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

stim: image gen ## Run the one-shot bench stimulators to store KV chunks
	$(COMPOSE) --profile stim up --detach $(shell seq 1 $(NUM) | sed 's/^/stim-/')

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

check: ## Assert every server registered and holds both L1 and L2 placements
	python3 scripts/check_fleet.py $(NUM)

shot: ## Screenshot the Grafana dashboard to SHOT (needs playwright chromium)
	@port=$$($(COMPOSE) port grafana 3000 2>/dev/null | cut -d: -f2); \
	if [ -z "$$port" ]; then echo "grafana is not running — try 'make up'"; exit 1; fi; \
	python3 scripts/grafana_shot.py "http://localhost:$$port" -o $(SHOT) \
		--settle $(SETTLE) --window $(WINDOW)

logs: ## Follow logs from every service
	$(COMPOSE) logs -f --tail=100

##@ Teardown

down: ## Stop and remove the fleet containers
	$(COMPOSE) --profile stim down --remove-orphans

clean: down ## Stop the fleet and delete cached L2 blobs plus metric history
	@echo "removing bind-mount contents under ./data/"
	rm -rf ./data/l2-server-* ./data/prometheus ./data/grafana ./data/exporter

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
