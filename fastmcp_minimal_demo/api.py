from fastapi import FastAPI

app = FastAPI()


@app.get("/add", operation_id="add")
def add(a: int, b: int) -> dict[str, int]:
    """计算两个整数的和。"""
    return {"sum": a + b}
