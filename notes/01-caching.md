# Step 1: Caching and spend control (D35, touching D34, D5, D6, D36)

Status: **IMPLEMENTED, CHECKED** (2026-11-07). The decisions are in §9; sections 1–8 are the original ideation.

---

## 1. Why this step comes first

- **$10 is the whole budget.** The eval loop (D36) will re-run the 8 sample docs dozens of times
  while we change one stage at a time. Without a cache, every iteration pays again for every
  call, including the ones whose inputs did not change.
- **Cost discipline is graded** (15 pts, and the brief says outright that budget handling is judged).
  A ledger that shows where every cent went is also the evidence for DESIGN.md
  ("roughly how much of the $10 you spent").
- **Reproducibility.** LLM output varies even at temperature 0. A cache freezes the upstream
  stages, so when the score moves we know it was the change we made.
- **Free offline tests.** Recorded real responses become fixtures for integration tests (D38), with no spend and no network.

## 2. What "caching" covers here: three separate things

| Layer | What is cached | Saves | Key |
|---|---|---|---|
| **L1 LLM response cache** | The response to one model request | **Money** (the main point) | Hash of the canonical request |
| **L2 Artifact cache** | Expensive non-LLM work: OCR text, rendered page images, parsed XLSX/DOCX representation | **Time** (OCR on a 21-page scan is slow) | Hash of the file bytes + stage name + stage version + params |
| **L3 Spend ledger** (not a cache, but the same step) | An append-only record of every *real* network call and its cost | **Visibility and enforcement** | n/a |

Not in scope here: **job-level idempotency** (D6, the same doc + schema submitted twice returns the
existing job). That belongs to the service layer, though it can reuse the same hashing helper.

---

## 3. L1: the LLM response cache, in detail

### 3.1 Where it sits

All model traffic goes through **one** function (the chokepoint that D33 retries and D34 cost
accounting also need). The proposed order inside it:

```
call(request, tags)
  ├─ build canonical key
  ├─ cache lookup ──hit──► return cached response (+ mark hit, original cost)
  ├─ budget guard (refuse if over cap)
  ├─ rate limiter / concurrency semaphore
  ├─ retry loop (429 / 5xx / timeout, backoff + Retry-After)
  ├─ HTTP POST {LLM_BASE_URL}/chat/completions
  ├─ write ledger line (always, for every real attempt that returned)
  └─ cache store (only successful 2xx responses)
```

A cache hit skips the rate limiter and the budget guard, so replays are instant and free.

### 3.2 What goes into the key

**Include** everything that can change the output:
- `model`
- `messages`, canonicalised (stable key order, no whitespace). **Images are replaced by the
  sha256 of their bytes**, so the key stays small and the cache never stores image data.
- Generation params: `temperature`, `top_p`, `max_tokens`, `seed`, `response_format`, `tools`,
  `tool_choice`, `stop`, and any OpenRouter `provider` routing options.
- An optional **`sample_index`** (default 0), so that experiments sampling the same prompt N times
  (self-consistency, variance measurement) get N distinct entries.
- A global `CACHE_NAMESPACE` / schema version, as a manual "invalidate everything" lever.

**Exclude:** API key, `LLM_BASE_URL`, request-id headers, timestamps, our own tags.
(Swapping to their gateway with the same model id should not change the meaning of the key.)

Because the key is **content-addressed**, changing a prompt automatically misses. We never have to
version prompts by hand for L1. This is the main benefit over a cache keyed by "stage name".

### 3.3 What is stored

Raw response JSON, usage (tokens in/out), the cost of the original call, the model actually
served, latency of the original call, a created-at timestamp, and the key's readable prefix
(model + first ~200 chars of the last message) for debugging.

### 3.4 Subtleties to design for

1. **Bad output is cached too.** If a 200 response is malformed and we re-ask with the *identical*
   request, we get the same cached bad answer forever. Rules:
   - Repair re-asks should *change* the request (append the parse error, "return valid JSON"),
     so they get a new key naturally.
   - A plain re-ask must bump `attempt`/`sample_index` in the key.
   - Provide a way to evict one entry (or one stage) on demand.
