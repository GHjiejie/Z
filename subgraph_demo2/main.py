"""家庭晚餐助手：两个独立状态的子图，各调用一次真实模型。"""

from __future__ import annotations

import sys
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from chat_models.chat import chat_model


class Requirements(BaseModel):
    """模型解析的用餐需求；没有提供的信息保持为空，不猜测。"""

    adults: int | None = Field(description="成人人数，未提供则为 null", ge=0)
    children: int | None = Field(description="儿童人数，未提供则为 null", ge=0)
    ingredients: list[str] = Field(description="用户明确拥有的食材，使用统一中文名称")
    restrictions: list[str] = Field(description="用户明确提出的口味、忌口、过敏等限制")
    max_time: int | None = Field(description="最多可用分钟数，未提供则为 null", gt=0)


class Dish(BaseModel):
    name: str
    ingredients: list[str] = Field(
        description="全部必需食材，含油盐等调味品，与需求中的名称一致"
    )
    time: int = Field(description="含准备工作的预计分钟数", gt=0)
    steps: list[str] = Field(description="具体可执行的做法步骤")


class Menu(BaseModel):
    dishes: list[Dish] = Field(
        description="顺序制作、尽量满足时间与食材限制的菜单", min_length=1
    )
    notes: list[str] = Field(
        description="儿童适配做法、假设和不能满足的限制；不做无依据的安全保证"
    )


class MealPlan(BaseModel):
    menu: Menu
    total_time: int
    missing_ingredients: list[str]
    warnings: list[str]


class RequirementState(TypedDict, total=False):
    user_input: str
    extracted: Requirements  # 子图内部中间字段，不传给主图
    requirements: Requirements


class MealPlanState(TypedDict, total=False):
    requirements: Requirements
    menu: Menu  # 子图内部中间字段
    meal_plan: MealPlan


class DinnerState(TypedDict, total=False):
    user_input: str
    requirements: Requirements
    meal_plan: MealPlan
    final_answer: str


# 使用项目已有模型配置，Pydantic 将模型输出转换为经过校验的对象。
requirement_model = chat_model.with_structured_output(
    Requirements, method="function_calling"
)
menu_model = chat_model.with_structured_output(Menu, method="function_calling")


def extract(state: RequirementState) -> dict:
    result = requirement_model.invoke(
        [
            (
                "system",
                (
                    "解析家庭晚餐需求。只提取明确提供的信息，不默认两位大人、不默认30分钟。"
                    "统一食材别名，例如番茄统一为西红柿；保留所有口味、过敏及饮食限制。"
                ),
            ),
            ("human", state["user_input"]),
        ]
    )
    return {"extracted": result}


def normalize(state: RequirementState) -> dict:
    result = state["extracted"]
    return {
        "requirements": result.model_copy(
            update={
                "ingredients": list(dict.fromkeys(result.ingredients)),
            }
        )
    }


def build_requirement_subgraph():
    builder = StateGraph(RequirementState)
    builder.add_node("extract", extract)
    builder.add_node("normalize", normalize)
    builder.add_edge(START, "extract")
    builder.add_edge("extract", "normalize")
    builder.add_edge("normalize", END)
    return builder.compile()


def plan_menu(state: MealPlanState) -> dict:
    result = menu_model.invoke(
        [
            (
                "system",
                (
                    "你是家庭晚餐规划助手。根据JSON需求用中文设计简单菜单和具体步骤。"
                    "优先使用现有食材，遵守所有忌口和人数需求。按一个人依次做菜估算含准备的时间，"
                    "各道菜耗时之和尽量不超过时间上限。食材列出油盐等全部必需品并统一命名。"
                    "不把未提及的食材视为已有；无法满足的要求和必要假设放入notes。"
                    "有孩子时说明具体适配做法，不直接保证儿童安全。"
                ),
            ),
            ("human", state["requirements"].model_dump_json()),
        ]
    )
    return {"menu": result}


def summarize(state: MealPlanState) -> dict:
    requirements, menu = state["requirements"], state["menu"]
    total = sum(dish.time for dish in menu.dishes)
    needed = {item for dish in menu.dishes for item in dish.ingredients}
    warnings = []
    if requirements.max_time is not None and total > requirements.max_time:
        warnings.append(
            f"预计超出时间上限 {total - requirements.max_time} 分钟，此方案未满足时间要求。"
        )
    return {
        "meal_plan": MealPlan(
            menu=menu,
            total_time=total,
            missing_ingredients=sorted(needed - set(requirements.ingredients)),
            warnings=warnings,
        )
    }


