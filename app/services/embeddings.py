"""OpenRouter embeddings: validación de cardinalidad, dimensión y valores."""
import logging
import math
import httpx


class EmbeddingError(RuntimeError):
    pass


def embedding_ready(settings):
    return bool(settings.openrouter_embedding_model and settings.openrouter_api_key
                and settings.openrouter_api_key.get_secret_value().strip())


def embed_texts(texts, settings, client=None):
    if not embedding_ready(settings):
        raise EmbeddingError("Configura OPENROUTER_EMBEDDING_MODEL y OPENROUTER_API_KEY.")
    if not texts or len(texts) > 16 or any(not t.strip() or len(t) > 7000 for t in texts):
        raise EmbeddingError("Entrada de embeddings fuera de los límites.")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
        logging.getLogger(name).propagate = False

    def request(http):
        try:
            response = http.post("https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": "Bearer " + settings.openrouter_api_key.get_secret_value()},
                json={"model": settings.openrouter_embedding_model, "input": texts, "encoding_format": "float"})
            response.raise_for_status()
            payload = response.json()
            rows = payload["data"]
            if len(rows) != len(texts) or {r["index"] for r in rows} != set(range(len(texts))):
                raise ValueError
            vectors = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
            for vector in vectors:
                if not isinstance(vector, list) or len(vector) != settings.openrouter_embedding_dimensions:
                    raise ValueError
                if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector) or not any(vector):
                    raise ValueError
            return vectors
        except Exception:
            raise EmbeddingError("OpenRouter no devolvió embeddings válidos; los chunks se conservan para reintentar.") from None
    if client is not None:
        return request(client)
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as http:
        return request(http)
