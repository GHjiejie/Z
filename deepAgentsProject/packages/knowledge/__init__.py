from .models import (
    KnowledgeBaseCreate,
    KnowledgeSearchRequest,
    UploadComplete,
    UploadPrepare,
)


def __getattr__(name):
    # Parsing children must not import the control plane, model clients or DB.
    if name == "KnowledgeService":
        from .service import KnowledgeService
        return KnowledgeService
    raise AttributeError(name)

__all__ = [
    "KnowledgeBaseCreate",
    "KnowledgeSearchRequest",
    "KnowledgeService",
    "UploadComplete",
    "UploadPrepare",
]
