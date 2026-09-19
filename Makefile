.PHONY: dev api web test lint demo

api:
	uvicorn business_signals.main:app --app-dir apps/api/src --reload --port 8000

web:
	npm --prefix apps/web run dev

test:
	pytest

lint:
	ruff check apps/api && npm --prefix apps/web run lint

demo:
	docker compose up -d --wait
