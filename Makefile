TEST_DB_URL ?= postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test

.PHONY: test-db test lint check

test-db:
	docker start helpdesk-test-db 2>/dev/null || docker run -d --name helpdesk-test-db \
	  -p 127.0.0.1:5433:5432 -e POSTGRES_USER=helpdesk -e POSTGRES_PASSWORD=helpdesk \
	  -e POSTGRES_DB=helpdesk_test postgres:16

test:
	TEST_DATABASE_URL=$(TEST_DB_URL) uv run pytest -q

lint:
	uv run ruff check src tests && uv run ruff format --check src tests

check: lint test
