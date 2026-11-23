# LMCache Fleet Demo — see gen_compose.py
NUM ?= 3
SEQ ?= 8

.DEFAULT_GOAL := help
.PHONY: help gen up stim report down clean logs

##@ Fleet Lifecycle

gen: ## Regenerate docker-compose.yml for NUM servers
	NUM_SERVERS=$(NUM) SEQ=$(SEQ) python3 gen_compose.py

up: gen ## Regenerate, then start the coordinator and servers
	docker compose up -d
	@echo "waiting for servers to be healthy..."
	@docker compose ps --format '{{.Name}} {{.Status}}'

stim: gen ## Run the one-shot bench stimulators to store KV chunks
	docker compose --profile stim up --detach $(shell seq 1 $(NUM) | sed 's/^/stim-/')

##@ Inspection

report: ## Print the coordinator key directory and per-server L1 usage
	./report.sh $(NUM)

logs: ## Follow logs from every service
	docker compose logs -f --tail=100

##@ Teardown

down: ## Stop and remove the fleet containers
	docker compose --profile stim down --remove-orphans

clean: down ## Stop the fleet and delete the cached L2 blobs
	@echo "removing L2 bind-mount contents under ./data/"
	rm -rf ./data/l2-server-*

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
