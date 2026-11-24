"""LLM chatbot backend: FastAPI + LiteLLM (key failover, response cache) + SQLite."""
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from litellm.caching.caching import Cache
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("chatbot")
logging.basicConfig(level=logging.INFO)

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant. Answer clearly and concisely."
SIMULATED_STATUSES = {429, 500, 502, 503}

# --------------------------------------------------------------------------- config


def get_keys() -> list[str]:
    return [k.strip() for k in os.getenv("LLM_API_KEYS", "").split(",") if k.strip()]


def get_model() -> str:
    return os.getenv("LLM_MODEL", "gemini/gemini-2.5-flash")


def get_db_path() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:////data/app.db")
    prefix = "sqlite:///"
    path = url[len(prefix):] if url.startswith(prefix) else url
    return path or "app.db"


def simulation_enabled() -> bool:
    return os.getenv("ENABLE_FAILURE_SIMULATION", "false").strip().lower() == "true"


# --------------------------------------------------------------------------- LiteLLM cache
# Built-in in-memory LiteLLM cache. LiteLLM derives the cache key from the model and the
# complete message list (plus fixed sampling params), not from the API key.
litellm.cache = Cache(type="local")
litellm.drop_params = True

# --------------------------------------------------------------------------- database


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    directory = os.path.dirname(get_db_path())
    if directory:
        os.makedirs(directory, exist_ok=True)
    with closing(connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                system_prompt TEXT
            );
            """
        )
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- app

app = FastAPI(title="LLM Chatbot")

origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")  # stack trace stays in server logs only
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# --------------------------------------------------------------------------- schemas


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str
    system_prompt: Optional[str] = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 4000:
            raise ValueError("message must be 1-4000 characters after trimming whitespace")
        return v

    @field_validator("system_prompt")
    @classmethod
    def normalize_system_prompt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


# --------------------------------------------------------------------------- failure simulation


def parse_simulation(header: Optional[str]) -> dict[str, int]:
    """'key_1=429,key_2=500' -> {'key_1': 429, 'key_2': 500}; '*=429' -> {'*': 429}."""
    result: dict[str, int] = {}
    if not header or not simulation_enabled():
        return result
    for pair in header.split(","):
        name, sep, status = pair.strip().partition("=")
        if not sep:
            continue
        try:
            code = int(status.strip())
        except ValueError:
            continue
        if code in SIMULATED_STATUSES and name.strip():
            result[name.strip()] = code
    return result


def simulated_error(status: int, model: str) -> Exception:
    msg = f"simulated provider failure {status}"
    if status == 429:
        return litellm.RateLimitError(message=msg, llm_provider="simulated", model=model)
    if status == 503:
        return litellm.ServiceUnavailableError(message=msg, llm_provider="simulated", model=model)
    if status == 500:
        return litellm.InternalServerError(message=msg, llm_provider="simulated", model=model)
    return litellm.APIError(status_code=status, message=msg, llm_provider="simulated", model=model)


# --------------------------------------------------------------------------- LLM call with failover


def call_with_failover(model: str, messages: list[dict], simulation: dict[str, int]):
    """Try each key in order. Returns (response, key_id, fallback_attempts) or raises 503."""
    keys = get_keys()
    failures = 0
    for index, api_key in enumerate(keys, start=1):
        key_id = f"key_{index}"
        try:
            status = simulation.get(key_id, simulation.get("*"))
            if status is not None:
                raise simulated_error(status, model)
            kwargs = {}
            if simulation:
                # Failover demos must exercise a real provider call, so skip cache reads.
                kwargs["cache"] = {"no-cache": True}
            response = litellm.completion(
                model=model, messages=messages, api_key=api_key, num_retries=0, timeout=60, **kwargs
            )
            return response, key_id, failures
        except Exception as exc:  # rate limit, quota, 5xx, auth, network: try the next key
            failures += 1
            logger.warning("%s failed (%s): %s", key_id, type(exc).__name__, str(exc)[:200])
    raise HTTPException(status_code=503, detail="all providers unavailable")


# --------------------------------------------------------------------------- endpoints


@app.get("/health")
def health():
    try:
        with closing(connect()) as conn:
            conn.execute("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "keys_configured": len(get_keys()), "cache_enabled": litellm.cache is not None}


@app.post("/chat")
def chat(body: ChatRequest, x_simulate_failure: Optional[str] = Header(default=None)):
    started = time.perf_counter()
    simulation = parse_simulation(x_simulate_failure)
    model = get_model()

    with closing(connect()) as conn:
        row = conn.execute("SELECT system_prompt FROM sessions WHERE session_id = ?", (body.session_id,)).fetchone()
        stored_prompt = row["system_prompt"] if row else None
        history = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", (body.session_id,)
        ).fetchall()

    system_prompt = body.system_prompt or stored_prompt or DEFAULT_SYSTEM_PROMPT
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": r["role"], "content": r["content"]} for r in history]
    messages.append({"role": "user", "content": body.message})

    response, key_id, fallback_attempts = call_with_failover(model, messages, simulation)

    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        raise HTTPException(status_code=503, detail="all providers unavailable")

    cache_hit = bool(getattr(response, "_hidden_params", {}).get("cache_hit"))
    usage = response.usage
    metadata = {
        "resolution_source": "LLM_CACHE" if cache_hit else "LIVE_INFERENCE",
        "model": model,
        "latency_ms": 0,
        "input_tokens": int(usage.prompt_tokens or 0),
        "output_tokens": int(usage.completion_tokens or 0),
        "key_id": None if cache_hit else key_id,
        "fallback_attempts": 0 if cache_hit else fallback_attempts,
    }
    metadata["latency_ms"] = max(0, int((time.perf_counter() - started) * 1000))

    # Persist only after success, in one transaction (failed requests leave no trace).
    with closing(connect()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO sessions(session_id, system_prompt) VALUES(?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET system_prompt = excluded.system_prompt",
                (body.session_id, body.system_prompt or stored_prompt),
            )
            conn.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES(?, 'user', ?, NULL, ?)",
                (body.session_id, body.message, now_iso()),
            )
            conn.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES(?, 'assistant', ?, ?, ?)",
                (body.session_id, reply, json.dumps(metadata), now_iso()),
            )

    return {"session_id": body.session_id, "reply": reply, "metadata": metadata}


@app.get("/sessions/{session_id}/messages")
def session_messages(session_id: str):
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="session not found")
    messages = []
    for r in rows:
        item = {"role": r["role"], "content": r["content"]}
        if r["role"] == "assistant" and r["metadata"]:
            meta = json.loads(r["metadata"])
            item["metadata"] = {
                "resolution_source": meta.get("resolution_source"),
                "input_tokens": meta.get("input_tokens"),
                "output_tokens": meta.get("output_tokens"),
            }
        messages.append(item)
    return {"session_id": session_id, "messages": messages}
