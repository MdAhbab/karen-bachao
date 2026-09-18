"""GridWise API service.

Endpoints required by the judge harness:
  GET  /health            readiness probe
  POST /optimize-energy   LLM interpretation + 24-hour optimization

The operator console is served from /ui by the same process, so there is a
single deployable service.
"""

import json
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import llm
from app.config import REPO_ROOT
from app.optimizer import optimize, summarize
from app.replay import replay, totals
from app.schemas import OptimizeRequest, OptimizeResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gridwise")

app = FastAPI(
    title="GridWise Energy Optimizer",
    description="LLM-assisted operator directive interpretation and 24-hour "
                "energy scheduling.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = REPO_ROOT / "web"


# --------------------------------------------------------------------------
# Error handling: controlled responses, never a stack trace or a secret.
# --------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    """Malformed JSON is 400; well-formed but invalid is 422 (section 6.1)."""
    malformed = any(e.get("type") == "json_invalid" for e in exc.errors())
    status = 400 if malformed else 422
    return JSONResponse(
        status_code=status,
        content={
            "error": "invalid request",
            "detail": [
                {"field": ".".join(str(p) for p in e.get("loc", [])),
                 "message": e.get("msg", "")}
                for e in exc.errors()[:10]
            ],
        },
    )


@app.exception_handler(Exception)
async def _on_unhandled_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal error"})


# --------------------------------------------------------------------------
# Judge endpoints
# --------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Readiness probe. Deliberately does no LLM work so it is always fast."""
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(payload: OptimizeRequest):
    # 1. LLM interprets every operator note (mandatory path, section 04).
    directives, source = await llm.interpret(payload.operator_notes, payload.battery)

    # 2. Optimize against the guardrailed directives.
    plan, meta = optimize(payload, directives)

    # 3. Final replay (section 08): verify the plan honours every directive.
    violations = replay(payload, directives, plan)
    if violations:
        log.error("replay rejected the plan for %s: %s", payload.scenario_id,
                  violations[:5])
        plan, meta = optimize(payload, [])
        violations = replay(payload, [], plan)
        if violations:
            log.error("base plan also invalid for %s", payload.scenario_id)

    computed = totals(payload, plan)
    log.info(
        "scenario=%s source=%s dropped=%s cost=%.2f",
        payload.scenario_id, source, meta.get("dropped_directives"),
        computed["total_cost_bdt"],
    )

    return {
        "scenario_id": payload.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": plan,
        "total_grid_kwh": computed["total_grid_kwh"],
        "total_cost_bdt": computed["total_cost_bdt"],
        "peak_grid_kwh": computed["peak_grid_kwh"],
        "plan_summary": summarize(plan, directives),
    }


# --------------------------------------------------------------------------
# Operator console (same service, so there is one deployment)
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ui/")


@app.get("/samples", include_in_schema=False)
async def samples():
    """Public sample scenarios, so the console can load them in one click."""
    path = REPO_ROOT / "tests" / "public_cases.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [{"id": c["id"], "label": c.get("label", ""), "input": c["input"]}
            for c in data.get("cases", [])]


if WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
