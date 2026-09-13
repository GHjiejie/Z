"""Small OpenAI-compatible client for the local LiteLLM Gateway."""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# Prefer a local use_gateway/.env, then fall back to the repository .env used
# by the other demos. Existing environment variables always win.
load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).parents[1] / ".env")

client = OpenAI(
    api_key=os.getenv("LITELLM_MASTER_KEY", "sk-demo-master-key"),
    base_url=os.getenv("LITELLM_BASE_URL", "http://localhost:4000"),
)

request = {
    "model": os.getenv("LITELLM_MODEL", "gateway-model"),
    "messages": [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "请用一句话介绍 LiteLLM Gateway。"},
    ],
}

# Some hosted reasoning models only accept their provider default temperature.
# Set LITELLM_TEMPERATURE explicitly when the upstream supports it.
if temperature := os.getenv("LITELLM_TEMPERATURE"):
    request["temperature"] = float(temperature)

response = client.chat.completions.create(
    **request,
)

print(response.choices[0].message.content)
