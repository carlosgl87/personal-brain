"""Supervisa FastAPI y Telegram dentro de un único contenedor."""
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.database import get_engine


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    except ProcessLookupError:
        pass


def wait_api(port, child, stop, timeout=120):
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
        while not stop.is_set() and time.monotonic() < deadline:
            if child.poll() is not None:
                return False
            try:
                if client.get(f"http://127.0.0.1:{port}/health/db").status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            stop.wait(1)
    return False


def supervise(port, stop):
    api = bot = memory_worker = connection = None
    worker_restart_after = 0
    lock_id = None
    locked = False
    try:
        settings = get_settings()
        try:
            token, _ = settings.telegram_credentials()
        except ValueError:
            print("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_USER_ID validos en Railway.", flush=True)
            return 1
        try:
            settings.llm_credentials()
        except ValueError:
            print("Configura LLM_PROVIDER=anthropic, LLM_MODEL y LLM_API_KEY en Railway.", flush=True)
            return 1
        if settings.openrouter_api_key is None or not settings.openrouter_api_key.get_secret_value().strip():
            print("Configura OPENROUTER_API_KEY en Railway.", flush=True)
            return 1
        # Bloqueo por bot, con conexión dedicada; la clave nunca se registra.
        lock_id = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big", signed=True)
        api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                                "--host", "0.0.0.0", "--port", str(port), "--no-access-log"])
        if not wait_api(port, api, stop):
            return 0 if stop.is_set() else 1
        connection = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
        print("API lista. Esperando turno para iniciar Telegram.", flush=True)
        while not stop.is_set():
            if api.poll() is not None:
                return 1
            if not locked:
                locked = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:lock_id)"),
                                                {"lock_id": lock_id}))
                if locked:
                    bot = subprocess.Popen([sys.executable, "-m", "app.telegram",
                                            "--port", str(port), "--process"])
                    print("Receptor Telegram iniciado.", flush=True)
            else:
                # Si se pierde la conexión que sostiene el bloqueo, detener el bot.
                connection.scalar(text("SELECT 1"))
                if bot.poll() is not None:
                    print("El receptor se detuvo; se reiniciará el servicio.", flush=True)
                    return 1
            if locked and settings.project_memory_enabled is True:
                if memory_worker is not None and memory_worker.poll() is not None:
                    print("Project Memory worker detenido; Telegram sigue activo. Reintento en 30 segundos.", flush=True)
                    memory_worker = None
                    worker_restart_after = time.monotonic() + 30
                if memory_worker is None and time.monotonic() >= worker_restart_after:
                    try:
                        memory_worker = subprocess.Popen([sys.executable, "-m", "app.project_memory_worker"])
                        print("Project Memory worker iniciado.", flush=True)
                    except Exception:
                        worker_restart_after = time.monotonic() + 30
                        print("Project Memory worker no pudo iniciar; Telegram sigue activo.", flush=True)
            stop.wait(2)
        return 0
    except Exception:
        print("No se completó el arranque. Revisa variables, base de datos y migraciones; detalles sensibles omitidos.", flush=True)
        return 1
    finally:
        # Stop polling before releasing ownership, even if another child fails to stop.
        for child in (bot, memory_worker, api):
            try:
                stop_child(child)
            except Exception:
                print("No se completo el cierre de un proceso hijo; detalles omitidos.", flush=True)
        if connection is not None:
            try:
                if locked:
                    connection.scalar(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
            except Exception:
                connection.invalidate()
            connection.close()


def main():
    try:
        port = int(os.environ.get("PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT debe ser un puerto válido.") from None
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    raise SystemExit(supervise(port, stop))


if __name__ == "__main__":
    main()
