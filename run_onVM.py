#!/usr/bin/env python3
"""Deploy GridWise on an Ubuntu VM with Docker, nginx and Let's Encrypt.

Run this ON the VM, from the repository root:

    sudo python3 run_onVM.py --domain BUPtestAPI.ahbab.dev --email you@example.com

What it does, in order:
  1. verifies Docker and the compose plugin, installing them if missing
  2. checks that the domain actually resolves to this machine
  3. stops any previous stack and frees ports 80, 443 and 8000
  4. builds and starts the stack on plain HTTP
  5. obtains a certificate, then switches nginx to HTTPS
  6. waits for /health and prints the verified public URL

Extra modes:
    --watchdog          poll /health and restart the API if it stops answering
    --renew             renew the certificate and reload nginx
    --stop              stop the stack and exit
    --skip-tls          stay on plain HTTP (useful before DNS has propagated)
"""

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NGINX_DIR = ROOT / "nginx"
CERTBOT_DIR = ROOT / "certbot"
LOG = ROOT / "watchdog.log"

DEFAULT_DOMAIN = "BUPtestAPI.ahbab.dev"


# ----------------------------------------------------------------- utilities
def say(message: str) -> None:
    print(f"  {message}", flush=True)


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(command: list[str], check: bool = True, quiet: bool = False):
    result = subprocess.run(command, capture_output=quiet, text=True)
    if check and result.returncode != 0:
        if quiet and result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(f"Command failed: {' '.join(command)}")
    return result


def compose(*args: str, check: bool = True, quiet: bool = False):
    return run(["docker", "compose", *args], check=check, quiet=quiet)


# ----------------------------------------------------------------- prechecks
def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sys.exit("This script needs root for Docker and ports 80/443. Re-run with sudo.")


def ensure_docker() -> None:
    step("Checking Docker")
    if shutil.which("docker") is None:
        say("docker not found, installing from the Ubuntu repositories")
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "-qq", "docker.io", "docker-compose-v2"])
    if subprocess.run(["docker", "compose", "version"],
                      capture_output=True).returncode != 0:
        say("compose plugin missing, installing")
        run(["apt-get", "install", "-y", "-qq", "docker-compose-v2"])
    run(["systemctl", "enable", "--now", "docker"], check=False, quiet=True)
    say("docker and compose are ready")


def ensure_env() -> None:
    env = ROOT / ".env"
    if not env.is_file():
        sys.exit(
            f"No .env file at {env}.\n"
            "  Copy .env.example to .env and set GEMINI_API_KEY before deploying.\n"
            "  The key is read at runtime and is never baked into the image."
        )
    text = env.read_text(encoding="utf-8")
    if "GEMINI_API_KEY=" not in text or not text.split("GEMINI_API_KEY=")[1].split("\n")[0].strip():
        say("WARNING: GEMINI_API_KEY is empty; operator notes will use the "
            "deterministic parser only.")
    else:
        say(".env present, GEMINI_API_KEY is set")


def public_ip() -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                return response.read().decode().strip()
        except Exception:  # noqa: BLE001
            continue
    return None


def check_dns(domain: str) -> bool:
    """Warn loudly if the domain does not point here: certbot will fail."""
    step(f"Checking DNS for {domain}")
    import socket
    try:
        resolved = sorted({info[4][0] for info in socket.getaddrinfo(domain, None)})
    except socket.gaierror:
        say(f"{domain} does not resolve yet.")
        return False

    mine = public_ip()
    say(f"{domain} resolves to {', '.join(resolved)}")
    if mine:
        say(f"this VM's public IP is {mine}")
        if mine in resolved:
            say("DNS points at this machine")
            return True
        say("WARNING: DNS does not point at this machine.")
        say("  A CNAME must target a hostname; if this VM only has a bare IP,")
        say(f"  create an A record for {domain} -> {mine} instead.")
        say("  If the record is behind a proxy (for example Cloudflare), turn the")
        say("  proxy off until the certificate is issued.")
        return False
    return True


# ----------------------------------------------------------------- lifecycle
def free_ports() -> None:
    step("Stopping any previous deployment")
    compose("down", "--remove-orphans", check=False, quiet=True)
    for name in ("gridwise-api", "gridwise-nginx"):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    for port in (80, 443, 8000):
        result = subprocess.run(["bash", "-c", f"ss -ltnp 2>/dev/null | grep ':{port} ' || true"],
                                capture_output=True, text=True)
        if result.stdout.strip():
            say(f"port {port} still busy: {result.stdout.strip().splitlines()[0]}")
            subprocess.run(["bash", "-c", f"fuser -k {port}/tcp 2>/dev/null || true"],
                           capture_output=True)
    say("previous stack stopped")


def write_nginx_config(domain: str, tls: bool) -> None:
    source = NGINX_DIR / ("gridwise.tls.conf" if tls else "gridwise.http.conf")
    config = source.read_text(encoding="utf-8").replace("DOMAIN_PLACEHOLDER", domain)
    (NGINX_DIR / "gridwise.conf").write_text(config, encoding="utf-8")
    say(f"nginx configured for {'HTTPS' if tls else 'HTTP'}")


def start_stack(build: bool = True) -> None:
    step("Building and starting the stack")
    CERTBOT_DIR.joinpath("www").mkdir(parents=True, exist_ok=True)
    CERTBOT_DIR.joinpath("conf").mkdir(parents=True, exist_ok=True)
    if build:
        compose("build")
    compose("up", "-d")
    say("containers are up")


def cert_exists(domain: str) -> bool:
    return (CERTBOT_DIR / "conf" / "live" / domain / "fullchain.pem").is_file()


