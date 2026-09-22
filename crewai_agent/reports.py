"""Read requirement text and persist separate, non-overwriting analysis runs."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crewai.crews.crew_output import CrewOutput
    from crewai.tasks.task_output import TaskOutput

MAX_INPUT_BYTES = 64 * 1024
STAGES = (
    ("requirements_analysis", "需求分析", "01-requirements.md"),
    ("technical_design", "技术方案", "02-design.md"),
    ("delivery_review", "方案评审与开发清单", "03-review.md"),
)
_active_run: ContextVar[Path | None] = ContextVar(
    "analysis_report_directory", default=None
)


@contextmanager
def report_session(run_dir: Path | None) -> Iterator[None]:
    """Scope the module-level callback to this synchronous kickoff only."""
    token = _active_run.set(run_dir)
    try:
        yield
    finally:
        _active_run.reset(token)


def record_stage(output: "TaskOutput") -> None:
    """Persist each stage before the next model call, using our absolute paths."""
    for index, (name, title, filename) in enumerate(STAGES, start=1):
        if output.name != name:
            continue
        run_dir = _active_run.get()
        if run_dir is not None:
            (run_dir / filename).write_text(output.raw.strip() + "\n", encoding="utf-8")
        print(f"[{index}/{len(STAGES)}] {title}完成", flush=True)
        return
    raise ValueError(f"未知的分析阶段：{output.name}")


def validate_text(text: str) -> str:
    """Reject empty or oversized inputs before sending anything to the model."""
    text = text.strip()
    if not text:
        raise ValueError("需求或任务内容不能为空。")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("输入内容超过 64 KiB，请先拆分需求。")
    return text


def read_requirement(path: Path) -> str:
    """Read a bounded UTF-8 Markdown or plain-text file supplied by the user."""
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("需求文件仅支持 UTF-8 编码的 .md 或 .txt 文件。")
    with path.open("rb") as source:
        data = source.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("需求文件超过 64 KiB，请先拆分需求。")
    try:
        return validate_text(data.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ValueError("需求文件不是有效的 UTF-8 文本。") from error


def create_run(output_root: Path, requirement: str) -> Path:
    """Reserve a unique directory before execution; retain completed stages on error."""
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S-")
    run_dir = Path(mkdtemp(prefix=prefix, dir=output_root))
    (run_dir / "00-input.md").write_text(requirement + "\n", encoding="utf-8")
    return run_dir


def write_report(run_dir: Path, result: "CrewOutput", source: str, model: str) -> Path:
    """Only publish a complete report when all three stages returned text."""
    outputs = result.tasks_output
    if len(outputs) != len(STAGES) or any(not output.raw.strip() for output in outputs):
        raise ValueError("阶段输出不完整，未生成最终报告；请查看已保存的阶段文件。")

    sections = [
        "# 需求分析与开发方案报告",
        (
            f"- 输入来源：{source}\n- 模型：{model}\n"
            f"- 生成时间：{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}"
        ),
        "本报告为模型分析和建议。假设、待澄清问题及评审结论仍需人工确认。",
        "原始输入见 [00-input.md](00-input.md)。",
    ]
    for (_, title, filename), output in zip(STAGES, outputs, strict=True):
        # record_stage also persists outputs before later stages run or fail.
        (run_dir / filename).write_text(output.raw.strip() + "\n", encoding="utf-8")
        sections.append(f"## {title}\n\n{output.raw.strip()}")

    destination = run_dir / "report.md"
    temporary = run_dir / ".report.md.tmp"
    temporary.write_text("\n\n---\n\n".join(sections) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination
