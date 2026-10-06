import argparse
from uuid import uuid4
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_engine
from app.services.project_memory_events import schedule_refresh
from app.services.project_memory import refresh_project
from app.services.normalization import normalize
from app.services.queries import resolve_scope


def main():
    parser = argparse.ArgumentParser(description="Explicit project memory refresh")
    parser.add_argument("command", choices=["refresh", "rebuild"])
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        settings = get_settings()
        engine = get_engine()
        with Session(engine) as session:
            with session.begin():
                scope = resolve_scope(session, "proyecto " + normalize(args.project))
            if scope.error or len(scope.project_ids) != 1:
                raise SystemExit(scope.error or "Indica un proyecto inequivoco.")
            print(schedule_refresh(session, args.project, settings, origin_key="cli:" + str(uuid4())))
        done = refresh_project(engine, scope.project_ids[0], settings, reconciliation=args.command == "rebuild", force=True)
        print("Project memory refreshed." if done else "Project memory pending, busy, disabled or without evidence.")
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("Project memory no completada; detalles sensibles omitidos.") from None


if __name__ == "__main__":
    main()
