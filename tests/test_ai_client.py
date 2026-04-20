from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import ai_client


class AIClientTests(unittest.TestCase):
    def test_gemini_uses_native_generate_content_endpoint(self) -> None:
        captured: dict[str, object] = {}

        def fake_post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": '{"ok": true}'},
                            ]
                        }
                    }
                ]
            }

        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-key",
                "GEMINI_MODEL": "gemini-2.5-flash",
            },
            clear=False,
        ):
            with patch("ai_client._post_json", side_effect=fake_post_json):
                completion = ai_client.request_json_completion("system", "user", preferred_provider="gemini")

        self.assertEqual(completion.provider, "gemini")
        self.assertEqual(completion.text, '{"ok": true}')
        self.assertEqual(
            captured["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(captured["headers"], {"Content-Type": "application/json", "x-goog-api-key": "test-key"})
        self.assertEqual(
            captured["payload"],
            {
                "systemInstruction": {"parts": [{"text": "system"}]},
                "contents": [{"role": "user", "parts": [{"text": "user"}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
        )

    def test_gemini_accepts_explicit_model_resource_name(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-key",
                "GEMINI_MODEL": "gemini-2.5-flash",
            },
            clear=False,
        ):
            with patch(
                "ai_client._post_json",
                return_value={"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]},
            ) as post_json:
                ai_client.request_json_completion(
                    "system",
                    "user",
                    preferred_provider="gemini",
                    model_override="models/gemini-2.5-pro",
                )

        self.assertEqual(
            post_json.call_args.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
        )

    def test_gemini_uses_google_api_key_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_KEY": "google-key",
                "GEMINI_MODEL": "gemini-2.5-flash",
            },
            clear=True,
        ):
            with patch("ai_client._secret", return_value=None):
                with patch(
                    "ai_client._post_json",
                    return_value={"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]},
                ) as post_json:
                    ai_client.request_json_completion("system", "user", preferred_provider="gemini")

        self.assertEqual(post_json.call_args.kwargs["headers"]["x-goog-api-key"], "google-key")

    def test_gemini_surfaces_prompt_block_reason(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-key",
                "GEMINI_MODEL": "gemini-2.5-flash",
            },
            clear=False,
        ):
            with patch(
                "ai_client._post_json",
                return_value={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
            ):
                with self.assertRaisesRegex(RuntimeError, "Gemini blocked the prompt: SAFETY"):
                    ai_client.request_json_completion("system", "user", preferred_provider="gemini")


if __name__ == "__main__":
    unittest.main()
