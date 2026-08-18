from typing import TypedDict,Literal
from pydantic import BaseModel, Field


class State(TypedDict):
    messages: list
    intent: Literal["search_order", "delete_order"]
    
class StateModel(BaseModel):
    messages: list = Field(..., description="消息列表")
    intent: Literal["search_order", "delete_order"] = Field(..., description="识别到的意图")



# 接下来就是测试TypedDict和Pydantic的区别了

# TypedDict
# 下面的代码只是会爆红（原因是静态检查没有通过），但是你实际上是可以执行代码的。
# 最后会输出{'messages': [{'role': 'user', 'content': '我想搜索订单'}], 'intent': 'hhh'}
try:
  test_dict=State(
      messages=[{"role": "user", "content": "我想搜索订单"}],
      intent="hhh")
  print(test_dict)
except Exception as e:
    print(f"State validation error: {e}")

# Pydantic
# 下面的代码首先会爆红（原因是静态检查没有通过），然后在运行时也会抛出异常，提示验证失败。
# Input should be 'search_order' or 'delete_order' [type=literal_error, input_value='hhh', input_type=str]
try:
    test=StateModel(
        messages=[{"role": "user", "content": "我想搜索订单"}],
        intent="hhh")
except Exception as e:
    print(f"StateModel validation error: {e}")
    
    