
.PHONY: up down

up:
	@if not exist .env copy .env.example .env
	docker compose up --build -d
	@echo "Backend: http://localhost:8000/health"
	@echo "Frontend: http://localhost:3000"

down:
	docker compose down

