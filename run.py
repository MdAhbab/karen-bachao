#!/usr/bin/env python3
"""Single entry point: install dependencies, stop any previous run, start up.

    python run.py                 install deps, then serve API + console
    python run.py --no-install    skip dependency installation
    python run.py --port 9000     use a different port
    python run.py --stop          stop a running instance and exit

One uvicorn process serves both the JSON API and the operator console, so
there is exactly one service to deploy.
"""

import argparse
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PIDFILE = ROOT / ".run.pid"
IS_WINDOWS = platform.system() == "Windows"


def info(message: str) -> None:
    print(f"  {message}", flush=True)


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


# ---------------------------------------------------------------- environment
def check_python() -> None:
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ is required (found {platform.python_version()}).")


def install_requirements() -> None:
    step("Installing dependencies")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")]
    )
    if result.returncode != 0:
        sys.exit("Dependency installation failed. Fix the errors above and retry.")
    info("dependencies ready")


def check_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        example = ROOT / ".env.example"
        print(
            "\nWARNING: no .env file found.\n"
            f"  Copy {example.name} to .env and set GEMINI_API_KEY, otherwise the\n"
            "  service falls back to its deterministic parser for operator notes.\n",
            flush=True,
        )
        return
    text = env_file.read_text(encoding="utf-8")
    match = re.search(r"^GEMINI_API_KEY=(.*)$", text, re.MULTILINE)
    if not match or not match.group(1).strip():
        print("\nWARNING: GEMINI_API_KEY is empty in .env.\n", flush=True)
    else:
        info(".env loaded, GEMINI_API_KEY is set")


# ---------------------------------------------------------------- stop previous
def pids_on_port(port: int) -> set[int]:
    """Every process currently listening on the port."""
    pids: set[int] = set()
    try:
        if IS_WINDOWS:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP" and "LISTENING" in line:
                    if parts[1].endswith(f":{port}"):
                        pids.add(int(parts[-1]))
        elif shutil.which("lsof"):
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
            pids.update(int(p) for p in out.split() if p.strip().isdigit())
        elif shutil.which("ss"):
            out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if f":{port} " in line:
                    pids.update(int(p) for p in re.findall(r"pid=(\d+)", line))
    except Exception as exc:  # noqa: BLE001
        info(f"could not inspect port {port}: {exc}")
    pids.discard(0)
    return pids


def kill(pid: int) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


def stop_previous(port: int) -> None:
    step(f"Stopping anything already running on port {port}")
    stopped = set()

    if PIDFILE.is_file():
        try:
            pid = int(PIDFILE.read_text().strip())
            kill(pid)
            stopped.add(pid)
            info(f"stopped previous run (pid {pid})")
        except Exception:  # noqa: BLE001
            pass
        PIDFILE.unlink(missing_ok=True)

    for pid in pids_on_port(port):
        if pid in stopped or pid == os.getpid():
            continue
        kill(pid)
        stopped.add(pid)
        info(f"freed port {port} (pid {pid})")

    if stopped:
        # Give the OS a moment to release the socket before rebinding.
        for _ in range(20):
            if not pids_on_port(port):
                break
            time.sleep(0.25)
    else:
        info("nothing was running")


# ---------------------------------------------------------------- start
def wait_for_health(port: int, timeout: float = 45.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def serve(port: int) -> int:
    step(f"Starting GridWise on port {port}")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", str(port)],
        cwd=str(ROOT),
    )
    PIDFILE.write_text(str(process.pid), encoding="utf-8")

    if wait_for_health(port):
        print(
            f"\n  GridWise is ready.\n"
            f"    Console : http://localhost:{port}/ui/\n"
            f"    Health  : http://localhost:{port}/health\n"
            f"    API     : POST http://localhost:{port}/optimize-energy\n"
            f"\n  Press Ctrl+C to stop.\n",
            flush=True,
        )
    else:
        print("\n  Service did not become healthy in time. See the log above.\n",
              flush=True)

    try:
        return process.wait()
    except KeyboardInterrupt:
        print("\n  Shutting down...", flush=True)
        kill(process.pid)
        return 0
    finally:
        PIDFILE.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GridWise service.")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--no-install", action="store_true",
                        help="skip pip install")
    parser.add_argument("--stop", action="store_true",
                        help="stop a running instance and exit")
    args = parser.parse_args()

    check_python()
    if args.stop:
        stop_previous(args.port)
        return 0

    if not args.no_install:
        install_requirements()
    check_env()
    stop_previous(args.port)
    return serve(args.port)


if __name__ == "__main__":
    sys.exit(main())
