"""Minimal ntfy.sh sender: one unguessable topic per device, generic text only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import os
import re
import ssl
from collections.abc import Callable, Mapping

from .remote_delivery import RemoteNotificationKnownFailure


HOST = "ntfy.sh"
TITLE = "Personal AI OS"
MESSAGE = "有一条任务提醒，请打开 Personal AI OS 查看。"
_DEVICE_ID = re.compile(r"[0-9a-f]{32}\Z")


class NtfyNotifier:
    def __init__(self, topic_secret: str,
                 connection_factory: Callable[[], http.client.HTTPSConnection] | None = None):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", topic_secret):
            raise ValueError("invalid_ntfy_topic_secret")
        self._secret = bytes.fromhex(topic_secret)
        self._connection_factory = connection_factory or (
            lambda: http.client.HTTPSConnection(
                HOST, timeout=8, context=ssl.create_default_context()
            )
        )

    def topic_for_device(self, device_id: str) -> str:
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ValueError("invalid_remote_device_id")
        digest = hmac.new(self._secret, device_id.encode("ascii"), hashlib.sha256).digest()
        return "paos_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def send(self, device_id: str, title: str, body: str, idempotency_key: str) -> None:
        # The caller's title/body may contain private task information. Neither those
        # values nor the device ID or idempotency key are sent to the public service.
        path = "/" + self.topic_for_device(device_id)
        payload = MESSAGE.encode("utf-8")
        connection = self._connection_factory()
        try:
            connection.request("POST", path, body=payload, headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(payload)),
                "X-Title": TITLE,
            })
            response = connection.getresponse()
            if 200 <= response.status < 300:
                return
            if 400 <= response.status < 500:
                raise RemoteNotificationKnownFailure("ntfy_rejected")
            # A server error can arrive after the provider accepted the message.
            # The dispatcher records an unknown result and never resends it.
            raise RuntimeError("ntfy_result_unknown")
        finally:
            connection.close()


def notifier_from_environment(environ: Mapping[str, str] | None = None) -> NtfyNotifier | None:
    secret = (os.environ if environ is None else environ).get("NTFY_TOPIC_SECRET", "").strip()
    return NtfyNotifier(secret) if secret else None
