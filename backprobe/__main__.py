"""Entry point: python -m backprobe — launches the Watcher daemon.

Backend selection (Decision 2): the watcher takes an injected TransportBackend.
This picks it explicitly and logs which one ran — never silently. Default is
J2534 on the bench; `--virtual [--preset NAME]` runs the fake vehicle for dev,
which is how the daemon is exercised on Linux before the DLL exists (step 7).
"""

from __future__ import annotations

import argparse
import sys

from backprobe import session_log


def _build_backend(args):
    """Return the chosen TransportBackend, or exit with a clear message."""
    if args.virtual:
        from backprobe.transport import virtual
        attached = virtual.get_preset(args.preset) if args.preset else None
        print(f"backend: virtual (preset={args.preset or 'none — waiting'})")
        return virtual.VirtualBackend(attached=attached)
    try:
        from backprobe.transport.j2534 import J2534Backend
    except ImportError:
        print("J2534 backend is not built yet (build-order step 7).",
              file=sys.stderr)
        print("Run with --virtual to exercise the daemon against a fake vehicle.",
              file=sys.stderr)
        raise SystemExit(2)
    print("backend: J2534")
    return J2534Backend()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backprobe",
                                 description="The Watcher — Phase 1 daemon.")
    ap.add_argument("--virtual", action="store_true",
                    help="use the virtual (fake-vehicle) backend instead of J2534")
    ap.add_argument("--preset", metavar="NAME",
                    help="virtual vehicle preset to attach (e.g. sedan_mil_on)")
    ap.add_argument("--events-dir", default="logs",
                    help="where to write the event log (default: ./logs)")
    args = ap.parse_args(argv)

    session_log.init(args.events_dir)            # diagnostic log alongside events
    session_log.install_crash_handler()
    session_log.log_system_info()

    backend = _build_backend(args)

    from backprobe.daemon.watcher import Watcher
    watcher = Watcher(backend, events_dir=args.events_dir)
    return watcher.run()


if __name__ == "__main__":
    sys.exit(main())
