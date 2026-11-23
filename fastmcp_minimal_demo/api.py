from fastapi import FastAPI

app = FastAPI()


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
