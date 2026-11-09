#!/usr/bin/env python3
"""Errand Agent Enterprise — Production Server Launcher.

Boots the unified FastAPI Enterprise Platform serving the modern Single-Page
Application (SPA) at http://localhost:8000 with real-time SSE FSM DAG,
HITL Approval Cockpit, OpenTelemetry waterfall, and MCP server.
"""

import argparse
import os
from pathlib import Path
import sys
import webbrowser

# Add errand-agent directory to sys.path
root_dir = Path(__file__).resolve().parent
agent_dir = root_dir if (root_dir / "src").is_dir() else (root_dir / "errand-agent")
if str(agent_dir) not in sys.path:
    sys.path.insert(0, str(agent_dir))
os.chdir(str(agent_dir))

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Errand Agent Enterprise Server")
    parser.add_argument("--host", default="0.0.0.0", help="Binding host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")
    args = parser.parse_args()

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    banner = f"""
===============================================================================
  * ERRAND AGENT ENTERPRISE - AUTONOMOUS MULTI-TENANT AGENTIC PLATFORM *
===============================================================================
  * Flagship SaaS Console:  http://localhost:{args.port}
  * OpenAPI REST Docs:      http://localhost:{args.port}/docs
  * Prometheus Metrics:     http://localhost:{args.port}/metrics
  * Model Context Protocol: http://localhost:{args.port}/mcp (JSON-RPC 2.0)
  * Liveness / Health:      http://localhost:{args.port}/healthz
-------------------------------------------------------------------------------
  Architecture: 10-State Deterministic FSM backed by SQLite WAL with Reentrant Locks
  Security:     Zero-Trust 2-Layer Approval Gate (SHA-256 Digest & Schema Guard)
  Observability:OpenTelemetry Distributed Tracing with W3C Baggage & Traceparents
===============================================================================
"""
    try:
        print(banner)
    except Exception:
        pass

    if not args.no_browser:
        try:
            webbrowser.open(f"http://localhost:{args.port}")
        except Exception:
            pass

    uvicorn.run("src.app_server:app", host=args.host, port=args.port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
