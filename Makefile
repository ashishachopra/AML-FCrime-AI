.PHONY: help setup validate build run stop clean clean-data install test lint format health demo

PYTHON ?= python

help:
	@echo "AML reference pipeline"
	@echo "  make setup       Create local configuration and random secret files"
	@echo "  make validate    Validate the Compose configuration"
	@echo "  make run         Build and start the stack, waiting for readiness"
	@echo "  make stop        Stop the stack without deleting data"
	@echo "  make clean       Remove containers and orphaned resources"
	@echo "  make clean-data  Remove containers and named data volumes"
	@echo "  make install     Install pinned development dependencies"
	@echo "  make test        Run the automated tests"
	@echo "  make lint        Run Ruff lint and formatting checks"
	@echo "  make demo        Run the authenticated fixture workflow"

setup:
	@test -f .env || cp example.env.txt .env
	$(PYTHON) scripts/create_local_secrets.py
	@echo "Local configuration and secret files are ready."

validate:
	docker compose config --quiet

build:
	docker compose build

run:
	docker compose up --build --detach --wait

stop:
	docker compose down

clean:
	docker compose down --remove-orphans

clean-data:
	docker compose down --volumes --remove-orphans

install:
	$(PYTHON) -m pip install --requirement requirements-dev.txt

test:
	$(PYTHON) -m pytest

lint:
	ruff check services tests scripts complete_pipeline_demo.py test_openai_env.py
	ruff format --check services tests scripts complete_pipeline_demo.py test_openai_env.py

format:
	ruff check services tests scripts complete_pipeline_demo.py test_openai_env.py --fix
	ruff format services tests scripts complete_pipeline_demo.py test_openai_env.py

health:
	curl --fail --silent http://127.0.0.1:8000/health/ready

demo:
	$(PYTHON) complete_pipeline_demo.py
