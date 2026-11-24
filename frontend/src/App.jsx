import { useEffect, useRef, useState } from "react";

const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

function newSessionId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return "s-" + crypto.randomUUID();
  return "s-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

function describeError(status, body) {
  if (status === 422) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    return "Invalid input: " + (detail || "please check your message and try again.");
  }
  if (status === 503) {
    return "All AI providers are currently unavailable. Please try again in a moment.";
  }
  return "Something went wrong (HTTP " + status + "). Please try again.";
}

export default function App() {
  const sessionId = useRef(newSessionId()); // new session on every page load
  const [items, setItems] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, loading]);

  async function send() {
    const message = input.trim();
    if (!message || loading) return;
    setInput("");
    setItems((prev) => [...prev, { type: "user", text: message }]);
    setLoading(true);
    try {
      const payload = { session_id: sessionId.current, message };
      if (systemPrompt.trim()) payload.system_prompt = systemPrompt.trim();
      const res = await fetch(API_URL + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      let body = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      if (res.ok && body && body.reply !== undefined) {
        setItems((prev) => [...prev, { type: "assistant", text: body.reply, meta: body.metadata }]);
      } else {
        setItems((prev) => [...prev, { type: "error", text: describeError(res.status, body) }]);
      }
    } catch {
      setItems((prev) => [
        ...prev,
        { type: "error", text: "Could not reach the server. Check your connection and try again." },
      ]);
    } finally {
      setLoading(false);
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="app">
      <h1>LLM Chatbot</h1>

      <input
        className="system"
        data-testid="system-prompt-input"
        placeholder="System prompt (optional)"
        value={systemPrompt}
        onChange={(e) => setSystemPrompt(e.target.value)}
      />

      <div className="chat">
        {items.length === 0 && <p className="empty">Send a message to start the conversation.</p>}
        {items.map((it, i) =>
          it.type === "assistant" ? (
            <div key={i} className="msg assistant" data-testid="assistant-message">
              <div className="text">{it.text}</div>
              <div className="meta">
                <span data-testid="meta-source">{it.meta.resolution_source}</span>
                <span data-testid="meta-model">{it.meta.model}</span>
                <span data-testid="meta-latency">{it.meta.latency_ms} ms</span>
                <span>
                  in: <span data-testid="meta-input-tokens">{it.meta.input_tokens}</span>
                </span>
                <span>
                  out: <span data-testid="meta-output-tokens">{it.meta.output_tokens}</span>
                </span>
              </div>
            </div>
          ) : (
            <div key={i} className={"msg " + it.type}>
              <div className="text">{it.text}</div>
            </div>
          )
        )}
        {loading && <div className="msg assistant thinking">Thinking…</div>}
        <div ref={endRef} />
      </div>

      <div className="composer">
        <textarea
          data-testid="chat-input"
          rows={2}
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button data-testid="send-button" onClick={send} disabled={loading}>
          Send
        </button>
      </div>
    </div>
  );
}