2. **Failures are never cached:** 429, 5xx, timeouts, network errors.
3. **Non-determinism is hidden.** A cached run always looks the same. That is good for
   attributing score changes, but we must measure variance on purpose (via `sample_index`) before
   trusting a 1–2 point improvement.
4. **Upstream stability matters.** The hit rate depends on pipeline structure: if a stage's prompt
   embeds anything volatile (timestamps, random ordering, job ids), it never hits. That rules out
   volatile prompt content, which is a constraint to carry into group 5.
5. **Concurrent identical requests** (10 jobs with the same schema all asking for the
   schema-enrichment call at once) would each miss and each pay. Optional *in-flight dedupe*
   (single-flight): the first caller does the call and the others wait on its result.
6. **Provider drift.** OpenRouter can route one model id to different upstream providers, or a
   model may be updated. The cache pins the old behaviour. Acceptable in dev; a `refresh` mode handles it.
7. **Disk safety.** The cache contains text extracted from the sample documents, so it must stay
   **gitignored** (`.cache/` already is) and **out of the Docker image** (`.dockerignore`).

### 3.5 Modes (one env var, e.g. `LLM_CACHE_MODE`)

| Mode | Behaviour | Use |
|---|---|---|
| `off` | No lookup, no store | Debugging live behaviour; possibly the production default |
| `read_write` | Normal cache | Dev and eval loop |
| `read_only` / `replay` | Lookup only; **a miss raises an error** | Guaranteed-$0 runs; CI tests; "did my change touch the LLM inputs?" check |
| `refresh` | Skip lookup, overwrite on store | Re-sample after provider drift |

`replay` is the strongest budget tool. For example, after changing only post-processing code,
run in replay mode: if anything misses, you learn the change leaked into a prompt.

### 3.6 Storage backend options

| Option | Pros | Cons |
|---|---|---|
| **A. File per entry**, sharded `ab/cd/<hash>.json` | Dead simple, human-inspectable, atomic writes via temp + rename, easy targeted deletes, no dependency | Many small files; stats need a scan |
| **B. SQLite (WAL)** single file | One file, queryable (hit rates, cost by stage), handles concurrent workers, also holds the ledger table | Slightly more code; opaque without a tool |
| **C. Redis** | Natural if Redis is also the job queue (D4) | Dev cache needs a running server; volatile unless persisted |
| **D. Library**: LiteLLM cache, `diskcache`, `joblib.Memory` | Fastest to adopt | Less control over key and cost semantics. **LiteLLM especially needs auditing**: anything that phones home or calls a non-listed endpoint breaks the C6 disqualification rule |
| **E. HTTP-level record/replay** (vcrpy / hishel / respx style) | Transparent to code; great for tests | Matching POST bodies is fiddly; images bloat cassettes; harder to attach our cost and tag semantics |

### 3.7 Production question: does the cache stay on during their scoring run?

This is the biggest open question in this step.

| Scope in prod | Effect | Risk |
|---|---|---|
| **Off** | Simplest, clearly honest | Pays again for everything after a worker crash; pays again for repeated schema-level calls |
| **Per job only** (key namespaced by job / idempotency key) | A crashed and restarted worker **resumes without re-paying** for completed calls. That is free crash recovery for D5 | Very little; cache lifetime = job lifetime |
| **Shared across jobs, schema-level calls only** (type inference, description rewriting keyed on the schema hash) | Real cost saving on their measured run: many test docs share a schema | Need a clear rule for which calls are "schema-level" |
| **Fully shared** | Max savings on duplicate submissions | Cross-tenant sharing of document-derived responses (namespace by org if D8 is implemented); duplicates are better handled by D6 anyway |

Note: their cost is measured at their gateway, so a genuine cache hit genuinely lowers it. That is
legitimate production engineering, not gaming, and worth explaining in DESIGN.md either way.

---

## 4. L2: artifact cache (OCR, page images, parsed representations)

