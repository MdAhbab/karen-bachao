# GridWise — Smart Campus Energy Optimization

LLM-assisted operator directive interpretation and 24-hour energy scheduling.

A single HTTP service that reads natural-language campus operator notes, converts
them into structured directives with a language model, validates those directives
deterministically, and returns the cheapest 24-hour schedule that satisfies every
directive plus the underlying energy, battery and grid rules.

| | |
| :--- | :--- |
| **Health endpoint** | `GET /health` → `{"status":"ok"}` |
| **Main endpoint** | `POST /optimize-energy` |
| **Operator console** | `GET /ui/` (served by the same process) |
| **Live endpoint** | https://buptestapi.ahbab.dev |
| **LLM providers** | Google Gemini and Groq (raced, separate quotas) |
| **Optimizer** | SciPy `linprog` with the HiGHS backend |

---

## 1. Quickstart (clean machine)

Requires **Python 3.10+** and **git**. Nothing else.

```bash
git clone https://github.com/MdAhbab/karen-bachao.git
cd karen-bachao
cp .env.example .env
```

Open `.env` and set at least one provider key. Setting both is better: they sit
on separate quotas, so one running out does not stop interpretation.

```
GEMINI_API_KEY=your_gemini_key
GROQ_API_KEY=your_groq_key
```

Then start everything with one command:

```bash
python run.py
```

`run.py` installs dependencies, stops anything already listening on the port,
starts the service, and waits until `/health` answers. It prints:

```
  GridWise is ready.
    Console : http://localhost:8000/ui/
    Health  : http://localhost:8000/health
    API     : POST http://localhost:8000/optimize-energy
```

Useful flags:

```bash
python run.py --no-install      # skip pip install
python run.py --port 9000       # different port
python run.py --stop            # stop a running instance
```

---

## 2. Verify it works

**Health check:**

```bash
curl http://localhost:8000/health
```

```json
{"status":"ok"}
```

**A real scenario:**

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "GRID-101",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour":0,"demand_kwh":180,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":1,"demand_kwh":170,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":2,"demand_kwh":165,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":3,"demand_kwh":160,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":4,"demand_kwh":160,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":5,"demand_kwh":165,"solar_kwh":5,"tariff_bdt_per_kwh":7},
      {"hour":6,"demand_kwh":175,"solar_kwh":20,"tariff_bdt_per_kwh":8},
      {"hour":7,"demand_kwh":190,"solar_kwh":45,"tariff_bdt_per_kwh":8},
      {"hour":8,"demand_kwh":200,"solar_kwh":70,"tariff_bdt_per_kwh":9},
      {"hour":9,"demand_kwh":210,"solar_kwh":95,"tariff_bdt_per_kwh":9},
      {"hour":10,"demand_kwh":215,"solar_kwh":110,"tariff_bdt_per_kwh":9},
      {"hour":11,"demand_kwh":220,"solar_kwh":120,"tariff_bdt_per_kwh":9},
      {"hour":12,"demand_kwh":220,"solar_kwh":125,"tariff_bdt_per_kwh":9},
      {"hour":13,"demand_kwh":215,"solar_kwh":120,"tariff_bdt_per_kwh":9},
      {"hour":14,"demand_kwh":210,"solar_kwh":110,"tariff_bdt_per_kwh":9},
      {"hour":15,"demand_kwh":205,"solar_kwh":90,"tariff_bdt_per_kwh":9},
      {"hour":16,"demand_kwh":200,"solar_kwh":65,"tariff_bdt_per_kwh":10},
      {"hour":17,"demand_kwh":205,"solar_kwh":35,"tariff_bdt_per_kwh":11},
      {"hour":18,"demand_kwh":215,"solar_kwh":10,"tariff_bdt_per_kwh":13},
      {"hour":19,"demand_kwh":220,"solar_kwh":0,"tariff_bdt_per_kwh":13},
      {"hour":20,"demand_kwh":215,"solar_kwh":0,"tariff_bdt_per_kwh":13},
      {"hour":21,"demand_kwh":205,"solar_kwh":0,"tariff_bdt_per_kwh":12},
      {"hour":22,"demand_kwh":195,"solar_kwh":0,"tariff_bdt_per_kwh":9},
      {"hour":23,"demand_kwh":200,"solar_kwh":0,"tariff_bdt_per_kwh":9}
    ],
    "battery": {
      "capacity_kwh": 500,
      "initial_energy_kwh": 200,
      "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100,
      "max_discharge_kwh_per_hour": 100
    }
  }'
