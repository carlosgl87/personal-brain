"""Polling local: Telegram -> FastAPI -> PostgreSQL. No inicia al importar."""
import argparse
import logging
import time

import httpx

from app.config import get_settings
from app.services.telegram_ingestion import authorized_message
from app.services.queries import answer_chunks
from app.services.audio import authorized_audio


class PollingError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, client: httpx.Client, token: str):
        self.client = client
        self._base_url = "https://api.telegram.org/bot" + token + "/"

    def call(self, method: str, payload: dict | None = None):
        try:
            response = self.client.post(self._base_url + method, json=payload or {})
            if response.status_code in {401, 403}:
                raise PollingError("Telegram rechazó las credenciales o los permisos del bot.")
            if response.status_code == 409:
                raise PollingError("Telegram detectó otro polling o un webhook. Detén el otro proceso; no se cambia el webhook.")
            if response.status_code == 429:
                raise PollingError("Telegram limitó temporalmente las solicitudes. Espera antes de reiniciar.")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
                raise ValueError
            return data["result"]
        except PollingError:
            raise
        except Exception:
            raise PollingError("No se pudo completar la solicitud a Telegram; detalles sensibles omitidos.") from None

    def ensure_no_webhook(self):
        info = self.call("getWebhookInfo")
        if not isinstance(info, dict):
            raise PollingError("Respuesta de Telegram no válida.")
        if info.get("url"):
            raise PollingError("El bot tiene un webhook activo. Se detiene el polling sin modificarlo.")


def forward_update(client: httpx.Client, api_url: str, token: str, update: dict) -> dict:
    try:
        response = client.post(
            api_url + "/telegram/updates", json=update,
            headers={"Authorization": "Bearer " + token},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("status") not in {"saved", "duplicate", "ignored", "answered"}:
            raise ValueError
        return result
    except Exception:
        raise PollingError("FastAPI no confirmó la recepción. Verifica servidor, migraciones y configuración; el mensaje se reintentará.") from None


def process_saved_source(client, api_url, token, source_id):
    try:
        response = client.post(
            api_url + "/sources/" + source_id + "/process",
            headers={"Authorization": "Bearer " + token}, timeout=600,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") not in {"processed", "already_processed"}:
            raise ValueError
        return result
    except Exception:
        print("Fuente guardada; procesamiento no completado. Puedes reintentarlo con python -m app.process.")
        return None


def process_update(client, telegram, api_url, token, user_id, update, processing=False):
    if authorized_message(update, user_id) is None and authorized_audio(update, user_id) is None:
        return
    result = forward_update(client, api_url, token, update)
    if result["status"] == "answered":
        for chunk in answer_chunks(result["answer"]):
            telegram.call("sendMessage", {"chat_id": user_id, "text": chunk})
        return
    extraction = None
    if processing and result["status"] in {"saved", "duplicate"}:
        extraction = process_saved_source(client, api_url, token, result["source_id"])
    if result["status"] != "saved":
        return
    label = (extraction or {}).get("project_name") or result.get("project_name") or "sin proyecto identificado"
    receipt = ("Audio original guardado: " if result.get("media_type") == "audio" else "Nota guardada: ") + label + ".\nFuente: " + result["source_id"]
    if processing:
        if extraction is None:
            receipt += "\nProcesamiento pendiente; la fuente original se conserva."
        else:
            if extraction.get("transcript_source_id"):
                receipt += "\nTranscripción: " + extraction["transcript_source_id"]
            receipt += ("\nTareas: " + str(extraction["tasks_count"])
                        + " | Decisiones: " + str(extraction["decisions_count"])
                        + "\nResumen: " + extraction["summary"][:1000])
    try:
        telegram.call("sendMessage", {"chat_id": user_id, "text": receipt})
    except PollingError:
        # La confirmación en Telegram es de mejor esfuerzo: la fuente ya está guardada.
        print("Nota guardada; no se pudo enviar la confirmación en Telegram.")


def poll_once(client, telegram, api_url, token, user_id, offset, processing=False):
    payload = {"timeout": 30, "allowed_updates": ["message", "edited_message"]}
    if offset is not None:
        payload["offset"] = offset
    updates = telegram.call("getUpdates", payload)
    if not isinstance(updates, list):
        raise PollingError("Respuesta de polling no válida.")
    for update in updates:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            raise PollingError("Update de Telegram no válido; offset conservado.")
        process_update(client, telegram, api_url, token, user_id, update, processing=processing)
        offset = update["update_id"] + 1
    return offset


def run(port: int, processing=False):
    # HTTPX incluye la URL (y el token de Telegram) en sus logs de solicitudes.
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
    token, user_id = get_settings().telegram_credentials()
    if processing:
        get_settings().llm_credentials()
    api_url = "http://127.0.0.1:" + str(port)
    # Sin proxies de entorno, redirecciones ni URLs configurables que filtren el token.
    with httpx.Client(timeout=40, trust_env=False, follow_redirects=False) as client:
        telegram = TelegramAPI(client, token)
        telegram.ensure_no_webhook()
        print("Telegram texto y audio activos. Solo se acepta el usuario autorizado en chat privado. Ctrl+C para detener.")
        offset = None
        while True:
            try:
                offset = poll_once(client, telegram, api_url, token, user_id, offset, processing=processing)
            except PollingError as exc:
                if str(exc).startswith("FastAPI no confirmó"):
                    print(str(exc))
                    time.sleep(5)
                    continue
                raise


def main():
    parser = argparse.ArgumentParser(description="Receptor local de texto Telegram para Personal Brain")
    parser.add_argument("--port", type=int, default=8000, help="Puerto de FastAPI en localhost (8000)")
    parser.add_argument("--process", action="store_true", help="Procesar textos guardados con Claude (consume API)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("El puerto debe estar entre 1 y 65535.")
    try:
        run(args.port, processing=args.process)
    except KeyboardInterrupt:
        print("Receptor Telegram detenido.")
    except PollingError as exc:
        raise SystemExit(str(exc)) from None
    except ValueError:
        # No imprimir errores de Settings ni trazas que puedan contener credenciales.
        raise SystemExit("Receptor detenido. Revisa las variables TELEGRAM y LLM, conexión y que no haya otro polling/webhook.") from None


if __name__ == "__main__":
    main()
