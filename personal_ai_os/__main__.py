"""One command starts the local Web process and an independent worker process."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import load_config
from .ntfy_notifications import notifier_from_environment
from .storage import Repository
from .worker import Worker


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal AI OS local processes")
    parser.add_argument("command", choices=["start", "worker", "remote-view", "mobile-view"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--public-origin", help="mobile-view: exact private HTTPS origin")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--once", action="store_true", help="worker: process due jobs once")
    args = parser.parse_args()
    if args.port is None:
        args.port = {"start": 8501, "worker": 8501, "remote-view": 8765,
                     "mobile-view": 8766}[args.command]
    config = load_config()
    if args.command == "worker":
        repository = Repository(config.database_path)
        repository.initialize()
        worker = Worker(repository, timezone_name=config.timezone,
                        remote_notifier=notifier_from_environment())
        if args.once:
            worker.run_once()
            return 0
        worker.run_forever(args.poll_seconds)
        return 0
    if args.command == "remote-view":
        from wsgiref.simple_server import make_server

        from .remote_view import RemoteViewApp
        from .services import RemoteReadService

        # Deliberately never bind a LAN interface here. A real deployment needs a
        # user-selected authenticated TLS boundary before any remote exposure.
        repository = Repository(config.database_path)
        repository.initialize()
        with make_server("127.0.0.1", args.port, RemoteViewApp(RemoteReadService(repository))) as server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0

    if args.command == "mobile-view":
        from wsgiref.simple_server import make_server
        from .bootstrap import build_service
        from .mobile_view import MobileViewApp

        origin = args.public_origin or f"http://127.0.0.1:{args.port}"
        app = MobileViewApp(build_service(), public_origin=origin, local_port=args.port)
        # The private HTTPS proxy connects to this loopback socket. Directory or
        # device settings cannot change the listener address.
        with make_server("127.0.0.1", args.port, app) as server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0

    root = Path(__file__).resolve().parents[1]
    Repository(config.database_path).initialize()
    web = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", str(root / "app.py"),
        "--server.address", "127.0.0.1", "--server.port", str(args.port),
        "--server.headless", "true",
    ], cwd=root, env=os.environ.copy())
    worker = subprocess.Popen([
        sys.executable, "-m", "personal_ai_os", "worker", "--poll-seconds", str(args.poll_seconds),
    ], cwd=root, env=os.environ.copy())
    def stop_children(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop_children)
    try:
        while True:
            for name, process in (("Web", web), ("worker", worker)):
                if process.poll() is not None:
                    raise RuntimeError(f"{name} process exited: {process.returncode}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        for process in (web, worker):
            if process.poll() is None:
                process.terminate()
        for process in (web, worker):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
