# LMCache MP fleet demo — see gen_compose.py
NUM ?= 3
SEQ ?= 8

.PHONY: gen up stimulate report down clean logs

gen:
	NUM_SERVERS=$(NUM) SEQ=$(SEQ) python3 gen_compose.py

up: gen
	docker compose up -d
	@echo "waiting for servers to be healthy..."
	@docker compose ps --format '{{.Name}} {{.Status}}'

stimulate:
	docker compose --profile stimulate up --detach $(shell seq 1 $(NUM) | sed 's/^/stimulate-/')

report:
	./report.sh $(NUM)

logs:
	docker compose logs -f --tail=100

down:
	docker compose --profile stimulate down --remove-orphans

clean: down
	@echo "removing L2 bind-mount contents under ./data/"
	rm -rf ./data/l2-server-*
