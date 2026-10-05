import base64
import json
import unittest
from unittest.mock import patch

import httpx

from app.config import Settings
from app.services.transcription import TranscriptionError, transcribe_bytes


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {}, clear=True):
            self.settings = Settings(_env_file=None, OPENROUTER_API_KEY="secret-test-key")

    def call(self, response, **kwargs):
        def transport(request):
            if isinstance(response, Exception):
                raise response
            return response
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            return transcribe_bytes(b"OggS-original", self.settings, client=client, **kwargs)

    def test_request_preserves_audio_and_uses_separate_key_and_spanish(self):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json={"text": " SIMA: enviar informe. ",
                "usage": {"seconds": 12, "cost": 0.000036, "total_tokens": 20,
                          "secret": "must-not-store"}})
        usage = {}
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            text = transcribe_bytes(b"OggS-original", self.settings, usage=usage, client=client)
        request = requests[0]
        body = json.loads(request.content)
        self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/audio/transcriptions")
        self.assertEqual(request.headers["authorization"], "Bearer secret-test-key")
        self.assertEqual(body["model"], "openai/whisper-large-v3-turbo")
        self.assertEqual(body["language"], "es")
        self.assertEqual(body["input_audio"]["format"], "ogg")
        self.assertEqual(base64.b64decode(body["input_audio"]["data"]), b"OggS-original")
        self.assertEqual(text, "SIMA: enviar informe.")
        self.assertEqual(usage["cost"], 0.000036)
        self.assertNotIn("secret", usage)

    def test_missing_key_never_calls_network(self):
        self.settings.openrouter_api_key = None
        with patch("app.services.transcription.httpx.Client") as client:
            with self.assertRaisesRegex(TranscriptionError, "OPENROUTER_API_KEY"):
                transcribe_bytes(b"audio", self.settings)
        client.assert_not_called()

    def test_errors_are_safe_and_actionable(self):
        for status, expected in ((401, "autorización"), (403, "autorización"),
                                 (402, "saldo"), (429, "límite"), (500, "no pudo")):
            with self.subTest(status=status):
                with self.assertRaises(TranscriptionError) as caught:
                    self.call(httpx.Response(status, json={"error": "secret-test-key"}))
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("secret-test-key", str(caught.exception))

    def test_timeouts_do_not_expose_key(self):
        with self.assertRaises(TranscriptionError) as caught:
            self.call(httpx.ReadTimeout("secret-test-key"))
        self.assertNotIn("secret-test-key", str(caught.exception))

    def test_invalid_results_and_duration_fail_without_accepting_transcript(self):
        for body in ({}, {"text": ""}, {"text": 7}, {"text": "x", "usage": {"seconds": 601}}):
            with self.subTest(body=body):
                with self.assertRaises(TranscriptionError):
                    self.call(httpx.Response(200, json=body))
        with self.assertRaises(TranscriptionError):
            self.call(httpx.Response(200, text="not json"))

    def test_unsupported_format_and_oversize_do_not_call_network(self):
        for data, mime in ((b"audio", "application/octet-stream"),
                           (b"a" * (self.settings.audio_max_bytes + 1), "audio/ogg"),
                           (b"", "audio/ogg")):
            with patch("app.services.transcription.httpx.Client") as client:
                with self.assertRaises(TranscriptionError):
                    transcribe_bytes(data, self.settings, mime_type=mime)
            client.assert_not_called()

    def test_wav_header_overrides_missing_mime(self):
        def transport(request):
            self.assertEqual(json.loads(request.content)["input_audio"]["format"], "wav")
            return httpx.Response(200, json={"text": "Hola"})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            self.assertEqual(transcribe_bytes(b"RIFF0000WAVEdata", self.settings,
                                             mime_type=None, client=client), "Hola")


if __name__ == "__main__":
    unittest.main()
