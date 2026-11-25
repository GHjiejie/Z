"""Permission-scoped retrieval over metadata from the active API tool registry."""

import re
from collections import Counter
from functools import lru_cache

from agent_permissions.agent_grants import _label, configurable_tools
from agent_permissions.operations import OPERATIONS
from agent_permissions.tool_vectors import ToolVectors

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
    "消耗量": "用量",
    "消耗": "用量",
    "停用": "禁用",
    "冻结": "禁用",
    "解封": "启用",
    "开个账号": "开通账号",
    "网名": "昵称",
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
    "token": "用量",
    "usage": "用量",
}
ACTIONS = {"新增", "查询", "更新", "删除", "重置", "分配", "退出", "启用", "禁用"}
RESOURCES = {"用户", "角色", "权限", "密码", "用量"}
ACTION_ONLY_TERMS = (ACTIONS - {"退出"}) | {
    word for word, action in TERM_ALIASES.items() if action in ACTIONS - {"退出"}
}
FILLER = re.compile(
    r"帮我|我想|请问|请帮|需要|一个|这个|那个|一下|一些|这位|那位|某位|"
    r"能否|是否|可以|暂且|操作|进行|处理|什么|多少|[个的了给让把]"
)
OTHER_USER = re.compile(
    r"指定|同事|别人|他人|其他(?:用户|成员|账号)|(?:这位|该)(?:用户|成员)"
)
OWN_USER = re.compile(r"自己|我的|我(?:想|要)?(?:换|改|用了|用掉|消耗)")

# Catalog browsing and short conversational continuations have no standalone
# business intent. An explicit new request always takes precedence over history.
DIRECTORY_QUERY = re.compile(
    r"工具(?:都|一共|目前)?(?:有哪|有什么|有多少|有几个|目录|列表|清单)"
    r"|(?:哪些|什么|可用|可调用|所有|全部|列出|展示|查询|查看|几个|多少)"
    r"(?:当前|账号|用户|自己|我的|可以|授权|使用|调用|可用|业务|全部|的|\s)*工具"
    r"|你(?:能|可以)(?:帮我)?做什么|what can you do|(?:list|available)\s+(?:(?:all|the|available)\s+)*tools?\b",
    re.IGNORECASE,
)
FOLLOWUP = re.compile(
    r"(?:那(?:么)?|再(?:查|看)(?:看|一下)?|(?:改|换)成)?\s*"
    r"(?:(?:上|下|这|本|前|近)[一二两三四五六七八九十几个\d]*(?:天|周|星期|月|年|季度?|页)"
    r"|[昨今明]天|第[一二三四五六七八九十\d]+页|20\d{2}[-年/.]\d{1,2}(?:[-月/.]\d{1,2})?[日号月]?"
    r"|刚才的(?:查询|结果))(?:的(?:数据|用量|结果))?(?:呢|吗)?"
    r"|(?:再查|再看)(?:一次|一遍)(?:呢|吗)?|(?:what|how) about (?:last|this|next) (?:week|month|year)",
    re.IGNORECASE,
)


def _terms(text: str, *, for_vectors: bool = False) -> set[str]:
    text = text.casefold()
    for alias, canonical in SYNONYMS.items():
        text = text.replace(alias, canonical)
    if for_vectors:
        text = FILLER.sub(" ", text)
    terms = set(re.findall(r"[a-z0-9]+", text))
    for plural in ("users", "roles", "permissions", "tools", "tokens"):
        if plural in terms:
            terms.add(plural[:-1])
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
    terms |= {TERM_ALIASES[word] for word in terms if word in TERM_ALIASES}
    return {t for t in terms if len(t) > 1} if for_vectors else terms


def catalog_vectors(snapshot: dict) -> ToolVectors:
    documents = []
    for op, tool in sorted(configurable_tools(snapshot).items()):
        title, group = _label(op, tool)
        definition = tool["definition"]
        spec = OPERATIONS.get(op)
        documents.append(
            (
                op,
                definition["name"],
                title,
                group,
                definition["description"],
                spec.search_examples if spec else (),
            )
        )
    return _fit_catalog(snapshot["bundleDigest"], tuple(documents))


@lru_cache(maxsize=2)
def _fit_catalog(revision: str, documents: tuple) -> ToolVectors:
    """Cache immutable published metadata only. Grants and user queries stay request-local."""
    corpus = {}
    for op, name, title, group, description, examples in documents:
        terms = Counter(_terms(title, for_vectors=True))
        terms.update(_terms(op, for_vectors=True) | _terms(name, for_vectors=True))
        terms.update(_terms(group, for_vectors=True))
        terms.update(_terms(description.partition("。")[0], for_vectors=True))
        for example in examples:
            terms.update(_terms(example, for_vectors=True))
        corpus[op] = terms
    return ToolVectors(corpus)


