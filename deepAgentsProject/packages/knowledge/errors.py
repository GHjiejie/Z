class KnowledgeError(Exception):
    code = "KNOWLEDGE_ERROR"


class KnowledgeNotFoundError(KnowledgeError):
    code = "KNOWLEDGE_NOT_FOUND"


class KnowledgeConflictError(KnowledgeError):
    code = "KNOWLEDGE_CONFLICT"


class KnowledgeStorageError(KnowledgeError):
    code = "KNOWLEDGE_STORAGE_ERROR"


class KnowledgeValidationError(KnowledgeError):
    code = "KNOWLEDGE_VALIDATION_ERROR"


class ParseLimitExceeded(KnowledgeValidationError):
    code = "KNOWLEDGE_PARSE_LIMIT_EXCEEDED"


class ParseTimeout(KnowledgeValidationError):
    code = "KNOWLEDGE_PARSE_TIMEOUT"


class ParseUnavailable(KnowledgeError):
    code = "KNOWLEDGE_PARSE_UNAVAILABLE"


class ParseProtocolError(KnowledgeError):
    code = "KNOWLEDGE_PARSE_PROTOCOL_ERROR"
