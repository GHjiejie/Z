"""Run inside the LiteLLM container; never print the master or generated key."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def bootstrap() -> Path:
    output = Path(os.getenv("GATEWAY_KEY_OUTPUT", "/bootstrap/.env.gateway"))
    if output.exists():
        raise RuntimeError(
            "Key file already exists; explicit key rotation is required."
        )
    master = os.environ["LITELLM_MASTER_KEY"]
    alias = os.environ.get("LITELLM_MODEL_ALIAS", "platform-chat")
    if not alias or alias == "*":
        raise ValueError("An explicit permitted model alias is required.")
    base = os.getenv("GATEWAY_BOOTSTRAP_URL", "http://127.0.0.1:4000").rstrip("/")

    def post(path: str, body: dict) -> dict:
        request = Request(
            base + path,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + master,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(
                f"Gateway control request {path} failed with HTTP {exc.code}; no automatic retry."
            ) from None
        except URLError:
            raise RuntimeError("Gateway unavailable; no automatic retry.") from None

    user_id = "agent-platform-runtime-" + uuid.uuid4().hex
    post(
        "/user/new",
        {
            "user_id": user_id,
            "user_alias": "Agent platform runtime",
            "user_role": "internal_user",
            "auto_create_key": False,
            "models": [alias],
        },
    )
    result = post(
        "/key/generate",
        {
            "user_id": user_id,
            "key_alias": "Agent platform runtime",
            "models": [alias],
            "key_type": "llm_api",
            "allowed_routes": ["/chat/completions", "/v1/chat/completions"],
            "max_budget": float(os.getenv("GATEWAY_KEY_MAX_BUDGET", "100")),
            "rpm_limit": int(os.getenv("GATEWAY_KEY_RPM", "60")),
            "tpm_limit": int(os.getenv("GATEWAY_KEY_TPM", "120000")),
            "max_parallel_requests": int(os.getenv("GATEWAY_KEY_CONCURRENT", "4")),
            "duration": "30d",
        },
    )
    key = result.get("key")
    if (
        not isinstance(key, str)
        or not key.startswith("sk-")
        or "\n" in key
        or key == master
    ):
        raise RuntimeError("Gateway did not return a distinct valid virtual key.")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(f"PLATFORM_LITELLM_KEY={key}\n")
    if "GATEWAY_KEY_UID" in os.environ and "GATEWAY_KEY_GID" in os.environ:
        os.chown(
            output,
            int(os.environ["GATEWAY_KEY_UID"]),
            int(os.environ["GATEWAY_KEY_GID"]),
        )
    return output


if __name__ == "__main__":
    try:
        destination = bootstrap()
    except (KeyError, ValueError, RuntimeError, OSError) as error:
        print(f"Key bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"Restricted runtime key saved to {destination}; expires in 30 days.")
