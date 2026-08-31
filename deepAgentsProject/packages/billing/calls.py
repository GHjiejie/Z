"""Meter auxiliary provider calls without sharing mutable per-request usage state."""
from __future__ import annotations

import hashlib
import json

from packages.auth.resource_access import refresh_context
from packages.billing.errors import BillingConfigurationError, QuotaExceeded
from packages.billing.meter import Meter


def token_reservation(value) -> int:
    # Conservative bytes, not a fabricated provider token measurement.
    return len(json.dumps(value, ensure_ascii=False, default=str).encode()) + 1024


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def complete(db, gateway, messages, context, *, purpose, resource_id):
    context = refresh_context(db, context)
    meter = Meter(db)
    identity = gateway.identity()
    config = getattr(gateway, "config", None)
    output_bound = getattr(config, "max_completion_tokens", 4096)
    ticket = meter.reserve(context, identity, meter.pricing(context, identity),
        purpose=purpose, resource_id=resource_id,
        input_tokens=token_reservation(messages), output_tokens=output_bound,
        duration_seconds=int(getattr(config, "timeout_seconds", 180)) + 60,
        fingerprint=fingerprint(messages))
    try:
        response = await gateway.complete(messages)
        violations = meter.settle(ticket, input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens, estimated=response.usage.estimated)
    except BaseException:
        meter.uncertain(ticket)
        raise
    if violations:
        raise QuotaExceeded("; ".join(violations))
    return response


def embed(db, provider, texts, context, *, purpose, resource_id):
    context = refresh_context(db, context)
    if not callable(getattr(provider, "embed_with_usage", None)) or not callable(getattr(provider, "identity", None)):
        raise BillingConfigurationError("Embedding providers must implement identity and usage reporting")
    meter = Meter(db)
    identity = provider.identity()
    bound = token_reservation(texts)
    ticket = meter.reserve(context, identity, meter.pricing(context, identity),
        purpose=purpose, resource_id=resource_id, input_tokens=bound, output_tokens=0,
        fingerprint=fingerprint(texts))
    try:
        result = provider.embed_with_usage(texts)
        violations = meter.settle(ticket, input_tokens=result.input_tokens if result.input_tokens is not None else bound,
            output_tokens=0, estimated=result.input_tokens is None, provider_receipt=result.provider_receipt)
    except BaseException:
        meter.uncertain(ticket)
        raise
    if violations:
        raise QuotaExceeded("; ".join(violations))
    return result.vectors
