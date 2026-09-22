from dataclasses import dataclass
from typing import Any
import httpx

from app.config import get_settings


class N8nError(Exception):
    """Base exception for n8n integration errors."""
    pass


class N8nClientError(N8nError):
    def __init__(self, message: str, status_code: int = 502, error_code: str = "AI_GATEWAY_ERROR") -> None:
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


class N8nTimeoutError(N8nClientError):
    def __init__(self, message: str = "Timed out waiting for response from n8n AI workflow.") -> None:
        super().__init__(message=message, status_code=504, error_code="AI_GATEWAY_TIMEOUT")


class N8nConnectionError(N8nClientError):
    def __init__(self, message: str = "Failed to connect to n8n AI workflow gateway.") -> None:
        super().__init__(message=message, status_code=502, error_code="AI_GATEWAY_CONNECTION_ERROR")


class N8nMalformedResponseError(N8nClientError):
    def __init__(self, message: str = "n8n returned a malformed or invalid response.") -> None:
        super().__init__(message=message, status_code=502, error_code="AI_GATEWAY_MALFORMED_RESPONSE")


class N8nExecutionError(N8nClientError):
    def __init__(self, message: str = "n8n AI execution failed.") -> None:
        super().__init__(message=message, status_code=502, error_code="AI_EXECUTION_FAILED")


class N8nClient:
    """Client for triggering DevMeeting AI workflows in n8n via webhook."""

    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self.webhook_url = webhook_url or settings.n8n_webhook_url
        self.webhook_secret = webhook_secret or settings.devmeet_webhook_secret
        self.timeout = timeout if timeout is not None else settings.n8n_timeout_seconds
        self.http_client = http_client

    def _get_client(self) -> httpx.Client:
        if self.http_client is not None:
            return self.http_client
        return httpx.Client(timeout=self.timeout)

    def trigger_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Sends an execution payload to the n8n webhook and returns the parsed JSON result.
        """
        if not self.webhook_url:
            raise N8nClientError("n8n webhook URL is not configured.")

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Secret": self.webhook_secret or "",
        }

        client = self._get_client()
        should_close = self.http_client is None

        try:
            response = client.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise N8nTimeoutError(f"Request to n8n timed out after {self.timeout}s: {e}") from e
        except httpx.RequestError as e:
            raise N8nConnectionError(f"Network error connecting to n8n at {self.webhook_url}: {e}") from e
        finally:
            if should_close:
                client.close()

        if response.status_code >= 400:
            try:
                err_data = response.json()
                msg = err_data.get("message") or err_data.get("error") or response.text
            except Exception:
                msg = response.text
            raise N8nClientError(
                message=f"n8n returned HTTP {response.status_code}: {msg}",
                status_code=502,
                error_code="AI_GATEWAY_ERROR",
            )

        try:
            data = response.json()
        except Exception as e:
            raise N8nMalformedResponseError(f"Failed to parse n8n response as JSON: {e}") from e

        if not isinstance(data, dict):
            raise N8nMalformedResponseError("n8n response root must be a JSON object.")

        return data
