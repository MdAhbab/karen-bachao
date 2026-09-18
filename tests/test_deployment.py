"""Deployment and documentation checks (rubric categories 6 and 7).

Docker itself is not available in every dev environment, so these validate the
artifacts statically: the image must bind correctly and carry no secrets, the
compose stack must restart itself, nginx must proxy and serve ACME, and the
README must actually document the commands and variable names it claims to.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def read(name):
    path = ROOT / name
    assert path.is_file(), f"{name} is missing"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Secrets must never reach the repository or the image
# --------------------------------------------------------------------------
SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{40,}"),      # Groq
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),   # Google classic
    re.compile(r"AQ\.[A-Za-z0-9_\-]{30,}"),   # Google newer
    re.compile(r"sk-[A-Za-z0-9]{32,}"),       # OpenAI style
]

TRACKED = [p for p in ROOT.rglob("*")
           if p.is_file()
           and ".git" not in p.parts
           and "__pycache__" not in p.parts
           and "certbot" not in p.parts
           and p.name != ".env"
           and p.suffix not in {".pyc", ".log"}]


def test_no_api_key_in_any_committed_file():
    offenders = []
    for path in TRACKED:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)} matched {pattern.pattern}")
    assert not offenders, "secret material found: " + "; ".join(offenders)


def test_gitignore_excludes_the_env_file():
    ignored = read(".gitignore")
    assert re.search(r"^\.env$", ignored, re.M), ".env must be gitignored"


def test_env_example_lists_names_but_no_values():
    example = read(".env.example")
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "GEMINI_MODELS",
                "GROQ_MODELS", "PORT"):
        assert key in example, f"{key} must be documented"
    for line in example.splitlines():
        if line.startswith(("GEMINI_API_KEY", "GROQ_API_KEY")):
            assert line.split("=", 1)[1].strip() == "", \
                f"{line.split('=')[0]} must ship empty"


def test_dockerignore_excludes_the_env_file():
    assert ".env" in read(".dockerignore")


# --------------------------------------------------------------------------
# Docker image
# --------------------------------------------------------------------------
def test_dockerfile_binds_all_interfaces_and_exposes_the_port():
    dockerfile = read("Dockerfile")
    assert "--host" in dockerfile and "0.0.0.0" in dockerfile, \
        "the container must not bind only to localhost"
    assert re.search(r"^EXPOSE\s+8000", dockerfile, re.M)
    assert "HEALTHCHECK" in dockerfile


def test_dockerfile_runs_unprivileged():
    dockerfile = read("Dockerfile")
    assert re.search(r"^USER\s+\w+", dockerfile, re.M), "image should not run as root"


def test_dockerfile_bakes_in_no_secret():
    dockerfile = read("Dockerfile")
    for line in dockerfile.splitlines():
        if line.strip().startswith(("ENV", "ARG")):
            assert "API_KEY" not in line.upper(), \
                f"no key may be baked into the image: {line}"


def test_dockerfile_copies_what_the_app_needs():
    dockerfile = read("Dockerfile")
    for needed in ("requirements.txt", "app/", "web/"):
        assert needed in dockerfile, f"{needed} must be copied into the image"


# --------------------------------------------------------------------------
# Compose stack
# --------------------------------------------------------------------------
def test_compose_restarts_services_automatically():
    compose = read("docker-compose.yml")
    assert compose.count("restart: unless-stopped") >= 2, \
        "api and nginx must both restart on failure"


def test_compose_supplies_the_key_at_runtime_not_build_time():
    compose = read("docker-compose.yml")
    assert "env_file: .env" in compose
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(compose)


def test_compose_publishes_the_public_ports():
    compose = read("docker-compose.yml")
    assert '"80:80"' in compose and '"443:443"' in compose


# --------------------------------------------------------------------------
# nginx
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["nginx/gridwise.http.conf", "nginx/gridwise.tls.conf"])
def test_nginx_proxies_to_the_api(name):
    config = read(name)
    assert "proxy_pass http://gridwise_api" in config
    assert "/.well-known/acme-challenge/" in config, "ACME challenge must be served"
    assert "proxy_read_timeout" in config, "a model call can outlast the default"


def test_tls_config_has_a_substitutable_domain():
    config = read("nginx/gridwise.tls.conf")
    assert "DOMAIN_PLACEHOLDER" in config
    assert "listen 443 ssl" in config
    assert "fullchain.pem" in config and "privkey.pem" in config


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def test_run_py_supports_the_documented_flags():
    source = read("run.py")
    for flag in ("--no-install", "--port", "--stop"):
        assert flag in source, f"run.py must support {flag}"
    assert "0.0.0.0" in source


def test_run_onvm_supports_the_documented_flags():
    source = read("run_onVM.py")
    for flag in ("--domain", "--email", "--watchdog", "--renew", "--stop",
                 "--skip-tls"):
        assert flag in source, f"run_onVM.py must support {flag}"


def test_requirements_are_pinned():
    """A clean-environment install must be reproducible."""
    for line in read("requirements.txt").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert "==" in line, f"{line} is not pinned to a version"


# --------------------------------------------------------------------------
# README reproducibility claims
# --------------------------------------------------------------------------
def test_readme_documents_the_required_sections():
    readme = read("README.md")
    for phrase in ("GET /health", "POST /optimize-energy", "python run.py",
                   "docker run", "docker build", "run_onVM.py",
                   "GEMINI_API_KEY", "GROQ_API_KEY", "pytest",
                   "tests/test_api.py", "git clone"):
        assert phrase in readme, f"README must document {phrase!r}"


def test_readme_names_every_directive_type():
    readme = read("README.md")
    for kind in ("solar_reduction", "minimum_battery_reserve", "no_charge_window",
                 "no_discharge_window", "max_grid_window", "no_op"):
        assert kind in readme


def test_readme_documents_solver_and_limitations():
    readme = read("README.md").lower()
    assert "highs" in readme and "linprog" in readme, "solver must be disclosed"
    assert "known limitations" in readme
    assert "secret handling" in readme


def test_readme_curl_example_is_a_complete_scenario():
    """The copy-paste example must actually be valid input."""
    import json
    readme = read("README.md")
    match = re.search(r"-d '(\{.*?\n  \})'", readme, re.S)
    assert match, "README must contain a runnable curl example"
    payload = json.loads(match.group(1))

    from app.schemas import OptimizeRequest
    request = OptimizeRequest(**payload)          # raises if the example is invalid
    assert len(request.hours) == 24
    assert 1 <= len(request.operator_notes) <= 3


def test_video_script_stays_within_the_limit():
    script = read("VIDEO_SCRIPT.md")
    spoken, in_script = [], False
    for line in script.split("\n"):
        if line.startswith("## Part"):
            in_script = True
        if line.startswith("## Hard Words"):
            in_script = False
        if in_script and line.startswith("|") and line.count("|") >= 3 \
                and "Say this" not in line and ":---" not in line:
            cell = re.sub(r"[*`]|\[cut if long\]", "", line.split("|")[2]).strip()
            if cell:
                spoken.append(cell)
    words = len(" ".join(spoken).split())
    # 3:00 at a brisk 150 wpm is 450 words; staying under keeps the cut margin.
    assert words <= 450, f"script is {words} words, too long for a 3 minute limit"
