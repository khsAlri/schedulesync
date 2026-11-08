"""슬랙 사용자 조회 및 DM 발송.

DB나 애플리케이션 설정을 직접 읽지 않는다.
호출자가 환경 변수에서 읽은 Bot Token을 전달한다.

네트워크 호출은 DB 트랜잭션 밖에서 실행해야 한다.
특히 SQLite 잠금을 유지한 채 슬랙 응답을 기다리지 않는다.

자동 재시도는 하지 않는다. 발송 타임아웃은 메시지가 실제 전송되었는지
알 수 없는 상태일 수 있으므로 무조건 재발송하면 중복 알림이 생길 수 있다.
"""

import re

import httpx

_API_BASE = "https://slack.com/api/"
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,49}$")
_DM_ID_RE = re.compile(r"^D[A-Z0-9]{2,49}$")

_SAFE_ERROR_CODES = frozenset({
    "invalid_auth",
    "not_authed",
    "token_revoked",
    "account_inactive",
    "missing_scope",
    "users_not_found",
    "user_not_found",
    "user_not_visible",
    "channel_not_found",
    "cannot_dm_bot",
    "cannot_dm_self",
    "restricted_action",
    "not_in_channel",
    "is_archived",
    "ratelimited",
    "internal_error",
    "service_unavailable",
})


class SlackError(RuntimeError):
    """외부 응답이나 비밀 값을 포함하지 않는 오류."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"슬랙 처리 실패: {code}")


class SlackClient:
    """고정된 Slack API 호스트에만 요청하는 동기 클라이언트.

    테스트에서는 transport에 httpx.MockTransport를 전달한다.
    사용 후에는 with 문 또는 close()로 연결을 닫아야 한다.
    """

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        token = token.strip()
        if not token:
            raise SlackError("not_configured")

        # 토큰을 별도 공개 속성이나 객체 표현에 넣지 않는다.
        self._http = httpx.Client(
            base_url=_API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, payload: dict) -> dict:
        try:
            response = self._http.post(method, json=payload)
        except httpx.TimeoutException:
            raise SlackError("timeout") from None
        except httpx.RequestError:
            raise SlackError("network_error") from None

        if response.status_code == 429:
            raise SlackError("ratelimited")
        if response.status_code >= 500:
            raise SlackError("service_unavailable")
        if response.status_code != 200:
            # 리다이렉트도 실패 처리한다. 다른 호스트로 토큰을 보내지 않는다.
            raise SlackError("http_error")

        try:
            data = response.json()
        except ValueError:
            raise SlackError("invalid_response") from None

        if not isinstance(data, dict):
            raise SlackError("invalid_response")

        if data.get("ok") is not True:
            code = data.get("error")
            if not isinstance(code, str) or code not in _SAFE_ERROR_CODES:
                code = "api_error"
            raise SlackError(code)

        return data

    def lookup_user_id(self, email: str) -> str:
        """users:read.email 권한으로 이메일에 대응하는 활성 사용자를 찾는다."""
        email = email.strip()
        if not email or len(email) > 255 or "@" not in email:
            raise SlackError("invalid_email")

        data = self._call("users.lookupByEmail", {"email": email})
        user = data.get("user")
        if not isinstance(user, dict):
            raise SlackError("invalid_response")
        if user.get("deleted") is True:
            raise SlackError("account_inactive")
        if user.get("is_bot") is True:
            raise SlackError("bot_user")

        user_id = user.get("id")
        if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
            raise SlackError("invalid_response")
        return user_id

    def send_dm(self, user_id: str, text: str) -> str:
        """DM을 열고 발송한 메시지의 ts를 반환한다.

        필요한 권한: im:write, chat:write.
        메시지는 일반 텍스트로 보내고 자동 링크·미디어 펼침을 막는다.
        """
        if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
            raise SlackError("invalid_user_id")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise SlackError("invalid_message")

        opened = self._call("conversations.open", {"users": user_id})
        channel = opened.get("channel")
        if not isinstance(channel, dict):
            raise SlackError("invalid_response")
        channel_id = channel.get("id")
        if not isinstance(channel_id, str) or not _DM_ID_RE.fullmatch(channel_id):
            raise SlackError("invalid_response")

        sent = self._call(
            "chat.postMessage",
            {
                "channel": channel_id,
                "text": text,
                "mrkdwn": False,
                "parse": "none",
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        timestamp = sent.get("ts")
        if (
            not isinstance(timestamp, str)
            or not re.fullmatch(r"\d+\.\d+", timestamp)
        ):
            raise SlackError("invalid_response")
        return timestamp
