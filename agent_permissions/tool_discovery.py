"""Permission-scoped retrieval over metadata from the active API tool registry."""

import re

from agent_permissions.agent_grants import _label

SEARCH_NAME = "search_tools"
PAGE_SIZE = 10
CANDIDATE_LIMIT = 3
SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "maxLength": 256,
            "description": "业务意图或操作 ID，空字符串列目录。只写能力关键词。",
        },
        "page": {"type": "integer", "minimum": 1, "description": "目录页码，从 1 开始"},
    },
    "required": ["query"],
    "additionalProperties": False,
}

# Normalize shared vocabulary rather than maintaining a second list of business tools.
SYNONYMS = {
    "创建": "新增",
    "添加": "新增",
    "新建": "新增",
    "列出": "列表",
    "查看": "查询",
    "读取": "查询",
    "搜索": "查询",
    "修改": "更新",
    "编辑": "更新",
    "重设": "重置",
    "口令": "密码",
    "成员": "用户",
    "账户": "账号",
    "当前登录账号": "自己",
    "账号信息": "资料",
    "个人信息": "自己的资料",
    "我的信息": "自己的资料",
    "个人": "自己",
    "我的": "自己",
    "我是谁": "自己的资料",
}
TERM_ALIASES = {
    "user": "用户",
    "账号": "用户",
    "role": "角色",
    "password": "密码",
    "permission": "权限",
    "create": "新增",
    "add": "新增",
    "delete": "删除",
    "remove": "删除",
    "read": "查询",
    "get": "查询",
    "list": "查询",
    "列表": "查询",
    "edit": "更新",
    "update": "更新",
    "change": "更新",
    "reset": "重置",
    "assign": "分配",
    "logout": "退出",
    "enable": "启用",
    "disable": "禁用",
}
ACTIONS = {"新增", "查询", "更新", "删除", "重置", "分配", "退出", "启用", "禁用"}
RESOURCES = {"用户", "角色", "权限", "密码"}


def _terms(text: str) -> set[str]:
    text = text.casefold()
    for alias, canonical in SYNONYMS.items():
        text = text.replace(alias, canonical)
    terms = set(re.findall(r"[a-z0-9]+", text))
    for plural in ("users", "roles", "permissions", "tools"):
        if plural in terms:
            terms.add(plural[:-1])
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
    return terms | {TERM_ALIASES[word] for word in terms if word in TERM_ALIASES}


def matching_operations(tools: list[dict], query: str) -> list[str]:
    """Prefer explicit IDs, then rank localized catalog metadata by term overlap."""
    by_name = {}
    for tool in tools:
        operation = tool["binding"]["operationId"]
        by_name[operation.casefold()] = operation
        by_name[tool["definition"]["name"].casefold()] = operation
    words = [
        word.rstrip(".")
        for word in re.findall(r"[a-z0-9][a-z0-9_.-]*", query.casefold())
    ]
    identifiers = [
        word
        for word in words
        if word in by_name or any(separator in word for separator in "._")
    ]
    if identifiers:
        matches = dict.fromkeys(
            by_name[word] for word in identifiers if word in by_name
        )
        return list(matches)[:CANDIDATE_LIMIT]

    terms = _terms(query)
    actions, resources = terms & ACTIONS, terms & RESOURCES
    ranked = []
    for tool in tools:
        operation = tool["binding"]["operationId"]
        title, group = _label(operation, tool)
        title_terms, id_terms = _terms(title), _terms(operation)
        primary = title_terms | id_terms
        # An unavailable "create user" must not suggest "delete user" or "create role".
        if (actions and actions.isdisjoint(primary)) or (
            resources and resources.isdisjoint(primary)
        ):
            continue
        score = (
            6 * len(terms & title_terms)
            + 5 * len(terms & id_terms)
            + 2 * len(terms & _terms(group))
            + len(terms & _terms(tool["definition"]["description"]))
        )
        if score:
            ranked.append((score, operation))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    # Drop weak single-word overlaps when the query has a stronger match.
    return [op for score, op in ranked[:CANDIDATE_LIMIT] if 2 * score >= ranked[0][0]]


def safe_search_arguments(arguments: dict, allowed: list[dict]) -> dict:
    """Persist only trusted operation IDs or a no-match marker in place of free text."""
    query, page = arguments.get("query"), arguments.get("page", 1)
    if (
        arguments.keys() - {"query", "page"}
        or not isinstance(query, str)
        or len(query) > 256
        or type(page) is not int
        or page < 1
        or (query.strip() and page != 1)
    ):
        return {}
    if not query.strip():
        return {"query": "", "page": page}
    operations = matching_operations(allowed, query)
    return {"query": " ".join(operations) or "?"}


def search_result(arguments: dict, allowed: list[dict]) -> tuple[dict, list[str]]:
    """Resolve a prepared lookup against the caller's current effective permissions."""
    by_id = {tool["binding"]["operationId"]: tool for tool in allowed}
    query = arguments["query"]
    if query:
        operations = [op for op in query.split() if op in by_id][:CANDIDATE_LIMIT]
        result = {"mode": "search", "count": len(operations)}
    else:
        page = arguments["page"]
        start = (page - 1) * PAGE_SIZE
        operations = sorted(by_id)[start : start + PAGE_SIZE]
        result = {
            "mode": "list",
            "total": len(by_id),
            "page": page,
            "next_page": page + 1 if start + PAGE_SIZE < len(by_id) else None,
        }
    result["tools"] = [
        {
            "operation_id": op,
            "name": by_id[op]["definition"]["name"],
            "title": _label(op, by_id[op])[0],
            "effect": by_id[op]["binding"]["policy"]["effect"],
        }
        for op in operations
    ]
    return result, operations if query else []
