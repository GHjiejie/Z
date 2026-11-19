"""Sample sprint API and the OpenAPI contract consumed by FastMCP."""

from collections import Counter
from typing import Literal, TypedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from fastmcp_demo.responses import ResponseRequest, generate_response

type TaskStatus = Literal["todo", "in_progress", "done"]


class TaskSummary(TypedDict):
    id: str
    title: str
    status: TaskStatus


class TaskListItem(TaskSummary):
    uri: str


class Task(TaskSummary):
    description: str


TASKS: tuple[Task, ...] = (
    {
        "id": "TASK-101",
        "title": "更新通知中心使用说明",
        "status": "done",
        "description": "补充通知筛选、标记已读和消息详情的操作说明。",
    },
    {
        "id": "TASK-102",
        "title": "验证通知列表分页",
        "status": "in_progress",
        "description": "验证加载下一页时保留已选筛选条件，并检查重复通知。",
    },
    {
        "id": "TASK-103",
        "title": "补充无通知时的提示",
        "status": "todo",
        "description": "为首次打开和筛选无结果两种情况编写提示文案。",
    },
    {
        "id": "TASK-104",
        "title": "复核发布前验收清单",
        "status": "todo",
        "description": "核对通知列表、详情和标记已读的验收结果。",
    },
)

app = FastAPI(title="Sprint Tasks API")


@app.get("/tasks", operation_id="list_tasks", summary="列出迭代任务")
def list_tasks(status: TaskStatus | None = None) -> list[TaskListItem]:
    """可按状态筛选。每条结果含有详情 URI。"""
    return [
        {
            "id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "uri": f"tasks://item/{task['id']}",
        }
        for task in TASKS
        if status is None or task["status"] == status
    ]


@app.get("/tasks/summary", operation_id="summarize_tasks", summary="统计任务数量")
def summarize_tasks() -> dict[str, int]:
    """返回任务总数以及各状态的任务数量。"""
    counts = Counter(task["status"] for task in TASKS)
    return {
        "total": len(TASKS),
        "todo": counts["todo"],
        "in_progress": counts["in_progress"],
        "done": counts["done"],
    }


@app.get("/tasks/{task_id}", operation_id="get_task", summary="读取任务详情")
def get_task(task_id: str) -> Task:
    """按任务编号返回任务记录和详细说明。"""
    for task in TASKS:
        if task["id"] == task_id:
            return task
    raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在。")


@app.post("/v1/responses", response_class=JSONResponse, summary="创建模型响应")
async def create_response(request: ResponseRequest) -> JSONResponse:
    """Run the configured model with the generated task tools."""
    # The MCP server reads this app's OpenAPI schema during initialization.
    from fastmcp_demo.server import mcp

    response = await generate_response(request, mcp)
    return JSONResponse(response.model_dump(mode="json", exclude_unset=True))
