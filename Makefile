PYTHON ?= python3

.PHONY: test build
test:
	$(PYTHON) -m unittest discover -s tests -v

build:
	docker compose build review-worker