```

Abbreviated response:

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "Usable solar is reduced to 20% during the stated window."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's energy schedule."
    }
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 80.0, "solar_used_kwh": 0.0,
     "battery_action": "discharge", "battery_kwh": 100.0,
     "battery_energy_after_kwh": 100.0},
    "... 23 more hourly entries ..."
  ],
  "total_grid_kwh": 3879.0,
  "total_cost_bdt": 34071.0,
  "peak_grid_kwh": 300.0,
  "plan_summary": "Charged the battery in 9 low-tariff hour(s) and discharged across 11 higher..."
}
```

### Run the public sample pack

The organizer's 10 public cases ship in `tests/public_cases.json`.

```bash
# offline: optimizer, guardrails and the fallback parser (no service needed)
python -m pytest tests -q

# live: every public case + 15 synthetic scenarios against a running service
python tests/test_api.py --base-url http://localhost:8000
```

Expected result on a healthy run:

```
  cases run        : 25
  failures         : 0
  directive match  : 37/37  (100%)
  optimization     : 1.0000 average quality ratio
  latency          : p95 5.48s   max 5.88s
```

That p95 is a deliberate worst case: 25 requests fired back to back with no gap,
which trips per-model rate limits and forces escalation to later tiers. Sent at
a realistic pace the p95 is about 4.2s, and a repeated note is served from cache
in roughly 0.01s.

The live suite replays every returned plan against the energy-balance, battery,
solar and directive rules, compares cost to the organizer reference, checks the
error contract, and reports p95 latency.

---

## 3. Architecture

```
POST /optimize-energy
        │
        ├─► schemas.py      Pydantic validation ────────► 400 / 422 on bad input
        │
        ├─► llm.py          Gemini: all models raced concurrently,
        │       │           first valid answer wins, cached by note text
        │       │           (this is the mandatory LLM interpretation path)
        │       └─ provider fully down? ─► fallback.py  deterministic parser
        │
        ├─► guardrails.py   LLM output is untrusted: one entry per note in
        │                   note_index order, allowed types only, hours unique
        │                   and ascending in 0..23, 0 ≤ factor ≤ 1, reserve
        │                   ≤ capacity, applies semantics forced
        │
        ├─► optimizer.py    Linear program, solved with HiGHS
        │
        └─► replay.py       Final replay: the finished plan is re-checked
                            against every directive and every energy rule
                                    │
                                    └─► 200 JSON response
```

### The LLM's role

The language model is what actually reads the operator notes. It receives all
notes for a scenario in one call, along with the battery capacity (needed to
resolve percentage-based reserves such as *"keep at least 50% of capacity"*),
and returns a structured directive per note. Its output is **never** trusted
directly — it passes through `guardrails.py` before it can influence the
optimizer, and the finished schedule is replayed afterwards.

**Two providers, twelve models.** Google Gemini and Groq bill against entirely
separate quotas, so one provider being rate limited or exhausted does not stop
interpretation:

| Provider | Models (`GEMINI_MODELS` / `GROQ_MODELS`) |
| :--- | :--- |
| Gemini | `gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3-flash-preview`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3.1-pro-preview` |
| Groq | `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `groq/compound-mini` |

Models are **raced in tiers**, not tried one at a time. The list is interleaved
across providers and split into groups of `LLM_RACE_SIZE` (default 3), so the
first tier always spans both providers. That tier runs concurrently and the
first valid answer wins; only if a whole tier fails does the next one run.
Racing beats a fixed order because the failure modes are uneven (some models
answer in under a second, some return `503` instantly, some hang), and tiering
keeps a normal request to three calls instead of twelve, which matters against
per-model rate limits.

