#!/usr/bin/env python3
"""Start a fresh official container, seed fixtures, test through MCP, and remove its volumes."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def docker(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="openproject/openproject:17.8.0")
    parser.add_argument("--timeout", type=int, default=900, help="Startup timeout in seconds.")
    parser.add_argument("--version-mode", choices=("multiple", "single"), default="multiple")
    args = parser.parse_args()
    name = f"openproject-mcp-smoke-{secrets.token_hex(5)}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    started = False
    try:
        print(f"Starting {args.image} at {url}", flush=True)
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "openproject-mcp-smoke=true",
            "-p",
            f"127.0.0.1:{port}:80",
            "-e",
            f"OPENPROJECT_HOST__NAME=127.0.0.1:{port}",
            "-e",
            "OPENPROJECT_HTTPS=false",
            "-e",
            "OPENPROJECT_DEFAULT__LANGUAGE=en",
            "-e",
            f"SECRET_KEY_BASE={secrets.token_hex(64)}",
            "-e",
            "OPENPROJECT_WEB_WORKERS=1",
            args.image,
            stdout=subprocess.DEVNULL,
        )
        started = True
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/health_checks/default", timeout=5) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(3)
        else:
            docker("logs", "--tail", "80", name)
            raise RuntimeError("OpenProject did not become healthy before the startup timeout")
        print("Instance ready; creating disposable fixtures", flush=True)
        docker("cp", str(ROOT / "tests/integration/bootstrap.rb"), f"{name}:/tmp/mcp-bootstrap.rb")
        seeded = docker(
            "exec",
            "-u",
            "app",
            "-e",
            f"OP_SMOKE_VERSION_MODE={args.version_mode}",
            name,
            "bundle",
            "exec",
            "rails",
            "runner",
            "/tmp/mcp-bootstrap.rb",
            capture_output=True,
            timeout=180,
        )
        prefix = "MCP_SMOKE_FIXTURE="
        fixture = json.loads(
            next(
                line[len(prefix) :]
                for line in seeded.stdout.splitlines()
                if line.startswith(prefix)
            )
        )
        fixture["url"] = url
        with tempfile.TemporaryDirectory(prefix="openproject-mcp-smoke-") as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(json.dumps(fixture))
            path.chmod(0o600)
            env = {**os.environ, "OP_MCP_SMOKE_FIXTURE": str(path)}
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/integration",
                    "-m",
                    "integration",
                    "--live-openproject",
                    "-q",
                ],
                cwd=ROOT,
                env=env,
                check=False,
            )
            return result.returncode
    except subprocess.CalledProcessError as exc:
        # Bootstrap output can contain disposable credentials: never echo stdout.
        print(f"Container operation failed (exit {exc.returncode}).", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr[:2000], file=sys.stderr)
        return 1
    finally:
        if started:
            docker("rm", "-f", "-v", name, stdout=subprocess.DEVNULL)
            print("Disposable instance and volumes removed", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
