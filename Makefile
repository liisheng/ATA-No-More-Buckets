.PHONY: backend-check frontend-check test docker-build smoke

backend-check:
	python -m ruff check backend/app backend/tests
	cd backend && python -m mypy app

frontend-check:
	cd frontend && npm run lint && npm test -- --run && npm run build

test:
	python -m pytest backend/tests

docker-build:
	docker build -t no-more-buckets:local .

smoke:
	powershell -ExecutionPolicy Bypass -File infra/smoke.ps1
