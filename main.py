"""
main.py — Unified Master Launcher for Kite Connect Trading Systems

Launches both:
1. server.py (Flask Web Application, Intraday Strategies, Order & MTM Engine)
2. pos_strngl.py (Positional Strangle Multi-Day Strategy Engine)
"""

import sys
import os
import time
import subprocess
import signal
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def start_server():
    """Runs the main Flask web and trading server."""
    print("🚀 [Main] Starting server.py on http://127.0.0.1:5050...")
    try:
        import server
        # Server starts Flask app and intraday background threads
        server.app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
    except Exception as e:
        print(f"❌ [Main] Error running server.py: {e}")

def start_pos_strangle():
    """Starts the positional strangle strategy engine."""
    print("🧭 [Main] Initializing pos_strngl.py engine...")
    try:
        import pos_strngl
        # pos_strngl starts its internal background daemon thread upon import
        print("✅ [Main] pos_strngl.py engine initialized and monitoring.")
    except Exception as e:
        print(f"❌ [Main] Error initializing pos_strngl.py: {e}")

def main():
    print("=" * 65)
    print("  KITE CONNECT TRADING SYSTEM — UNIFIED MASTER LAUNCHER")
    print("  Components: server.py (Intraday/Web) + pos_strngl.py (Positional)")
    print("=" * 65)

    # 1. Initialize positional strangle engine
    start_pos_strangle()

    # 2. Run Flask Web Server in main thread
    start_server()

if __name__ == "__main__":
    main()
