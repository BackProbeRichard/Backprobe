"""Entry point: python -m backprobe

Launches the Phase 1 daemon (the Watcher). Until the daemon exists
(build-order step 6), this is an honest placeholder.
"""

import sys


def main() -> int:
    print("backprobe: the daemon is not built yet (build-order step 6).")
    print("Nothing was opened, nothing is held.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