- Key = `sha256(file bytes)` + `stage` + **`stage_version`** (a manual constant bumped when that
  stage's code changes; unlike L1, code changes don't show up in the inputs) + params (DPI, OCR language, page range).
- Value: JSON for text and structure, PNG/WebP for page images.
- Same backend choice as L1, or simply files (images are large blobs; files suit them).
- In prod it doubles as **stage checkpointing**: a restarted job skips re-OCR. This links to D5.
- Cheap to build once L1's hashing and storage helpers exist.

---

## 5. L3: spend ledger and budget guard

### 5.1 Ledger
- **Append-only**, one record per real HTTP attempt that returned (including 4xx/5xx, which may still be billed).
- Fields: timestamp, run_id, doc_id, stage, model, served model, tokens in/out, cost, latency,
  status code, cache key, attempt number.
- **Kept separate from the cache**: the cache is disposable (we will wipe it), the ledger is the record of the budget and must never be wiped.
- Format options: JSONL file (greppable, trivially appendable) or a table in the SQLite file.

### 5.2 Where cost comes from
1. **The provider's own usage field**, preferred. OpenRouter can return the per-request cost in the
   response's usage block (to verify with one cheap call during implementation).
2. **A static price table** for the 9 models, as a fallback when the gateway doesn't return cost (their scoring gateway may not).
3. **Account-level check** (dev only): OpenRouter's key-usage endpoint, as ground truth to reconcile the ledger against. Never called from the service.

### 5.3 Budget guard
- A hard cap from env (e.g. `LLM_BUDGET_USD`), checked against the ledger total before each real call; raises instead of spending.
- An optional **per-run cap** for eval runs ("this iteration may spend at most $0.30").
- A **dry-run / estimate mode**: build every request, count tokens, price them from the table, call
  nothing. This lets us price a full sample run *before* paying for it.

### 5.4 Reporting into the output `metadata`
The contract says metadata reports "what your service did". On a cache hit, no model call happened. Proposal:
- `model_calls`, `tokens_*` and `cost_usd` count **real calls only**.
- Extra keys (metadata allows extra properties): `cache_hits`, and `cost_usd_uncached`, the cost
  had nothing been cached. The latter gives a true **$/doc estimate for the scoring run** even when
  every dev call was a hit, which DESIGN.md needs.

---

## 6. Tooling around it (small, but makes it usable)

- `cache stats`: entries, size, hit rate per stage and per model.
- `cache evict --stage X | --model Y | --older-than Z`.
- `ledger summary`: total, by run, by model, by stage, by doc. This feeds the PLAN.md budget table directly.
- Every eval run prints: hits / misses / $ spent this run / $ total / $ remaining.

---

## 7. Proposed approach (for revision, not decided)

1. **L1 is custom code** at the single LLM chokepoint, content-addressed with images hashed, and has the four modes.
2. **Storage: SQLite (WAL)** for the L1 entries and the ledger table, since one file is queryable and
   safe with concurrent workers. *Alternative:* files for L1 and JSONL for the ledger. Simpler, equally valid.
3. **L2 as files** under `.cache/artifacts/`, keyed by file hash + stage + version.
4. **Ledger separate from the cache, append-only**, with a hard global cap and a per-run cap.
5. **Production:** cache on, **scoped per job** (crash-resume), plus **shared schema-level calls**. Fully shared stays off.
6. **Metadata:** real-call numbers in the required keys; `cache_hits` and `cost_usd_uncached` as extras.
7. **In-flight dedupe:** design for it now, build it later (it matters under the 10-in-flight load, less in dev).

## 8. Open questions for revision

1. **Storage:** SQLite vs files + JSONL?
2. **Prod scope:** off / per-job / per-job + schema-level / fully shared?
3. **Metadata on hits:** report real calls only (proposed), or uncached numbers in the required keys?
4. **Budget caps:** what global hard cap? (e.g. stop at $9 to keep a $1 reserve for the final full
   run.) What default per-run cap?
5. **Commit the ledger?** It holds no document content, only doc ids, models and costs, and it is strong
   evidence of budget discipline. Or keep it local and commit only a summary?