def build_meal_plan_subgraph():
    builder = StateGraph(MealPlanState)
    builder.add_node("plan_menu", plan_menu)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "plan_menu")
    builder.add_edge("plan_menu", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile()


requirement_subgraph = build_requirement_subgraph()
meal_plan_subgraph = build_meal_plan_subgraph()


# 父子状态结构不同，用包装节点显式映射输入和输出。
# 下面两个函数的作用主要就是把子图的最后的运行结果映射到父图的状态中。
def run_requirements(state: DinnerState) -> dict:
    result = requirement_subgraph.invoke({"user_input": state["user_input"]})
    return {"requirements": result["requirements"]}


def run_meal_plan(state: DinnerState) -> dict:
    result = meal_plan_subgraph.invoke({"requirements": state["requirements"]})
    return {"meal_plan": result["meal_plan"]}


def format_result(state: DinnerState) -> dict[str, str]:
    plan = state["meal_plan"]
    lines = [
        f"预计总耗时：{plan.total_time} 分钟（依次制作，含准备）",
        "需补充食材：" + ("、".join(plan.missing_ingredients) or "无"),
    ]
    for dish in plan.menu.dishes:
        lines.extend(
            [
                f"\n{dish.name}（约 {dish.time} 分钟）",
                "食材：" + "、".join(dish.ingredients),
            ]
        )
        lines.extend(f"  {index}. {step}" for index, step in enumerate(dish.steps, 1))
    lines.extend(f"提示：{note}" for note in plan.menu.notes + plan.warnings)
    return {"final_answer": "\n".join(lines)}


def build_graph():
    builder = StateGraph(DinnerState)
    builder.add_node("requirements_subgraph", run_requirements)
    builder.add_node("meal_plan_subgraph", run_meal_plan)
    builder.add_node("format", format_result)
    builder.add_edge(START, "requirements_subgraph")
    builder.add_edge("requirements_subgraph", "meal_plan_subgraph")
    builder.add_edge("meal_plan_subgraph", "format")
    builder.add_edge("format", END)
    return builder.compile()


def write_graph_visualizations(graph) -> None:
    """Write Mermaid source and print ASCII topology for all graph levels."""
    mermaid = """flowchart TD
    START --> requirements_subgraph
    requirements_subgraph --> meal_plan_subgraph
    meal_plan_subgraph --> format
    format --> END
    subgraph R[requirements_subgraph]
        R1[START] --> extract[extract / 模型调用]
        extract --> normalize[normalize] --> R2[END]
    end
    subgraph M[meal_plan_subgraph]
        M1[START] --> plan_menu[plan_menu / 模型调用]
        plan_menu --> summarize[summarize] --> M2[END]
    end
"""
    from pathlib import Path

    Path(__file__).with_name("graph.mmd").write_text(mermaid, encoding="utf-8")
    print("\n主图 ASCII：")
    print(graph.get_graph().draw_ascii())
    print("\n需求子图 ASCII：")
    print(requirement_subgraph.get_graph().draw_ascii())
    print("\n菜谱子图 ASCII：")
    print(meal_plan_subgraph.get_graph().draw_ascii())
    print(f"Mermaid 源码已写入：{Path(__file__).with_name('graph.mmd')}")


def main() -> None:
    user_input = (
        " ".join(sys.argv[1:])
        or "家里有鸡蛋、西红柿、青菜，两位大人和一个孩子，孩子不吃辣，希望30分钟内完成"
    )
    if not user_input.strip():
        raise SystemExit("请输入晚餐需求。")
    graph = build_graph()
    write_graph_visualizations(graph)
    print("\n需求子图：extract（模型）-> normalize", flush=True)
    print("菜谱子图：plan_menu（模型）-> summarize", flush=True)
    print("正在调用模型解析需求、规划菜单……", flush=True)
    print("\n" + graph.invoke({"user_input": user_input})["final_answer"])


if __name__ == "__main__":
    main()
