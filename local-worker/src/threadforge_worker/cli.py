"""ThreadForge local Worker command line."""

from __future__ import annotations

import argparse
import platform
import re
from urllib.parse import parse_qs, urlsplit

from .client import WorkerClient, _validated_server_url, pair
from .config import ConfigStore
from .machine import machine_fingerprint


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
    protocol = subparsers.add_parser("protocol", help="handle a trusted threadforge:// link")
    protocol.add_argument("uri")
    subparsers.add_parser("status", help="show non-secret local Worker configuration")
    update = subparsers.add_parser("update", help="verify and install the latest signed Worker release")
    update.add_argument("--check", action="store_true", help="check without installing")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "protocol":
        action, parameters = _parse_protocol_uri(args.uri)
        if action == "uninstall":
            from .service import start_uninstaller

            start_uninstaller()
            return 0

    store = ConfigStore(args.data_dir)
    config = store.load()
    if args.command == "pair":
        _pair_and_save(store, config, args.server, args.code, args.name)
        print(f"Paired device: {config.device_name} ({config.device_id})")
        from .service import start_service_background

        start_service_background(args.data_dir)
        return 0
    if args.command == "protocol":
        from .service import start_service_background

        if action == "pair":
            _pair_and_save(
                store,
                config,
                parameters["server"],
                parameters["code"],
                parameters.get("name", platform.node() or "My computer"),
            )
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
        from .updater import load_update_status

        print(f"Server: {config.server_url}")
        print(f"Device: {config.device_name or '-'} ({config.device_id or 'not paired'})")
        print(f"Workspaces: {len(config.workspaces)}")
        update_status = load_update_status(store)
        if update_status:
            print(
                "Update: "
                f"{update_status['status']} "
                f"({update_status['current_version']} -> "
                f"{update_status['target_version'] or '-'})"
            )
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


def _pair_and_save(
    store: ConfigStore,
    config,
    server: str,
    code: str,
    name: str,
) -> None:
    validated_server = _validated_server_url(server)
    if not re.fullmatch(r"[0-9A-Fa-f]{4}(?:-[0-9A-Fa-f]{4}){3}", code):
        raise ValueError("pairing code is invalid")
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 100:
        raise ValueError("device name is invalid")
    # 稳定机器指纹：优先沿用已持久化的值（重装/重配对不重建 device），否则现算。
    fingerprint = config.machine_fingerprint or machine_fingerprint()
    result = pair(validated_server, code, normalized_name, machine_fingerprint=fingerprint)
    config.server_url = validated_server
    config.device_id = result["device_id"]
    config.device_token = result["device_token"]
    config.device_name = result["name"]
    config.machine_fingerprint = fingerprint
    store.save(config)


def _parse_protocol_uri(uri: str) -> tuple[str, dict[str, str]]:
    parsed = urlsplit(uri)
    if (
        parsed.scheme.lower() != "threadforge"
        or parsed.netloc.lower() != "worker"
        or parsed.fragment
    ):
        raise ValueError("unsupported ThreadForge link")
    action = parsed.path.strip("/").lower()
    allowed = {
        "start": set(),
        "pair": {"server", "code", "name"},
        "uninstall": set(),
    }
    if action not in allowed:
        raise ValueError("unsupported ThreadForge action")
    raw = (
        parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if parsed.query
        else {}
    )
    if set(raw) - allowed[action] or any(len(values) != 1 for values in raw.values()):
        raise ValueError("ThreadForge link contains unsupported parameters")
    parameters = {key: values[0] for key, values in raw.items()}
    if action in {"start", "uninstall"} and parameters:
        raise ValueError(f"{action} link must not contain parameters")
    if action == "pair":
        if set(parameters) not in ({"server", "code"}, {"server", "code", "name"}):
            raise ValueError("pair link is incomplete")
        _validated_server_url(parameters["server"])
        if not re.fullmatch(r"[0-9A-Fa-f]{4}(?:-[0-9A-Fa-f]{4}){3}", parameters["code"]):
            raise ValueError("pairing code is invalid")
        if "name" in parameters and (not parameters["name"].strip() or len(parameters["name"]) > 100):
            raise ValueError("device name is invalid")
    return action, parameters


if __name__ == "__main__":
    raise SystemExit(main())