Interpretations are cached by note text, so a repeated note costs nothing.

Only chat-completion models capable of structured extraction are listed. The
Groq account also exposes `whisper-large-v3` (speech to text), the `orpheus`
voices (text to speech) and `llama-prompt-guard` (a classifier); none of these
can perform this task, so they are deliberately excluded.

### Guardrails

Every directive is rebuilt into a legal shape rather than accepted as given:

- exactly one entry per note, in `note_index` order (a missing note becomes `no_op`)
- `directive_type` must be one of the six supported values, otherwise `no_op`
- `hours` coerced to unique integers 0–23 in ascending order; empty means `no_op`
- `solar_reduction.factor` must be in `[0, 1]`; a percentage-style `80` is
  normalised once to `0.8`, and anything still out of range is rejected
- `minimum_energy_kwh` finite, non-negative, clamped to battery capacity
- `max_grid_kwh` finite and non-negative
- `applies` is forced: `false` with a null adjustment for `no_op`, `true` for
  everything else

### The optimizer

The scheduling problem is a **pure continuous linear program**. `battery_action`
looks categorical, but the specification defines no round-trip efficiency loss,
so charging and discharging in the same hour is cost-neutral and nets out during
post-processing. No integer variables are needed, which means the LP returns the
**provably globally optimal** cost rather than a heuristic approximation.

96 variables — `grid[h]`, `solar_used[h]`, `charge[h]`, `discharge[h]`:

```
minimize   Σ grid[h] · tariff[h]

subject to grid[h] + solar_used[h] + discharge[h] − charge[h] = demand[h]
           Σ charge − Σ discharge = 0                      (end-of-day neutrality)
           floor[h] ≤ initial + Σ(charge−discharge) ≤ capacity
           0 ≤ grid[h]       ≤ max_grid_window cap
           0 ≤ solar_used[h] ≤ solar[h] × solar_reduction factor
           0 ≤ charge[h]     ≤ 0 inside a no_charge_window
           0 ≤ discharge[h]  ≤ 0 inside a no_discharge_window
```

`grid_kwh` is recomputed last from the balance equation and battery state is
accumulated hour by hour, so the returned plan satisfies the energy balance and
end-of-day neutrality *exactly* rather than approximately. Reported totals are
summed from the final `hourly_plan`, so they can never disagree with it.

If a directive combination were infeasible, the solver soft-drops the reserve
directives first and then all interpreted directives, so the service always
returns a valid schedule instead of a 500.

**Why this solver.** Five options were compared:

| Solver | Class | Solve time | Verdict |
| :--- | :--- | :--- | :--- |
| **SciPy `linprog` (HiGHS)** | LP simplex / IPM | **~3 ms** | **chosen** — globally optimal, one dependency |
| `highspy` | same HiGHS engine | ~1 ms | identical result, clunkier API |
| PuLP + CBC | MILP | 50–200 ms | slower, MILP machinery not needed |
| OR-Tools GLOP / CP-SAT | LP / CP | 5–20 ms | ~100 MB wheel, bloats the image |
| CVXPY (ECOS/OSQP) | conic modeling | 20–100 ms | per-solve compile overhead |

Against the ten public cases the LP reproduces the organizer's reference optimum
to **0.00 BDT on all ten**, averaging 3.4 ms per scenario.

---

## 4. API contract

### `GET /health`

Returns `200` with `{"status":"ok"}`. Does no LLM work, so it is always fast.

### `POST /optimize-energy`

**Request** — `scenario_id` (string), `operator_notes` (1–3 non-empty strings),
`hours` (exactly 24 entries with `hour`, `demand_kwh`, `solar_kwh`,
`tariff_bdt_per_kwh`), and `battery` (`capacity_kwh`, `initial_energy_kwh`,
`minimum_energy_kwh`, `max_charge_kwh_per_hour`, `max_discharge_kwh_per_hour`).

