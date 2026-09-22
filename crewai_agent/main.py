"""Command-line entry points for chat and requirements analysis."""

import argparse
import os
import sys
from pathlib import Path

from config import PROJECT_DIR, load_llm_config
from crew import build_crew, build_requirements_crew
from reports import (
    create_run,
    read_requirement,
    report_session,
    validate_text,
    write_report,
)

DEFAULT_TASK = "请用中文简要介绍 AI Agent 的作用，并给出一个实际应用示例。"
EXAMPLE_FILE = PROJECT_DIR / "examples" / "task_manager.md"


def resolve_input(args: argparse.Namespace) -> tuple[str, str]:
    """Explicit CLI input overrides Make/environment inputs."""
    task, filename = args.task, args.input_file
    if task is None and filename is None:
        task = os.environ.get("TASK") or None
        filename = os.environ.get("INPUT") or None
    if task is not None and filename is not None:
        raise ValueError("TASK 和 INPUT 只能指定一个，请选择文字或文件作为输入。")
    if filename is not None:
        path = Path(filename).expanduser()
        return read_requirement(path), str(path.resolve())
    if task is not None:
        return validate_text(task), "直接输入"
    if args.mode == "requirements":
        return read_requirement(EXAMPLE_FILE), str(EXAMPLE_FILE)
    return DEFAULT_TASK, "默认示例任务"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行通用 Agent 或三角色需求分析流程。"
    )
    parser.add_argument("--mode", choices=("chat", "requirements"), default="chat")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--task", help="直接输入任务或需求")
    source.add_argument("--input-file", help="读取 UTF-8 编码的 .md/.txt 文件")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("OUTPUT_DIR") or PROJECT_DIR / "outputs"),
        help="需求分析报告保存目录，每次运行创建独立子目录",
    )
    parser.add_argument(
        "--check", action="store_true", help="检查输入和配置，不调用模型"
    )
    args = parser.parse_args(argv)

    try:
        task, source_label = resolve_input(args)
        config = load_llm_config()
    except (ValueError, OSError) as error:
        print(f"输入或配置错误：{error}", file=sys.stderr)
        return 2

    run_dir = None
    try:
        if args.mode == "requirements":
            if not args.check:
                run_dir = create_run(args.output_dir, task)
            crew = build_requirements_crew(task, config)
        else:
            crew = build_crew(task, config)

        if args.check:
            print(
                f"输入、模型配置和 {len(crew.agents)} 个 Agent 构造检查通过；"
                "未调用模型，未写入报告。"
            )
            return 0

        if run_dir is not None:
            print(
                f"开始需求分析 → 方案设计 → 方案评审。输出目录：{run_dir}", flush=True
            )
        else:
            print("Agent 正在执行任务…", flush=True)
        with report_session(run_dir):
            result = crew.kickoff()
        if run_dir is not None:
            report = write_report(run_dir, result, source_label, config["model"])
            print(f"报告已生成：{report}")
        else:
            print(result.raw)
        return 0
    except Exception as error:  # noqa: BLE001 -- CLI boundary reports failures and saved work.
        message = str(error).replace(config["api_key"], "***")
        print(f"运行失败（{type(error).__name__}）：{message}", file=sys.stderr)
        if run_dir is not None:
            print(
                f"原始输入和已完成阶段保留在：{run_dir}；本次没有生成完整报告。",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n任务已取消。已完成的分析阶段保留在本次输出目录。", file=sys.stderr)
        sys.exit(130)
