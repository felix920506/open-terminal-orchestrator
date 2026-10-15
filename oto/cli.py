"""``oto`` command line: run the server or talk to its admin API."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import httpx


def _client(args) -> httpx.Client:
    key = args.key or os.environ.get("ORCH_ADMIN_API_KEY") or os.environ.get("ORCH_API_KEY", "")
    return httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {key}"}, timeout=120)


def _print(resp: httpx.Response) -> None:
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    if resp.is_error:
        print(f"HTTP {resp.status_code}: {data}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, indent=2) if not isinstance(data, str) else data)


def _table(rows: list[dict], cols: list[str]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows else len(c) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="oto", description="Open Terminal Orchestrator")
    parser.add_argument("--url", default=os.environ.get("ORCH_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--key", default=None, help="admin key (default: $ORCH_ADMIN_API_KEY or $ORCH_API_KEY)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the orchestrator server")
    sub.add_parser("status", help="show orchestrator status")

    ls = sub.add_parser("ls", help="list sandboxes")
    ls.add_argument("--policy")
    ls.add_argument("--user")
    ls.add_argument("--state")
    ls.add_argument("--json", action="store_true")

    for name in ("show", "stop", "reset", "destroy"):
        p = sub.add_parser(name, help=f"{name} a sandbox")
        p.add_argument("sandbox_id")

    pol = sub.add_parser("policy", help="show or set a policy")
    pol.add_argument("policy_id")
    pol.add_argument("--set", help='policy JSON, e.g. \'{"image": "…", "storage": "5Gi"}\'')
    pol.add_argument("--lifecycle", help="lifecycle JSON")

    sub.add_parser("gc", help="run one reaper pass now")
    audit = sub.add_parser("audit", help="show recent audit events")
    audit.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .config import get_settings
        from .main import create_app

        settings = get_settings()
        logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        uvicorn.run(
            create_app(settings), host=settings.listen_host, port=settings.listen_port, log_level=settings.log_level
        )
        return

    with _client(args) as c:
        if args.cmd == "status":
            _print(c.get("/api/v1/status"))
        elif args.cmd == "ls":
            params = {
                k: v for k, v in {"policy_id": args.policy, "user_id": args.user, "state": args.state}.items() if v
            }
            r = c.get("/api/v1/terminals/sandboxes", params=params)
            if args.json or r.is_error:
                _print(r)
            else:
                _table(
                    r.json(),
                    ["id", "state", "policy_id", "context_id", "owner_user_id", "generation", "last_active_at"],
                )
        elif args.cmd == "show":
            _print(c.get(f"/api/v1/terminals/sandboxes/{args.sandbox_id}"))
        elif args.cmd == "stop":
            _print(c.post(f"/api/v1/terminals/sandboxes/{args.sandbox_id}/stop"))
        elif args.cmd == "reset":
            _print(c.post(f"/api/v1/terminals/sandboxes/{args.sandbox_id}/reset"))
        elif args.cmd == "destroy":
            _print(c.delete(f"/api/v1/terminals/sandboxes/{args.sandbox_id}"))
        elif args.cmd == "policy":
            if args.set:
                _print(c.put(f"/api/v1/policies/{args.policy_id}", json=json.loads(args.set)))
            if args.lifecycle:
                _print(c.put(f"/api/v1/policies/{args.policy_id}/lifecycle", json=json.loads(args.lifecycle)))
            if not args.set and not args.lifecycle:
                _print(c.get(f"/api/v1/policies/{args.policy_id}"))
                _print(c.get(f"/api/v1/policies/{args.policy_id}/lifecycle"))
        elif args.cmd == "gc":
            _print(c.post("/api/v1/terminals/gc"))
        elif args.cmd == "audit":
            _print(c.get("/api/v1/terminals/audit", params={"limit": args.limit}))


if __name__ == "__main__":
    main()