**Response** — `scenario_id`, `directive_interpretation`, `hourly_plan`,
`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.

**Supported directive types**

| Type | `structured_adjustment` |
| :--- | :--- |
| `solar_reduction` | `{"hours": [...], "factor": number}` |
| `minimum_battery_reserve` | `{"hours": [...], "minimum_energy_kwh": number}` |
| `no_charge_window` | `{"hours": [...]}` |
| `no_discharge_window` | `{"hours": [...]}` |
| `max_grid_window` | `{"hours": [...], "max_grid_kwh": number}` |
| `no_op` | `null` |

Time windows are start-inclusive and end-exclusive: 1 PM to 3 PM is `[13, 14]`.
For `solar_reduction`, `factor` is the fraction of solar that **remains**, so an
80% reduction is `factor = 0.2`.

**Status codes**

| Code | Meaning |
| :--- | :--- |
| `200` | Success |
| `400` | Malformed JSON |
| `422` | Well-formed but schema-invalid |
| `500` | Controlled internal error — `{"error": "internal error"}`, no stack trace |

---

## 5. Operator console

`http://localhost:8000/ui/` — plain HTML, CSS and JavaScript, no build step.

- load any of the 10 public samples, **upload a `.json` file**, or paste JSON
- the scenario is **validated in the browser before anything is sent**: note
  count, all 24 hours present and unique, required numeric fields, battery
  consistency. Problems are listed and the request is blocked.
- for each operator note it shows the directive type, whether it applies, the
  numeric value, and **the affected hours** — both as a list (`[18, 19, 20]`)
  and as a 24-cell strip with those hours highlighted
- the full 24-hour plan, the totals, and the raw response JSON

---

## 6. Configuration

All configuration is environment variables, loaded from `.env` (never committed).

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(none)* | Google Gemini API key. |
| `GEMINI_MODELS` | the eight models above | Comma-separated Gemini model IDs. |
| `GROQ_API_KEY` | *(none)* | Groq API key. |
| `GROQ_MODELS` | the four models above | Comma-separated Groq model IDs. |
| `LLM_RACE_SIZE` | `3` | How many models to race concurrently per tier. |
| `LLM_TIMEOUT_S` | `10` | Per-model request timeout. |
| `LLM_TOTAL_BUDGET_S` | `7` | Total interpretation budget, well under the 30s limit. |
| `PORT` | `8000` | Port the API binds to. |

---

## 7. Deployment

### Docker (fallback image)

```bash
docker build -t gridwise-api:1.0.0 .
docker run -d -p 8000:8000 -e GEMINI_API_KEY=your_key gridwise-api:1.0.0
curl http://localhost:8000/health
```

The image binds `0.0.0.0:8000`, runs as an unprivileged user, and contains **no
baked-in credentials** — the key is supplied at runtime.

### Live deployment

The service runs at **https://buptestapi.ahbab.dev**:

```bash
curl https://buptestapi.ahbab.dev/health
# {"status":"ok"}
```

It shares a VM with unrelated sites, so it does **not** run its own nginx.
The API container binds loopback only and the host nginx proxies to it:

```bash
docker run -d --name gridwise-api --restart unless-stopped   -p 127.0.0.1:8200:8000 --env-file .env --memory 512m gridwise-api:1.0.0
```

`/etc/nginx/sites-available/buptestapi.conf` proxies `buptestapi.ahbab.dev`
to `127.0.0.1:8200`, with TLS issued by the host's certbot. Nothing binds
ports 80, 443 or 8000, so co-hosted sites are untouched.

> Use `run_onVM.py` only on a **dedicated** VM. It frees ports 80, 443 and
> 8000 before starting, which would stop anything already serving on them.

### Full VM deployment (Docker + nginx + HTTPS)

Run on an Ubuntu VM, from the repository root:

```bash
sudo python3 run_onVM.py --domain BUPtestAPI.ahbab.dev --email you@example.com
```

This verifies Docker, checks that the domain resolves to the VM, stops any
previous stack, frees ports 80/443/8000, builds and starts `api` + `nginx`,
obtains a Let's Encrypt certificate, switches nginx to HTTPS, and waits for
`/health`.

Both containers use `restart: unless-stopped`, so they come back after a crash
or reboot. For the case where the process is alive but unhealthy:

