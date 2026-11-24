# LLM Chatbot with Key Failover and Response Caching

React + FastAPI + LiteLLM + SQLite. Multi-turn chat, automatic API-key failover, LiteLLM response caching, per-request token metadata.

## 1. Live URLs

- Frontend: https://lyftr-ai-chatbot-zeta.vercel.app/
- Backend: https://lyftr-ai-chatbot-azdc.onrender.com/ (health check: https://lyftr-ai-chatbot-azdc.onrender.com/health)
- GitHub repository: https://github.com/AkshatAg10/lyftr-ai-chatbot

## 2. Run locally: with Docker and without

### With Docker (requires Docker with Compose v2)

```bash
cp .env.example .env      # then edit .env and set LLM_API_KEYS=<key1>,<key2>
make up                   # backend http://localhost:8000, frontend http://localhost:3000
make down                 # stop (the SQLite volume `app_data` is kept)
```

`.env` is git-ignored. Real keys must only ever live there (or in your host's environment settings).

### Without Docker

Backend (Python 3.12):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LLM_API_KEYS="<key1>,<key2>"
export DATABASE_URL="sqlite:///./app.db"
export ALLOWED_ORIGINS= "https://lyftr-ai-chatbot-azdc.onrender.com/"
export ENABLE_FAILURE_SIMULATION=true
uvicorn main:app --port 8000
```

Frontend (Node 20+):

```bash
cd frontend
npm install
VITE_API_URL=https://lyftr-ai-chatbot-zeta.vercel.app/ npm run dev 
```

Backend tests (no network or real keys needed): `cd backend && python -m pytest -q`

### Environment variables

| Variable | Meaning |
|---|---|
| `LLM_API_KEYS` | Comma-separated key pool (>= 2). Ids are `key_1`, `key_2`, ... in order |
| `LLM_MODEL` | LiteLLM model name (default `gemini/gemini-3.5-flash-lite`) |
| `DATABASE_URL` | SQLite location, e.g. `sqlite:////data/app.db` |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `ENABLE_FAILURE_SIMULATION` | `true` enables the `X-Simulate-Failure` header |
| `VITE_API_URL` | Frontend build-time backend URL |

## 3. Design decisions: key fallback, caching, SQLite session storage

**Key fallback.** `LLM_API_KEYS` is parsed into an ordered pool (`key_1`, `key_2`, ...). For each request the backend calls `litellm.completion` with `key_1`; on any exception (429, quota, 5xx, timeout, connection or auth error) it moves to the next key and counts a failure. `fallback_attempts` is the number of keys that failed before the successful one. If every key fails the API returns `503 {"detail": "all providers unavailable"}` and nothing is stored. Provider errors are logged server-side only; keys never appear in responses. LiteLLM's own retries are disabled (`num_retries=0`) so that failover between keys is immediate and visible.

**Failure simulation.** With `ENABLE_FAILURE_SIMULATION=true`, the `X-Simulate-Failure` header (`key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503) makes the listed keys raise the matching LiteLLM exception inside that request only; the next key then makes a real provider call. Because a failover demo must show a real call, cache reads are skipped for requests that carry a simulation header.

**Caching.** LiteLLM's built-in in-memory cache (`litellm.cache = Cache(type="local")`). LiteLLM builds the key from the model and the complete message list (system prompt + history + new message), not from the API key. So the same first message in a new session is a hit, a different message or different history never is. A hit is detected from LiteLLM's `cache_hit` flag, returns the identical reply with the token usage stored with it, `key_id: null` and `fallback_attempts: 0`. The cache is in memory, so it resets when the backend restarts.

**SQLite session storage.** Table `messages` follows the required schema (plus an index). A small `sessions` table stores the optional per-session system prompt, so it stays out of the history. The client sends only the new message; the backend rebuilds the full conversation from SQLite on every call. The user and assistant rows are written in one transaction only after a successful reply, so failed requests (422/503) leave no history. The schema is created on startup, and in Docker the file lives at `/data/app.db` on the named volume `app_data`.

**Tokens and latency.** `input_tokens`/`output_tokens` come from the usage of that one request only; there is no running total. `latency_ms` is measured on the server from the start of the request to just before persisting and responding.

## 4. Verification: how to see failover, cache hits and per-message token usage

Replace `BASE` with `http://localhost:8000` or your backend URL.

```bash
BASE=http://localhost:8000
H='Content-Type: application/json'

# Health
curl -s $BASE/health

# Live inference, then a cache hit from a DIFFERENT session (same first message)
curl -s -X POST $BASE/chat -H "$H" -d '{"session_id":"demo-1","message":"What is the capital of France?"}'
curl -s -X POST $BASE/chat -H "$H" -d '{"session_id":"demo-2","message":"What is the capital of France?"}'
#  -> second response: resolution_source LLM_CACHE, same reply, key_id null, lower latency_ms

# Failover: key_1 "fails", key_2 answers
curl -s -X POST $BASE/chat -H "$H" -H 'X-Simulate-Failure: key_1=429' -d '{"session_id":"demo-3","message":"Name a prime number"}'
#  -> 200, key_id "key_2", fallback_attempts 1

# All keys fail -> 503, and nothing stored
curl -s -w '\n%{http_code}\n' -X POST $BASE/chat -H "$H" -H 'X-Simulate-Failure: *=429' -d '{"session_id":"demo-4","message":"hello"}'
curl -s -w '\n%{http_code}\n' $BASE/sessions/demo-4/messages     # 404 session not found

# Validation -> 422
curl -s -w '\n%{http_code}\n' -X POST $BASE/chat -H "$H" -d '{"session_id":"demo-5","message":"   "}'

# Multi-turn + per-message tokens in stored history
curl -s -X POST $BASE/chat -H "$H" -d '{"session_id":"demo-6","message":"My name is Sam."}'
curl -s -X POST $BASE/chat -H "$H" -d '{"session_id":"demo-6","message":"What is my name?"}'
curl -s $BASE/sessions/demo-6/messages
```

In the UI, every assistant reply shows source, model, latency, input tokens and output tokens. `input_tokens` grows with history because the history is part of the prompt, but each value is for that request only. To see persistence: `make down && make up`, then call `GET /sessions/demo-6/messages` again (the SQLite file is on the volume). `make down` does not remove the volume.

## 5. Setup Used: your editor and AI tools

`<FILL IN: your editor (e.g. VS Code) and the AI tools you used (e.g. Claude)>`

## 6. Bonus features (if any): what you added beyond the requirements and why

No additional features were added beyond the requirements.