def initial_operations(
    messages: list[dict], allowed: list[dict], index: ToolVectors
) -> list[str]:
    """Retrieve from user intent before binding the first model turn of a run."""
    if not allowed or not messages or messages[-1]["role"] != "user":
        return []
    user_turns = [
        message["content"].strip()[:512]
        for message in messages
        if message["role"] == "user"
    ]
    query = user_turns[-1].rstrip("？?。！! ")
    followup = FOLLOWUP.fullmatch(query)
    if followup:
        # A date or page can refine a read, but cannot authorize repeating a write.
        prior = next(
            (
                turn
                for turn in reversed(user_turns[:-1])
                if not FOLLOWUP.fullmatch(turn.rstrip("？?。！! "))
            ),
            "",
        )
        if not prior or DIRECTORY_QUERY.search(prior):
            return []
        query = f"{prior} {query}"
    if DIRECTORY_QUERY.search(query):
        return []
    ranked = matching_operations(allowed, query, index=index)
    if followup:
        reads = {
            t["binding"]["operationId"]
            for t in allowed
            if t["binding"]["policy"]["effect"] == "read"
        }
        return [op for op in ranked if op in reads]
    return ranked


def matching_operations(
    tools: list[dict], query: str, *, index: ToolVectors | None = None
) -> list[str]:
    """Resolve explicit IDs first, then combine keyword evidence and vector similarity."""
    by_name = {}
    for tool in tools:
        operation = tool["binding"]["operationId"]
        by_name[operation.casefold()] = operation
        by_name[tool["definition"]["name"].casefold()] = operation
    words = [
        word.rstrip(".")
        for word in re.findall(
            r"[a-z][a-z0-9_.-]*",
            re.sub(r"[\w.+-]+@[\w.-]+", " ", query.casefold(), flags=re.ASCII),
        )
    ]
    identifiers = [
        word
        for word in words
        if word in by_name or "." in word or word.startswith("agent_users_")
    ]
    if identifiers:
        matches = dict.fromkeys(
            by_name[word] for word in identifiers if word in by_name
        )
        return list(matches)[:CANDIDATE_LIMIT]

    terms = _terms(query)
    actions, resources = terms & ACTIONS, terms & RESOURCES
    other_user = OTHER_USER.search(query)
    own_user = not other_user and OWN_USER.search(query)
    other_variants = (
        {op.replace(".self.", ".", 1) for op in index.rows if ".self." in op}
        if index and own_user
        else set()
    )
    semantic_terms = _terms(query, for_vectors=True)
    if index is not None and not (
        (semantic_terms - ACTION_ONLY_TERMS) & index.columns.keys()
    ):
        return []
    similarities = (
        index.similarities(semantic_terms, {t["binding"]["operationId"] for t in tools})
        if index
        else {}
    )
    ranked = []
    for tool in tools:
        operation = tool["binding"]["operationId"]
        title, group = _label(operation, tool)
        title_terms, id_terms = _terms(title), _terms(operation)
        primary = title_terms | id_terms
        if operation in other_variants or (
            index and other_user and "自己" not in terms and ".self." in operation
        ):
            continue
        # An unavailable "create user" must not suggest "delete user" or "create role".
        if (actions and actions.isdisjoint(primary)) or (
            resources and resources.isdisjoint(primary)
        ):
            continue
        if (
            index is not None
            and not actions
            and tool["binding"].get("policy", {}).get("effect", "read") != "read"
            and not semantic_terms.intersection(index.specific_terms.get(operation, ()))
        ):
            continue
        score = (
            6 * len(terms & title_terms)
            + 5 * len(terms & id_terms)
            + 2 * len(terms & _terms(group))
            + len(terms & _terms(tool["definition"]["description"]))
        )
        similarity = similarities.get(operation, 0)
        if similarity >= 0.45:
            score += 32 * similarity
        if score:
            ranked.append((score, operation))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    # Drop weak single-word overlaps when the query has a stronger match.
    return [op for score, op in ranked[:CANDIDATE_LIMIT] if 2 * score >= ranked[0][0]]


def safe_search_arguments(
    arguments: dict, allowed: list[dict], *, index: ToolVectors | None = None
) -> dict:
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
    operations = matching_operations(allowed, query, index=index)
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
