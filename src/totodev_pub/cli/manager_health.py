# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Health probe for a CaseManager deployment (§5).

Reads the manifest file DIRECTLY — it must NOT construct a CaseManagerClient
(whose constructor does a full CaseManager.attach()); a probe should be as dumb
and failure-proof as possible. This is the piece Kubernetes livenessProbes and
monitoring hook into; it catches what no in-process mechanism can ("the process
is gone entirely" / "the restart loop itself is failing").

Exit codes:
  0 — heartbeat fresh
  1 — heartbeat stale (page someone)
  2 — stopped_at set (deliberately stopped; expected during decommission)
  3 — no/unreadable manifest

Stale and deliberately-stopped are distinct on purpose: a probe that conflates
them pages people for scale-downs.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_NAMESPACE = ".case_manager"
DEFAULT_STALE_SECS = 30


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="totodev-manager-health",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cache_root", help="Cache root the manager serves")
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Manager namespace dir under the cache root (default {DEFAULT_NAMESPACE})",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.cache_root) / args.namespace / "manifest.yaml"
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest is not a mapping")
    except Exception as exc:
        print(f"no/unreadable manifest at {manifest_path}: {exc}", file=sys.stderr)
        return 3

    if data.get("stopped_at"):
        print(f"deliberately stopped (stopped_at={data['stopped_at']})")
        return 2

    heartbeat = data.get("heartbeat_at")
    stale_secs = data.get("manifest_stale_secs") or DEFAULT_STALE_SECS
    if heartbeat:
        try:
            beat = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
        except ValueError:
            print(f"unparseable heartbeat_at: {heartbeat!r}", file=sys.stderr)
            return 3
        age = (datetime.now(timezone.utc) - beat).total_seconds()
        if age <= stale_secs:
            print(f"fresh (heartbeat age {age:.1f}s, threshold {stale_secs}s)")
            return 0
        print(f"stale (heartbeat age {age:.1f}s > {stale_secs}s)")
        return 1

    print("no heartbeat recorded (manager never started or stale manifest)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
