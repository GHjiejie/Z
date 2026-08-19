"""File tools exposed to the chat model.

Every path is resolved under a configured workspace root.  This keeps a model
from accidentally reading or mutating files outside the demo workspace.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import BaseTool, tool


class WorkspaceFiles:
    """Build LangChain tools bound to one workspace directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, raw_path: str) -> Path:
        """Resolve *raw_path* and reject paths escaping the workspace."""
        candidate = (self.root / raw_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError(f"路径必须位于工作区内: {raw_path}")
        if candidate == self.root:
            raise ValueError("操作目标不能是工作区根目录")
        return candidate

    def build_tools(self) -> list[BaseTool]:
        resolve = self.resolve
        root = self.root

        @tool
        def list_files(path: str = ".") -> str:
            """列出工作区目录中的文件。path 是相对工作区的目录路径。"""
            directory = root if path in ("", ".") else resolve(path)
            if not directory.exists():
                return f"目录不存在: {path}"
            if not directory.is_dir():
                return f"不是目录: {path}"
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
            if not entries:
                return "目录为空"
            return "\n".join(
                f"{'[目录]' if item.is_dir() else '[文件]'} {item.relative_to(root)}"
                for item in entries
            )

        @tool
        def read_file(path: str) -> str:
            """读取工作区内 UTF-8 文本文件。path 是相对工作区的文件路径。"""
            target = resolve(path)
            if not target.exists():
                return f"文件不存在: {path}"
            if not target.is_file():
                return f"不是文件: {path}"
            return target.read_text(encoding="utf-8")

        @tool
        def write_file(path: str, content: str, overwrite: bool = False) -> str:
            """写入工作区内的 UTF-8 文本文件；执行前必须由用户批准。

            path 是相对工作区的文件路径。默认不覆盖已有文件；只有用户明确
            要求覆盖时才把 overwrite 设为 true。
            """
            target = resolve(path)
            if target.exists() and not overwrite:
                return f"文件已存在，未写入；如需覆盖请明确说明: {path}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"已写入 {target.relative_to(root)}（{len(content)} 个字符）"

        @tool
        def delete_file(path: str) -> str:
            """删除工作区内的单个文件；执行前必须由用户批准。不能删除目录。"""
            target = resolve(path)
            if not target.exists():
                return f"文件不存在: {path}"
            if not target.is_file():
                return f"拒绝删除目录: {path}"
            target.unlink()
            return f"已删除 {target.relative_to(root)}"

        return [list_files, read_file, write_file, delete_file]


def tool_map(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {item.name: item for item in tools}


def approval_preview(
    tool_name: str,
    args: dict[str, object],
    resolve: Callable[[str], Path],
) -> dict[str, object]:
    """Create a small serializable interrupt payload for a sensitive tool."""
    raw_path = str(args.get("path", ""))
    resolved = resolve(raw_path)
    payload: dict[str, object] = {
        "kind": "file_operation_approval",
        "tool": tool_name,
        "path": raw_path,
        "resolved_path": str(resolved),
    }
    if tool_name == "write_file":
        content = str(args.get("content", ""))
        payload.update(
            {
                "overwrite": bool(args.get("overwrite", False)),
                "characters": len(content),
                "preview": content[:240],
            }
        )
    return payload
