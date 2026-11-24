# LMCache Fleet Demo — see scripts/gen_compose.py
COMPOSE := docker compose -f docker/docker-compose.yml
NUM ?= 3
SEQ ?= 8

define TARGETS_PY
import json, sys
for t in json.load(sys.stdin)["data"]["activeTargets"]:
    print(f'  {t["labels"].get("job","?"):22s} {t["scrapeUrl"]:44s} {t["health"]}')
endef
export TARGETS_PY

.DEFAULT_GOAL := help
.PHONY: help gen up stim run dash targets report down clean logs

##@ Fleet Lifecycle

gen: ## Regenerate docker-compose.yml for NUM servers
	NUM_SERVERS=$(NUM) SEQ=$(SEQ) python3 scripts/gen_compose.py

up: gen ## Regenerate, then start the fleet plus prometheus and grafana
	$(COMPOSE) up -d
	@echo "waiting for servers to be healthy..."
	@$(COMPOSE) ps --format '{{.Name}} {{.Status}}'
	@$(MAKE) --no-print-directory dash

stim: gen ## Run the one-shot bench stimulators to store KV chunks
	$(COMPOSE) --profile stim up --detach $(shell seq 1 $(NUM) | sed 's/^/stim-/')

# `up` and `stim` stay separate so the stimulators can be re-run against a warm
# fleet, or the fleet inspected before anything is stored. For a plain run,
# this does both. The stimulators wait on the servers' healthchecks either way.
run: up stim ## Start the fleet and immediately run the stimulators
	@echo "fleet running; 'make report NUM=$(NUM)' or 'make dash' to watch"

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

logs: ## Follow logs from every service
	$(COMPOSE) logs -f --tail=100

##@ Teardown

down: ## Stop and remove the fleet containers
	$(COMPOSE) --profile stim down --remove-orphans

clean: down ## Stop the fleet and delete cached L2 blobs plus metric history
	@echo "removing bind-mount contents under ./data/"
	rm -rf ./data/l2-server-* ./data/prometheus ./data/grafana ./data/exporter

##@ Help

help: ## Show this help
	@awk 'BEGIN { \
		FS = ":.*##"; \
		printf "\n\033[1mLMCache Fleet Demo\033[0m\n\n"; \
		printf "  \033[2musage:\033[0m make \033[36m<target>\033[0m \033[2m[NUM=<servers>] [SEQ=<sequences>]\033[0m\n"; \
	} \
	/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } \
	/^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 } \
	END { printf "\n  \033[2mcurrent: NUM=$(NUM) SEQ=$(SEQ)\033[0m\n\n" }' $(MAKEFILE_LIST)
