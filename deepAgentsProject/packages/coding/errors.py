class CodingError(RuntimeError):
    code = "CODING_ERROR"


class CodingNotFoundError(CodingError):
    code = "CODING_NOT_FOUND"


class CodingConflictError(CodingError):
    code = "CODING_CONFLICT"


class RepositoryAccessError(CodingError):
    code = "REPOSITORY_ACCESS_DENIED"


class SandboxUnavailableError(CodingError):
    code = "SANDBOX_UNAVAILABLE"


class SandboxPolicyError(CodingError):
    code = "SANDBOX_POLICY_DENIED"
