"""One lightweight worker; failed projects do not stop Telegram."""
import argparse
import signal
import threading
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_engine
from app.services.project_memory import candidates, refresh_project


def run_once(engine, settings, now=None):
    tick = now or datetime.now(timezone.utc)
    if settings.project_memory_enabled is not True:
        return 0
    with Session(engine) as session:
        with session.begin():
            pending = candidates(session, settings, tick)
    count = 0
    for project_id, reconcile in pending:
        try:
            count += bool(refresh_project(engine, project_id, settings, reconciliation=reconcile, now=now))
        except Exception:
            print("Project memory refresh failed: " + str(project_id), flush=True)
    return count


def run(stop, settings=None, engine=None):
    settings = settings or get_settings()
    engine = engine or get_engine()
    while not stop.is_set():
        try:
            run_once(engine, settings)
        except Exception:
            print("Project memory worker retry pending; sensitive details omitted.", flush=True)
        stop.wait(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if settings.project_memory_enabled is not True:
        return
    if args.once:
        print("Project memories refreshed: " + str(run_once(get_engine(), settings)))
        return
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    run(stop, settings)


if __name__ == "__main__":
    main()
