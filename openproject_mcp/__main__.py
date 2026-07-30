"""Console entry point: ``openproject-mcp-server`` / ``openproject-mcp`` (SPEC §3.3, §14).

Selects the transport, checks configuration, and starts the server. A missing
or broken configuration produces a short actionable message on stderr and a
non-zero exit code — never a traceback.
"""

from __future__ import annotations

import argparse
import os.path
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from openproject_mcp import __version__
from openproject_mcp.config import Settings, check_runtime_config

__all__ = ["main"]

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130


def _prog() -> str:
    """The name this process was invoked as, when it is one of our scripts.

    Both console scripts run this ``main``; ``--version`` and usage text
    should echo whichever the user typed. Anything else (``python -m ...``,
    test harnesses) reports the canonical distribution name.
    """
    invoked = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if invoked in ("openproject-mcp", "openproject-mcp-server"):
        return invoked
    return "openproject-mcp-server"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description=(
            "MCP server for OpenProject. Configuration comes from the environment: "
            "OPENPROJECT_URL and OPENPROJECT_API_KEY are required."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="Transport to serve on (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for --transport http (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for --transport http (default: 8000).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and exit without starting the server.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _fail(message: str, problems: Sequence[str] = ()) -> int:
    print(f"openproject-mcp: {message}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "  See https://github.com/kar-thik/openproject-mcp#configuration "
        "for the full variable list.",
        file=sys.stderr,
    )
    return EXIT_CONFIG_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code; raises nothing."""
    args = build_parser().parse_args(argv)

    try:
        settings = Settings()
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        ]
        return _fail("invalid configuration", problems)

    problems = check_runtime_config(settings, args.transport)
    if problems:
        return _fail("cannot start, configuration is incomplete", problems)

    if args.check:
        print(f"openproject-mcp: configuration OK ({settings.url})", file=sys.stderr)
        return EXIT_OK

    # Imported here so `--help` and `--check` stay fast and dependency-light.
    from openproject_mcp.server import build_server

    server = build_server(settings)
    try:
        if args.transport == "http":
            server.run(
                transport="http",
                host=args.host or settings.http_host,
                port=args.port or settings.http_port,
                show_banner=False,
            )
        else:
            server.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
