import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient, type AskResponse, type Me, isLoopback } from "./api";
import { Clarification } from "./Clarification";
import { ResultTable } from "./ResultTable";

interface Turn {
  question: string;
  response: AskResponse | null;
  error: string | null;
}

const TOKEN_KEY = "peoplelens.localToken";

function readToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function writeToken(v: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, v);
  } catch {
    // storage unavailable: the token lives only in memory
  }
}

const EXAMPLES = ["Headcount by location", "Turnover by quarter in 2024", "Offer acceptance rate last year", "Average salary by job level"];

export function App() {
  const local = isLoopback(window.location.hostname);
  const [token, setToken] = useState<string | null>(local ? readToken() : null);
  const client = useMemo(() => new ApiClient(local ? token : null), [local, token]);
  const [me, setMe] = useState<Me | null>(null);
  const [session, setSession] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setStatus(null);
    client
      .me()
      .then(async (m) => {
        if (cancelled) return;
        setMe(m);
        setSession(await client.session());
      })
      .catch((e: unknown) => {
        if (!cancelled) setStatus(e instanceof Error ? e.message : "not signed in");
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  const send = useCallback(
    async (question: string, choose?: string) => {
      if (!session) return;
      setBusy(true);
      setTurns((t) => [...t, { question, response: null, error: null }]);
      try {
        const res = choose ? await client.choose(session, choose) : await client.ask(session, question);
        setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, response: res } : x)));
      } catch (e) {
        const message = e instanceof Error ? e.message : "request failed";
        setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error: message } : x)));
      } finally {
        setBusy(false);
      }
    },
    [client, session],
  );

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    void send(q);
  };

  return (
    <main>
      <header>
        <h1>PeopleLens</h1>
        <p className="sub">
          Governed workforce analytics for Lumen Works (synthetic data).
          {me && (
            <>
              {" "}
              Signed in as <strong>{me.role.replaceAll("_", " ")}</strong>
              {me.scope.departments !== "all" && <> for {me.scope.departments.join(" and ")}</>}.
            </>
          )}
        </p>
      </header>

      {local && (
        <details className="token" open={!me}>
          <summary>Local identity</summary>
          <p>
            Local only. Paste a token from <code>uv run pl token --role executive</code>.
          </p>
          <input
            aria-label="Local token"
            value={token ?? ""}
            onChange={(e) => {
              setToken(e.target.value.trim() || null);
              writeToken(e.target.value.trim());
            }}
            placeholder="eyJ..."
          />
        </details>
      )}

      {status && <p className="error">{status}</p>}

      <section className="turns" aria-live="polite">
        {turns.length === 0 && me && (
          <div className="examples">
            {EXAMPLES.map((q) => (
              <button key={q} type="button" onClick={() => void send(q)} disabled={busy || !session}>
                {q}
              </button>
            ))}
          </div>
        )}
        {turns.map((t, i) => (
          <article key={i} className="turn">
            <p className="question">{t.question}</p>
            {t.error && <p className="error">{t.error}</p>}
            {!t.response && !t.error && <p className="pending">Working...</p>}
            {t.response?.type === "answer" && t.response.answer && (
              <ResultTable answer={t.response.answer} grain={t.response.answer.intent.grain} />
            )}
            {t.response?.type === "clarification" && t.response.clarification && (
              <Clarification
                question={t.response.clarification.question}
                options={t.response.clarification.options}
                disabled={busy || i !== turns.length - 1}
                onChoose={(id) => {
                  const label = t.response?.clarification?.options.find((o) => o.id === id)?.label ?? id;
                  void send(label, id);
                }}
              />
            )}
            {t.response?.type === "refusal" && t.response.refusal && (
              <p className="refusal">
                {t.response.refusal.message}
                {t.response.refusal.alternatives.length > 0 && <> Try: {t.response.refusal.alternatives.join(", ")}.</>}
              </p>
            )}
            {t.response && (
              <p className="meta">
                {t.response.meta.latency_ms} ms; {t.response.meta.llm_provider}; {t.response.meta.warehouse}
                {t.response.meta.catalog_key ? `; ${t.response.meta.catalog_key}` : ""}
              </p>
            )}
          </article>
        ))}
      </section>

      <form onSubmit={onSubmit}>
        <input
          aria-label="Question"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={me ? "Ask about headcount, FTE, turnover, offers, time to fill or pay" : "Sign in first"}
          disabled={!session || busy}
          maxLength={500}
        />
        <button type="submit" disabled={!session || busy || input.trim() === ""}>
          Ask
        </button>
      </form>
    </main>
  );
}
