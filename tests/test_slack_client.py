"""슬랙 통신 모의 테스트.

실행: python -m unittest discover -s tests -v
실제 토큰, DB, SECRET_KEY, 외부 네트워크 연결은 사용하지 않는다.
"""

import json
import secrets
import unittest

import httpx

from app.slack_client import SlackClient, SlackError


class SlackClientTests(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)

    def test_lookup_sends_to_fixed_host_with_authorization(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), "https://slack.com/api/users.lookupByEmail")
            self.assertEqual(
                request.headers["Authorization"], f"Bearer {self.token}"
            )
            self.assertEqual(
                json.loads(request.content), {"email": "staff@example.com"}
            )
            return httpx.Response(200, json={
                "ok": True,
                "user": {"id": "U01ABCDEF", "deleted": False},
            })

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            self.assertEqual(
                client.lookup_user_id(" staff@example.com "),
                "U01ABCDEF",
            )
        self.assertEqual(len(requests), 1)

    def test_dm_opens_conversation_then_posts_plain_text(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            payload = json.loads(request.content)
            if request.url.path == "/api/conversations.open":
                self.assertEqual(payload, {"users": "U01ABCDEF"})
                return httpx.Response(200, json={
                    "ok": True, "channel": {"id": "D01ABCDEF"}
                })

            self.assertEqual(request.url.path, "/api/chat.postMessage")
            self.assertEqual(payload["channel"], "D01ABCDEF")
            self.assertEqual(payload["text"], "프로젝트 배정 안내")
            self.assertFalse(payload["mrkdwn"])
            self.assertEqual(payload["parse"], "none")
            self.assertFalse(payload["unfurl_links"])
            self.assertFalse(payload["unfurl_media"])
            return httpx.Response(200, json={
                "ok": True, "ts": "1234567890.123456"
            })

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            timestamp = client.send_dm("U01ABCDEF", "프로젝트 배정 안내")

        self.assertEqual(timestamp, "1234567890.123456")
        self.assertEqual(
            calls, ["/api/conversations.open", "/api/chat.postMessage"]
        )

    def test_missing_token_is_rejected(self):
        with self.assertRaises(SlackError) as caught:
            SlackClient(" ")
        self.assertEqual(caught.exception.code, "not_configured")

    def test_invalid_inputs_do_not_make_requests(self):
        def handler(request):
            self.fail("잘못된 입력에 네트워크 요청이 발생했습니다.")

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            for email in ["", "invalid", "a" * 256 + "@example.com"]:
                with self.subTest(email=email):
                    with self.assertRaises(SlackError):
                        client.lookup_user_id(email)

            for user_id, text in [
                ("invalid", "안내"),
                ("U01ABCDEF", ""),
                ("U01ABCDEF", " " * 5),
                ("U01ABCDEF", "a" * 4001),
            ]:
                with self.subTest(user_id=user_id, length=len(text)):
                    with self.assertRaises(SlackError):
                        client.send_dm(user_id, text)

    def test_api_errors_are_sanitized(self):
        for external_code, expected in [
            ("missing_scope", "missing_scope"),
            ("users_not_found", "users_not_found"),
            (self.token, "api_error"),
            ({"unexpected": "value"}, "api_error"),
        ]:
            with self.subTest(expected=expected):
                def handler(request):
                    return httpx.Response(200, json={
                        "ok": False, "error": external_code
                    })

                with SlackClient(
                    self.token, transport=httpx.MockTransport(handler)
                ) as client:
                    with self.assertRaises(SlackError) as caught:
                        client.lookup_user_id("staff@example.com")
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn(self.token, str(caught.exception))

    def test_http_errors_and_redirects_are_not_retried(self):
        for status, code in [
            (429, "ratelimited"),
            (503, "service_unavailable"),
            (403, "http_error"),
            (302, "http_error"),
        ]:
            with self.subTest(status=status):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(
                        status,
                        headers={"Location": "https://example.com/"},
                        text="외부 오류 본문",
                    )

                with SlackClient(
                    self.token, transport=httpx.MockTransport(handler)
                ) as client:
                    with self.assertRaises(SlackError) as caught:
                        client.lookup_user_id("staff@example.com")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(requests), 1)

    def test_timeout_and_network_failure_are_sanitized(self):
        for error_type, code in [
            (httpx.ReadTimeout, "timeout"),
            (httpx.ConnectError, "network_error"),
        ]:
            with self.subTest(code=code):
                requests = []

                def handler(request):
                    requests.append(request)
                    raise error_type(self.token, request=request)

                with SlackClient(
                    self.token, transport=httpx.MockTransport(handler)
                ) as client:
                    with self.assertRaises(SlackError) as caught:
                        client.lookup_user_id("staff@example.com")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(self.token, str(caught.exception))
                self.assertEqual(len(requests), 1)

    def test_invalid_lookup_responses_are_rejected(self):
        responses = [
            [],
            {"ok": "true", "user": {"id": "U01ABCDEF"}},
            {"ok": True},
            {"ok": True, "user": {"id": "invalid"}},
            {"ok": True, "user": {"id": "U01ABCDEF", "deleted": True}},
            {"ok": True, "user": {"id": "U01ABCDEF", "is_bot": True}},
        ]
        for payload in responses:
            with self.subTest(payload=payload):
                def handler(request):
                    return httpx.Response(200, json=payload)

                with SlackClient(
                    self.token, transport=httpx.MockTransport(handler)
                ) as client:
                    with self.assertRaises(SlackError):
                        client.lookup_user_id("staff@example.com")

    def test_non_json_response_is_rejected(self):
        def handler(request):
            return httpx.Response(200, text="<html>error</html>")

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SlackError) as caught:
                client.lookup_user_id("staff@example.com")
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_failed_conversation_open_does_not_post_message(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={
                "ok": False, "error": "missing_scope"
            })

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SlackError):
                client.send_dm("U01ABCDEF", "안내")
        self.assertEqual(calls, ["/api/conversations.open"])

    def test_message_failure_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/api/conversations.open":
                return httpx.Response(200, json={
                    "ok": True, "channel": {"id": "D01ABCDEF"}
                })
            return httpx.Response(200, json={
                "ok": False, "error": "channel_not_found"
            })

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SlackError) as caught:
                client.send_dm("U01ABCDEF", "안내")
        self.assertEqual(caught.exception.code, "channel_not_found")
        self.assertEqual(len(calls), 2)

    def test_invalid_dm_channel_is_rejected(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={
                "ok": True, "channel": {"id": "C01ABCDEF"}
            })

        with SlackClient(
            self.token, transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SlackError):
                client.send_dm("U01ABCDEF", "안내")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
