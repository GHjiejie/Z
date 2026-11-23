from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fastmcp_minimal_demo.settings import BACKEND_DELETE_TOKEN_ENV, configured_token

app = FastAPI()
delete_bearer = HTTPBearer(auto_error=False)


@app.get("/add", operation_id="add")
def add(a: int, b: int) -> dict[str, int]:
    """计算两个整数的和。"""
    return {"sum": a + b}


@app.get("/subtract", operation_id="subtract")
def subtract(a: int, b: int) -> dict[str, int]:
    """计算两个整数的差。"""
    return {"difference": a - b}


@app.get("/multiply", operation_id="multiply")
def multiply(a: int, b: int) -> dict[str, int]:
    """计算两个整数的积。"""
    return {"product": a * b}


@app.get("/divide", operation_id="divide")
def divide(a: int, b: int) -> dict[str, float]:
    """计算两个整数的商。"""
    if b == 0:
        return {"error": "除数不能为零。"}
    return {"quotient": a / b}


def require_delete_service(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(delete_bearer)],
) -> None:
    token = configured_token(BACKEND_DELETE_TOKEN_ENV)
    if not token:
        raise HTTPException(status_code=503, detail="删除接口的服务凭据尚未配置。")
    if credentials is None or not compare_digest(
        credentials.credentials.encode(), token.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="删除接口需要服务凭据。",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.delete(
    "/records/{record_id}",
    operation_id="delete_record",
    dependencies=[Depends(require_delete_service)],
)
async def delete_record(record_id: str) -> dict[str, str]:
    """删除指定记录。"""
    # 这里可以添加实际的删除逻辑，例如调用数据库或其他服务。
    # 目前仅返回一个模拟的响应。
    return {"status": "deleted", "record_id": record_id}


@app.delete(
    "/records_user",
    operation_id="delete_record_user",
    dependencies=[Depends(require_delete_service)],
)
async def delete_record_user(user_id: str) -> dict[str, str]:
    """按用户 ID 删除记录。"""
    # 这里可以添加实际的删除逻辑，例如调用数据库或其他服务。
    # 目前仅返回一个模拟的响应。
    return {"status": "deleted", "user_id": user_id}