def obtain_certificate(domain: str, email: str, staging: bool) -> bool:
    step(f"Requesting a certificate for {domain}")
    if cert_exists(domain):
        say("a certificate already exists, skipping issuance")
        return True

    command = [
        "docker", "run", "--rm",
        "-v", f"{CERTBOT_DIR / 'conf'}:/etc/letsencrypt",
        "-v", f"{CERTBOT_DIR / 'www'}:/var/www/certbot",
        "certbot/certbot", "certonly",
        "--webroot", "-w", "/var/www/certbot",
        "-d", domain,
        "--non-interactive", "--agree-tos", "--no-eff-email",
        "--email", email,
    ]
    if staging:
        command.append("--staging")
    result = subprocess.run(command)
    if result.returncode != 0:
        say("certificate issuance failed. The service stays on plain HTTP.")
        say("  Most common cause: DNS is not pointing at this VM yet, or port 80")
        say("  is not reachable from the internet. Fix that and re-run with --renew.")
        return False
    say("certificate issued")
    return True


def renew_certificate() -> None:
    step("Renewing certificates")
    run(["docker", "run", "--rm",
         "-v", f"{CERTBOT_DIR / 'conf'}:/etc/letsencrypt",
         "-v", f"{CERTBOT_DIR / 'www'}:/var/www/certbot",
         "certbot/certbot", "renew"], check=False)
    compose("exec", "nginx", "nginx", "-s", "reload", check=False, quiet=True)
    say("renewal attempted and nginx reloaded")


# ----------------------------------------------------------------- health
def probe(url: str, timeout: float = 5.0) -> bool:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return response.status == 200 and json.loads(
                response.read().decode()).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def wait_for_health(urls: list[str], timeout: float = 120.0) -> str | None:
    step("Waiting for /health")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for url in urls:
            if probe(url):
                say(f"healthy at {url}")
                return url
        time.sleep(3)
    say("service did not become healthy in time")
    compose("logs", "--tail", "40", "api", check=False)
    return None


def watchdog(url: str, interval: int) -> int:
    """Restart the API if it stops answering /health.

    Container crashes are already handled by `restart: unless-stopped`; this
    covers the case where the process is alive but no longer healthy.
    """
    step(f"Watchdog active on {url} (every {interval}s, Ctrl+C to stop)")
    failures = 0
    while True:
        try:
            if probe(url, timeout=10):
                failures = 0
            else:
                failures += 1
                message = f"{time.strftime('%Y-%m-%d %H:%M:%S')} health check failed ({failures})"
                print(f"  {message}", flush=True)
                with LOG.open("a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
                if failures >= 2:
                    print("  restarting the api container", flush=True)
                    with LOG.open("a", encoding="utf-8") as handle:
                        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} restarting api\n")
                    compose("up", "-d", "--force-recreate", "api",
                            check=False, quiet=True)
                    failures = 0
                    time.sleep(20)
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n  watchdog stopped", flush=True)
            return 0


# ----------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy GridWise on an Ubuntu VM.")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--email", default="")
    parser.add_argument("--skip-tls", action="store_true",
                        help="stay on plain HTTP")
    parser.add_argument("--staging", action="store_true",
                        help="use the Let's Encrypt staging environment")
    parser.add_argument("--watchdog", action="store_true",
                        help="after deploying, keep polling /health")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--renew", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    os.chdir(ROOT)
    require_root()

    if args.stop:
        free_ports()
        return 0

    if args.renew:
        renew_certificate()
        return 0

    ensure_docker()
    ensure_env()
    dns_ok = check_dns(args.domain)
    free_ports()

    # Always come up on HTTP first so the ACME challenge can be served.
    write_nginx_config(args.domain, tls=False)
    start_stack(build=not args.no_build)

    healthy = wait_for_health([f"http://127.0.0.1/health",
                               f"http://{args.domain}/health"])
    if healthy is None:
        return 1

    public_url = f"http://{args.domain}"
    if not args.skip_tls:
        if not args.email:
            say("no --email given, skipping TLS. Re-run with --email to enable HTTPS.")
        elif not dns_ok and not cert_exists(args.domain):
            say("skipping TLS because DNS does not point here yet.")
            say("  Fix the DNS record, then re-run this script to enable HTTPS.")
        elif obtain_certificate(args.domain, args.email, args.staging):
            write_nginx_config(args.domain, tls=True)
            compose("up", "-d", "nginx")
            if wait_for_health([f"https://{args.domain}/health"], timeout=60):
                public_url = f"https://{args.domain}"
            else:
                say("HTTPS did not answer; rolling nginx back to HTTP")
                write_nginx_config(args.domain, tls=False)
                compose("up", "-d", "nginx")

    step("Deployment complete")
    print(
        f"\n  Public base URL : {public_url}\n"
        f"    Health        : {public_url}/health\n"
        f"    Optimize      : POST {public_url}/optimize-energy\n"
        f"    Console       : {public_url}/ui/\n"
        f"\n  Verify from another machine:\n"
        f"    curl {public_url}/health\n"
        f"    python tests/test_api.py --base-url {public_url}\n"
        f"\n  Docker fallback image:\n"
        f"    docker run -d -p 8000:8000 -e GEMINI_API_KEY=your_key gridwise-api:1.0.0\n"
        f"\n  Keep it alive:\n"
        f"    sudo python3 run_onVM.py --watchdog --domain {args.domain}\n",
        flush=True,
    )

    if args.watchdog:
        return watchdog(f"{public_url}/health", args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
