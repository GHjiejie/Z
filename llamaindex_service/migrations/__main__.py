"""uv run --group rag python -m llamaindex_service.migrations"""

from llamaindex_service.config import Settings
from llamaindex_service.persistence import Repository


def main() -> None:
    repository = Repository(Settings())
    repository.create_schema()
    print("LlamaIndex service schema revision 0002 is ready.")


if __name__ == "__main__":
    main()
