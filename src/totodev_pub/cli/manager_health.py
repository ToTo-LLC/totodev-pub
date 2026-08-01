# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Health probe for a CaseManager deployment (§5).

Reads the manifest file DIRECTLY — it must NOT construct a CaseManagerClient
(whose constructor does a full CaseManager.attach()); a probe should be as dumb
and failure-proof as possible. This is the piece Kubernetes livenessProbes and
monitoring hook into; it catches what no in-process mechanism can ("the process
is gone entirely" / "the restart loop itself is failing").

Exit codes:
  0 — heartbeat fresh, or recovery in progress within its grace window
  1 — heartbeat stale, or recovery overran its grace window (page someone)
  2 — stopped_at set (deliberately stopped; expected during decommission)
  3 — no/unreadable manifest

Stale and deliberately-stopped are distinct on purpose: a probe that conflates
them pages people for scale-downs.

Recovery counts as healthy, and that is not leniency. Reclaiming a crashed
owner's cases means waiting out its heartbeat lease — tens of seconds during
which nothing beats, because the manager loop has not started. A probe that read
that as dead would kill the process every single time, restarting the wait from
zero and never letting a crashed fleet recover at all.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_NAMESPACE = ".case_manager"
DEFAULT_STALE_SECS = 30

# How long recovery may take before the probe stops giving it the benefit of the
# doubt. The lease-reclaim wait is itself bounded at two lease TTLs (60s); this
# leaves room for that plus the scan around it, and still fails loudly for a
# recovery that is genuinely stuck.
RECOVERY_GRACE_SECS = 120.0


def _age_secs(stamp: str) -> float | None:
    """Seconds since an ISO-8601 stamp, or None if it will not parse."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


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

    recovering = data.get("recovering_at")
    if recovering and not heartbeat:
        age = _age_secs(recovering)
        if age is None:
            print(f"unparseable recovering_at: {recovering!r}", file=sys.stderr)
            return 3
        if age <= RECOVERY_GRACE_SECS:
            # Healthy, not merely alive: recovery emits no heartbeat because it
            # is waiting out a crashed owner's lease, and killing it here would
            # restart that wait from zero — forever.
            print(f"recovering (started {age:.1f}s ago, grace {RECOVERY_GRACE_SECS}s)")
            return 0
        print(f"recovery has not completed in {age:.1f}s", file=sys.stderr)
        return 1
    if heartbeat:
        age = _age_secs(heartbeat)
        if age is None:
            print(f"unparseable heartbeat_at: {heartbeat!r}", file=sys.stderr)
            return 3
        if age <= stale_secs:
            print(f"fresh (heartbeat age {age:.1f}s, threshold {stale_secs}s)")
            return 0
        print(f"stale (heartbeat age {age:.1f}s > {stale_secs}s)")
        return 1

    print("no heartbeat recorded (manager never started or stale manifest)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
