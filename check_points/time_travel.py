from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph ,START,END
from langchain_core.messages import BaseMessage
from chat_models.chat import chat_model

from typing import TypedDict

class ChatState(TypedDict):
    user_msg: list[BaseMessage]
   

db_path= "./check_points/time_travel.sqlite" 


# 然后需要连接本地的数据库
checkpointer=SqliteSaver.from_conn_string(db_path)

def llm_call(state:ChatState):
    latest_msg=state["user_msg"][-1]
    
    # 获取用户发起的最新消息就可以
    user_msg=latest_msg.content
    response=chat_model.invoke(user_msg)
    return {"user_msg":[response]}

builer=StateGraph(ChatState)
builer.add_node("llm_call",llm_call)
builer.add_edge(START,"llm_call")
builer.add_edge("llm_call",END)
graph=builer.compile(checkpointer=checkpointer)




    
  
    





