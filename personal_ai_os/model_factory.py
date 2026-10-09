"""The only production model entry point."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agno.models.deepseek import DeepSeek

from .config import ALLOWED_DEEPSEEK_MODELS, AppConfig


@dataclass
class MeteredDeepSeek(DeepSeek):
    """Count actual provider requests, including tool turns and failed requests."""

    request_usage: list[dict] = field(default_factory=list, init=False, repr=False)

    def invoke(self, *args, **kwargs):
        started = time.monotonic()
        response = None
        try:
            response = super().invoke(*args, **kwargs)
            return response
        finally:
            usage = response.response_usage if response is not None else None
            self.request_usage.append({
                "model_id": self.id,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
            })


def make_deepseek_model(config: AppConfig) -> MeteredDeepSeek:
    if config.deepseek_model_id not in ALLOWED_DEEPSEEK_MODELS:
        raise ValueError("Only approved DeepSeek models are allowed")
    if not config.deepseek_api_key:
        raise ValueError("DEEPSEEK_API_KEY is required for real model calls")
    return MeteredDeepSeek(
        id=config.deepseek_model_id,
        api_key=config.deepseek_api_key,
        use_thinking=False,
        timeout=config.model_timeout_seconds,
        max_retries=0,
        retry_with_guidance=False,
    )
