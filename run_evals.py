#!/usr/bin/env python3
"""Root delegation runner for 33 evaluation scenarios."""
import sys
from pathlib import Path

agent_dir = Path(__file__).resolve().parent / "errand-agent"
if str(agent_dir) not in sys.path:
    sys.path.insert(0, str(agent_dir))

import run_evals

if __name__ == "__main__":
    raise SystemExit(run_evals.main())
