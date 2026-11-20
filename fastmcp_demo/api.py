"""Serve the sprint tasks and publish their OpenAPI contract."""

from collections import Counter
from typing import Literal, TypedDict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp import McpError
from openai import APIError, APIStatusError, APITimeoutError
from openai.types.responses import Response

from fastmcp_demo.responses import ResponseRequest, generate_response

type TaskStatus = Literal["todo", "in_progress", "done"]
RESPONSES_PATH = "/v1/responses"


class TaskSummary(TypedDict):
    id: str
    title: str
    status: TaskStatus


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
def list_tasks(status: TaskStatus | None = None) -> list[TaskSummary]:
    """按状态筛选迭代任务。省略状态时返回全部任务的编号、标题和状态。"""
    return [
        {"id": task["id"], "title": task["title"], "status": task["status"]}
        for task in TASKS
        if status is None or status == task["status"]
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


def responses_error(
    status_code: int, message: str, *, parameter: str | None = None
) -> JSONResponse:
    error_type = {400: "invalid_request_error", 429: "rate_limit_error"}.get(
        status_code, "server_error"
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": parameter,
                "code": None,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    if getattr(request.scope.get("route"), "path", None) != RESPONSES_PATH:
        return await request_validation_exception_handler(request, error)

    issue = error.errors()[0]
    parameter = ".".join(str(part) for part in issue["loc"][1:]) or None
    return responses_error(400, issue["msg"], parameter=parameter)


@app.post(
    RESPONSES_PATH,
    operation_id="create_response",
    summary="创建模型响应",
    response_model=Response,
    response_model_exclude_unset=True,
    responses={
        "4XX": {"description": "请求参数无效或模型供应商限流"},
        "5XX": {"description": "Agent、MCP 或模型供应商请求失败"},
    },
)
async def create_response(request: ResponseRequest) -> Response | JSONResponse:
    """通过 MCP 执行模型的工具调用，返回最终的非流式 Responses 对象。"""
    try:
        return await generate_response(request)
    except APITimeoutError:
        return responses_error(504, "模型供应商请求超时。")
    except APIStatusError as error:
        if error.status_code == 429:
            return responses_error(429, "模型供应商正在限流。")
        return responses_error(502, f"模型供应商返回 HTTP {error.status_code}。")
    except APIError:
        return responses_error(502, "模型供应商请求失败。")
    except ValueError:
        return responses_error(503, "Agent 服务配置不可用。")
    except (httpx.HTTPError, McpError, RuntimeError):
        return responses_error(502, "MCP 调用或 Agent 执行未完成。")