```bash
sudo python3 run_onVM.py --watchdog --domain BUPtestAPI.ahbab.dev
```

Other modes: `--skip-tls`, `--staging`, `--renew`, `--stop`, `--no-build`.

> **DNS note.** A CNAME can only point at a hostname. If the VM has only a bare
> IP address, create an **A record** for `BUPtestAPI.ahbab.dev` pointing at it.
> DNS must resolve to the VM *before* running the script, or certificate
> issuance will fail. If the record sits behind a proxy, disable the proxy until
> the certificate is issued.

---

## 8. Project layout

```
app/
  main.py        FastAPI app, endpoints, error handlers, static console
  config.py      environment loading
  schemas.py     request and response models
  prompt.py      interpretation prompt and response schema
  llm.py         Gemini client: racing, caching, cascade
  guardrails.py  deterministic validation of model output
  fallback.py    deterministic parser (provider-outage net)
  optimizer.py   the linear program
  replay.py      final validator
web/             operator console (plain HTML/CSS/JS)
tests/
  public_cases.json  organizer sample pack
  scenarios.json     15 synthetic paraphrase and boundary scenarios
  test_optimizer.py  LP correctness vs reference optima
  test_guardrails.py malformed model output handling
  test_fallback.py   parser accuracy on all cases
  test_api.py        live end-to-end verification
nginx/           reverse proxy configs (HTTP bootstrap and TLS)
Dockerfile       fallback image
docker-compose.yml
run.py           local one-command runner
run_onVM.py      VM deployment with nginx, certbot and watchdog
```

---

## 9. Dependencies

| Package | Use |
| :--- | :--- |
| [FastAPI](https://fastapi.tiangolo.com/) | HTTP framework |
| [Uvicorn](https://www.uvicorn.org/) | ASGI server |
| [Pydantic](https://docs.pydantic.dev/) | Request and response validation |
| [SciPy](https://scipy.org/) | `linprog` with the HiGHS solver |
| [httpx](https://www.python-httpx.org/) | Async HTTP client for the Gemini API |
| [pytest](https://pytest.org/) | Test suite |
| [Google Gemini API](https://ai.google.dev/) | Operator-note interpretation |
| [Groq API](https://groq.com/) | Operator-note interpretation, second provider |
| [nginx](https://nginx.org/), [Docker](https://www.docker.com/), [certbot](https://certbot.eff.org/) | Deployment |

---

## 10. Secret handling

- `.env` is listed in `.gitignore` and is **not** committed. Only `.env.example`
  is, and it contains variable names with no values.
- No key is baked into the Docker image; it is passed at runtime via `env_file`
  or `-e`.
- API keys are never logged. Model failures are logged by exception type only.
- The `500` handler returns a flat `{"error": "internal error"}` — no stack
  trace, no configuration, no secret. The live test suite asserts this.

---

## 11. Known limitations

- **Free-tier rate limits.** During development the Gemini free tier frequently
  returned `503 high demand` and `429 quota exceeded`. Running Groq as a second
  provider on a separate quota, plus tiered racing and caching, exists
  specifically to work around this. Under a hard burst (25 requests with no gap
  at all) some requests still escalate through tiers and approach the 7s budget.
  Enabling billing on either key would remove this entirely.
- **Deterministic fallback.** If all five models fail, a regex-based parser
  answers instead of returning an error, so the service stays available during a
  provider outage. This is an availability net, not the primary path — the LLM
  is what interprets notes in normal operation. It currently reproduces the
  ground truth on all 10 public cases and all 15 synthetic scenarios, but it
  will be less robust than the model on unseen phrasing.
- **The Docker image has not been built locally** — Docker was unavailable on the
  development machine. The Dockerfile and compose stack are written against a
  standard `python:3.12-slim` base and are built by `run_onVM.py` on the VM.
- **Latency depends on the provider.** Optimization itself takes ~3 ms; nearly
  all response time is the model call. A cache hit returns in milliseconds.
- Grid export and battery round-trip efficiency losses are out of scope, as
  specified.
