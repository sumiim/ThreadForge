"""ThreadForge local Worker command line."""

from __future__ import annotations

import argparse
import platform

from .client import WorkerClient, _validated_server_url, pair
from .config import ConfigStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threadforge-worker")
    parser.add_argument("--data-dir", default=None, help="override the user-scoped Worker data directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pair_parser = subparsers.add_parser("pair", help="pair this computer with a signed-in ThreadForge account")
    pair_parser.add_argument("--server", default="http://127.0.0.1:18000")
    pair_parser.add_argument("--code", required=True)
    pair_parser.add_argument("--name", default=platform.node() or "My computer")

    workspace = subparsers.add_parser("workspace", help="manage local workspace authorization")
    workspace_subparsers = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_add = workspace_subparsers.add_parser("add")
    workspace_add.add_argument("path")
    workspace_add.add_argument("--name", default=None)
    workspace_subparsers.add_parser("list")

    subparsers.add_parser("run", help="connect to ThreadForge and execute assigned tasks")
    subparsers.add_parser("service", help="run the per-user Worker Companion service")
    subparsers.add_parser("status", help="show non-secret local Worker configuration")
    update = subparsers.add_parser("update", help="verify and install the latest signed Worker release")
    update.add_argument("--check", action="store_true", help="check without installing")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    store = ConfigStore(args.data_dir)
    config = store.load()
    if args.command == "pair":
        result = pair(args.server, args.code, args.name)
        config.server_url = _validated_server_url(args.server)
        config.device_id = result["device_id"]
        config.device_token = result["device_token"]
        config.device_name = result["name"]
        store.save(config)
        print(f"Paired device: {config.device_name} ({config.device_id})")
        from .service import start_service_background

        start_service_background(args.data_dir)
        return 0
    if args.command == "workspace" and args.workspace_command == "add":
        workspace = store.add_workspace(config, args.path, args.name)
        print(f"Authorized workspace: {workspace.name} ({workspace.workspace_id})")
        return 0
    if args.command == "workspace" and args.workspace_command == "list":
        for workspace in config.workspaces:
            print(f"{workspace.workspace_id}\t{workspace.name}\t{workspace.path}")
        return 0
    if args.command == "status":
        print(f"Server: {config.server_url}")
        print(f"Device: {config.device_name or '-'} ({config.device_id or 'not paired'})")
        print(f"Workspaces: {len(config.workspaces)}")
        return 0
    if args.command == "service":
        from .service import run_service, start_service_background

        result = run_service(args.data_dir)
        if result == 75:
            start_service_background(args.data_dir)
            return 0
        return result
    if args.command == "update":
        from .updater import apply_update, update_available

        if args.check:
            available, manifest = update_available(store)
            print(f"Latest: {manifest['version']} ({'update available' if available else 'current'})")
            return 0
        print("Worker updated." if apply_update(store) else "Worker is current.")
        return 0
    WorkerClient(store, config).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
