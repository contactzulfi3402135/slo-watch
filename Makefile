PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help install test lint demo report docs docker clean

help:
	@echo "install  create $(VENV) and install slo-watch with dev extras"
	@echo "test     run the test suite"
	@echo "demo     start the flaky service, run slo-watch for 90s, print the report"
	@echo "docs     re-render docs/*.png"
	@echo "docker   build and run the compose stack"

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q -e ".[dev]"

test:
	$(BIN)/python -m pytest -q

lint:
	$(BIN)/python -m compileall -q slo_watch

demo:
	@mkdir -p /tmp/slo-watch-demo
	$(BIN)/python demo/flaky.py --fast --port 8099 & echo $$! > /tmp/slo-watch-demo/flaky.pid
	@sleep 1
	$(BIN)/slo-watch -c config.yml serve --port 8080 & echo $$! > /tmp/slo-watch-demo/serve.pid
	@sleep 90
	@kill $$(cat /tmp/slo-watch-demo/serve.pid) $$(cat /tmp/slo-watch-demo/flaky.pid) 2>/dev/null || true
	$(BIN)/slo-watch -c config.yml report

report:
	$(BIN)/slo-watch -c config.yml report

docs:
	$(BIN)/python scripts/render_docs.py --db slo-watch.db --out docs --transcript transcript.txt

docker:
	docker compose up --build

clean:
	rm -rf $(VENV) *.egg-info .pytest_cache **/__pycache__ *.db *.db-wal *.db-shm
