"""按名称运行 create_agent 的五个核心概念 demo。"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable

try:
    from .agent_state_demo import run_demo as run_agent_state_demo
    from .concept import print_concepts
    from .model_demo import run_demo as run_model_demo
    from .structured_output_demo import run_demo as run_structured_output_demo
    from .system_prompt_demo import run_demo as run_system_prompt_demo
    from .tools_demo import run_demo as run_tools_demo
except ImportError:
    from agent_state_demo import run_demo as run_agent_state_demo
    from concept import print_concepts
    from model_demo import run_demo as run_model_demo
    from structured_output_demo import run_demo as run_structured_output_demo
    from system_prompt_demo import run_demo as run_system_prompt_demo
    from tools_demo import run_demo as run_tools_demo


DEMOS: dict[str, Callable[[], Awaitable[None]]] = {
    "model": run_model_demo,
    "tools": run_tools_demo,
    "system_prompt": run_system_prompt_demo,
    "structured_output": run_structured_output_demo,
    "agent_state": run_agent_state_demo,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("concept", nargs="?", choices=DEMOS, help="要运行的概念 demo")
    args = parser.parse_args()

    if args.concept is None:
        print_concepts()
        print("\n运行方式：python main.py <concept>")
        return

    asyncio.run(DEMOS[args.concept]())


if __name__ == "__main__":
    main()
