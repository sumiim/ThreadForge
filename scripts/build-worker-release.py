"""Build a canonical Ed25519-signed Worker release manifest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.tag != f"worker-v{args.version}":
        raise ValueError("release tag must equal worker-v<version>")
    artifact = args.artifact.resolve(strict=True)
    private_key = serialization.load_pem_private_key(
        base64.b64decode(os.environ["WORKER_RELEASE_SIGNING_KEY_B64"], validate=True),
        password=None,
    )
    manifest = {
        "schema_version": 1,
        "channel": "stable",
        "version": args.version,
        "protocol_version": 1,
        "minimum_server_protocol": 1,
        "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "platforms": {
            "windows-x86_64": {
                "filename": artifact.name,
                "url": (
                    f"https://github.com/{args.repository}/releases/download/"
                    f"{args.tag}/{artifact.name}"
                ),
                "size": artifact.stat().st_size,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        },
    }
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest["signature"] = {
        "algorithm": "ed25519",
        "value": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
