"""Expose the sprint API as tools generated from its OpenAPI contract."""

import httpx
from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.providers.openapi import MCPType, RouteMap

from fastmcp_demo.api import Task, app, get_task

api_client = httpx.AsyncClient(
    transport=httpx.ASGITransport(app=app),
    base_url="http://sprint-tasks",
)
mcp = FastMCP.from_openapi(
    openapi_spec=app.openapi(),
    client=api_client,
    name="Sprint Task Assistant",
    route_maps=[RouteMap(pattern=r"^/v1/responses$", mcp_type=MCPType.EXCLUDE)],
)


@mcp.resource("tasks://status-guide", mime_type="text/plain")
def status_guide() -> str:
    """解释迭代任务的状态值。"""
    return "todo（待处理）\nin_progress（进行中）\ndone（已完成）"


@mcp.resource("tasks://item/{task_id}", mime_type="application/json")
def task_detail(task_id: str) -> Task:
    """按任务编号读取详情，包括任务说明。"""
    try:
        return get_task(task_id)
    except HTTPException as error:
        raise ResourceError(str(error.detail)) from error


@mcp.prompt
def sprint_brief(goal: str) -> str:
    """为给定的迭代目标生成简报提示。"""
    return (
        f"请围绕「{goal}」撰写一份迭代简报。\n"
        "先调用 summarize_tasks 获取状态统计，再用 list_tasks 分别查询 todo 和 in_progress。"
        "需要更多上下文时，读取任务条目里的详情 URI。\n"
        "根据任务数据整理当前进展、下一步和已知风险。"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
