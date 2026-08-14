import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
if api_key is None:
    raise ValueError("OPENAI_API_KEY environment variable is not set.")

api_base_url = os.getenv("OPENAI_BASE_URL")
if api_base_url is None:
    raise ValueError("OPENAI_BASE_URL environment variable is not set.")


chat_model = ChatOpenAI(
    temperature=1,
    model="k3",
    streaming=True,
    api_key=SecretStr(api_key),
    base_url=api_base_url,
)
