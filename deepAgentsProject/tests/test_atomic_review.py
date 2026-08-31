import hashlib

import pytest

from packages.application.services import ConflictError
from packages.coding.errors import CodingConflictError
from packages.domain.models import TenantContext, ThreadAccessUpdate, utc_now
from test_runtime_concurrency import runtime, race, new_thread
from test_coding_recovery import coding_run


def prepare_review(runtime, tmp_path):
    _, services, owner, _, _ = runtime
    run, plan = coding_run(runtime, tmp_path)
    workspace = services.db.fetch_one("SELECT * FROM coding_workspaces WHERE id=?", (run["coding_workspace_id"],))
    snapshot = services.db.fetch_one("SELECT * FROM repository_snapshots WHERE id=?", (workspace["repository_snapshot_id"],))
    patch = "review fixture patch"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    services.db.execute("""INSERT INTO artifacts
        (id,tenant_id,project_id,run_id,name,media_type,size_bytes,content_hash,content,
         plan_hash,base_commit_sha,workspace_generation,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "review_patch", owner.tenant_id, owner.project_id, run["id"], "review.patch", "text/x-diff", len(patch),
        digest, patch, plan["plan_hash"], snapshot["resolved_commit_sha"], workspace["workspace_generation"], utc_now(),
    ))
    services.db.execute("""INSERT INTO change_sets
        (id,tenant_id,project_id,run_id,workspace_id,base_commit_sha,workspace_generation,
         patch_artifact_id,diff_artifact_id,diff_stat_json,changed_files_json,status,content_hash,plan_hash,created_at)
        VALUES(?,?,?,?,?,?,?,'review_patch','review_patch','{}','[]','PENDING_APPROVAL',?,?,?)""", (
        "review_change", owner.tenant_id, owner.project_id, run["id"], workspace["id"], snapshot["resolved_commit_sha"],
        workspace["workspace_generation"], digest, plan["plan_hash"], utc_now(),
    ))
    services.runs.update_thread_access(run["thread_id"], ThreadAccessUpdate(version=1, visibility="project", reason="Permit project review"), owner)
    return run


def test_competing_approve_and_reject_have_one_atomic_winner(runtime, tmp_path):
    _, services, owner, _, _ = runtime
    run = prepare_review(runtime, tmp_path)

    def review(index):
        actor = owner.model_copy(update={"user_id": f"reviewer_{index}", "roles": ["operator"]})
        try:
            return services.coding.decide_change_set(run["id"], "review_change", bool(index % 2), actor, expected_version=1)
        except CodingConflictError:
            return None

    results = race(review)
    winners = [result for result in results if result]
    assert len(winners) == 1
    assert winners[0]["version"] == 2
    events = [event for event in services.events.list(run["id"]) if event["type"] in {"changeset.delivered", "changeset.rejected"}]
    assert len(events) == 1
    actor_id = events[0]["payload"]["actor"]
    actor = owner.model_copy(update={"user_id": actor_id, "roles": ["operator"]})
    approved = winners[0]["status"] == "DELIVERED"
    repeated = services.coding.decide_change_set(run["id"], "review_change", approved, actor, expected_version=1)
    assert repeated == winners[0]
    with pytest.raises(CodingConflictError):
        services.coding.decide_change_set(run["id"], "review_change", not approved, actor, expected_version=1)
    assert len([event for event in services.events.list(run["id"]) if event["type"].startswith("changeset.")]) == 1


def test_approval_and_its_audit_roll_back_together(runtime, tmp_path, monkeypatch):
    _, services, owner, _, _ = runtime
    run = prepare_review(runtime, tmp_path)
    original = services.events.append

    def fail_audit(run_id, event_type, *args, **kwargs):
        if event_type.startswith("changeset."):
            raise RuntimeError("injected audit persistence failure")
        return original(run_id, event_type, *args, **kwargs)

    monkeypatch.setattr(services.events, "append", fail_audit)
    with pytest.raises(RuntimeError, match="injected"):
        services.coding.decide_change_set(run["id"], "review_change", True, owner, expected_version=1)
    record = services.db.fetch_one("SELECT * FROM change_sets WHERE id='review_change'")
    assert record["status"] == "PENDING_APPROVAL"
    assert record["version"] == 1 and record["decision_hash"] is None


def test_sharing_version_check_is_atomic(runtime):
    _, services, owner, _, _ = runtime
    thread = new_thread(runtime)

    def update(index):
        try:
            return services.runs.update_thread_access(thread["id"], ThreadAccessUpdate(version=1,
                visibility="project" if index % 2 else "private", reason=f"Concurrent update {index}"), owner)
        except ConflictError:
            return None

    assert len([result for result in race(update) if result]) == 1
    assert services.runs.thread_access(thread["id"], owner)["version"] == 2
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events WHERE resource_id=? AND action='thread.sharing.updated'", (thread["id"],))["n"] == 1