6. **Build vs library:** custom (proposed) or adopt LiteLLM / diskcache after an audit?
7. **Blocking dependency: D2 (language).** Implementation of this step is the first code in the repo,
   so the language must be chosen during revision.

---

## 9. Revision outcome (the user delegated the choices) and implementation

| Question | Decision | Why |
|---|---|---|
| Language (D2) | **Python 3.11+** | Best ecosystem for PDF, OCR, DOCX and XLSX; the validator is Python |
| Storage | **SQLite (WAL)** for L1; **JSONL** for the ledger; **files** for L2 | SQLite is queryable and safe for concurrent workers; the ledger stays a separate append-only file so wiping the cache never loses budget history; images suit plain files |
| Prod scope | **Cache on (read_write)**; `namespace` param available for tenant isolation of document-level calls | Gives crash-resume for free and reuses schema-level calls across jobs; a genuine hit genuinely lowers measured cost |
| Metadata on hits | Required keys = **real calls only**; extras `cache_hits`, `cost_usd_uncached`, per-model breakdown | Honest against the contract; still gives a true $/doc estimate from cached dev runs |
| Budget caps | Dev `.env`: **`LLM_BUDGET_USD=9.00`** (keeps a $1 reserve); per-run cap optional; **no cap when unset** (prod default) | A cap must never block the graders' scoring run on their key |
| Commit the ledger | **Yes** (`ledger/spend.jsonl`) | No document content; the ledger is evidence of budget discipline |
| Build vs library | **Custom** (~400 lines) | Full control over the key and cost semantics; no third-party code that could call non-listed endpoints |
| Single-flight | **Implemented now** (in-process) | ~20 lines; matters for 10 jobs in flight sharing schema-level calls |

### Files
- `app/hashing.py`: canonical JSON and sha256 (reused later for idempotency)
- `app/llm/models.py`: allowed-model list and guard
- `app/llm/keys.py`: content-addressed request keys, inline blobs hashed
- `app/llm/cache.py`: `ResponseCache` (SQLite) and `CacheMode`
- `app/llm/ledger.py`: `SpendLedger` (JSONL) and `BudgetGuard`
- `app/llm/pricing.py` + `pricing.json`: provider cost first, price-table fallback
- `app/llm/usage.py`: `UsageTracker` → result `metadata`
- `app/llm/client.py`: `LLMClient.chat`, the single chokepoint
- `app/artifacts.py`: L2 `ArtifactCache`
- CLIs: `python -m app.cli.cache stats|evict`, `python -m app.cli.spend`, `python -m app.cli.fetch_pricing`, `python -m app.cli.smoke_llm`
- Tests: `tests/test_llm_cache.py`, `tests/test_artifacts.py`

### Check
- `pytest`: **30 passed** (keys, all 4 modes, errors never cached, 200-with-error-body, budget
  guard total/run/hits-bypass, disallowed model, single-flight success and failure, price-table
  fallback, stats/evict, concurrent ledger appends, half-written ledger line, artifact cache).
- Price table generated for all 9 models from the public endpoint ($0).
- **Pending, needs the API key in `.env`:** `python -m app.cli.smoke_llm`, one real call of
  under $0.0001. It confirms the key works, whether `usage.cost` comes back, and a real cache hit.

### Deferred to later steps (by design)
- Retry/backoff around `_send` (D33): the client raises typed, `retryable` errors ready for it.
- Pre-flight dry-run cost estimate: `estimate_cost()` exists; the token counting comes with the prompts.

### Carried forward as constraints
- **Prompts must not contain volatile content** (timestamps, job ids, random order), or they never hit the cache.
- **Provider-side prompt caching is a cost lever for group 5:** `input_cache_read` is ~10× cheaper than
  fresh input on most listed models, so stable shared prompt prefixes (schema and instructions first, document after) pay off.
- Pricing snapshot (USD per M tokens, in/out): luna and haiku 0.10/0.50, glm-flash 0.15/0.50, qwen-vl 0.21/1.90,
  deepseek 0.26/0.38, gemini-flash 0.825/4.125, sonnet and sol 2/10, gemini-pro 2/12.
