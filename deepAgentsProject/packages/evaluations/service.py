from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.resource_access import ResourceAccess
from packages.auth.transactions import authorized_write
from packages.domain.models import TenantContext, TERMINAL_RUN_STATUSES
from packages.evaluations.models import EvaluationCase, EvaluationPolicyUpdate, EvaluationRequest, EvaluationSuiteCreate
from packages.persistence import Database


BASE_CATEGORIES = {"functional", "safety", "recovery", "cost"}
RESULT_FIELDS = (
    "id", "tenant_id", "project_id", "agent_revision_id", "sequence", "plan_hash",
    "suite_id", "suite_hash", "status", "score", "production_eligible", "checks",
    "evidence", "created_by", "created_at",
)


def digest(value) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class EvaluationService:
    """Grade persisted Runs, never caller-supplied scores or execution claims.

    Suite administration and evaluation execution are separate permissions. A
    certificate freezes the observed Attempt and event boundary; subsequent Run
    retries do not rewrite that historical evidence. Release checks always use
    the newest result for the currently required suite and exact plan.
    """

    def __init__(self, db: Database):
        self.db = db

    def _audit(self, context, action, resource_id, details):
        self.db.execute(
            """INSERT INTO governance_audit_events
               (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (new_id("audit"), context.tenant_id, context.project_id, context.user_id,
             action, resource_id, self.db.encode(details), self.db.current_time().isoformat()),
        )

    def create_suite(self, payload: EvaluationSuiteCreate, context: TenantContext):
        suite_id = new_id("suite")
        cases = [case.model_dump() for case in payload.cases]
        suite_hash = digest(cases)
        with authorized_write(self.db, context, Permission.EVALUATION_MANAGE) as context:
            self.db.execute(
                """INSERT INTO evaluation_suites
                   (id,tenant_id,project_id,name,cases_json,suite_hash,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (suite_id, context.tenant_id, context.project_id, payload.name, self.db.encode(cases),
                 suite_hash, context.user_id, self.db.current_time().isoformat()),
            )
            self._audit(context, "evaluation.suite.created", suite_id, {"suite_hash": suite_hash})
            return self.get_suite(suite_id, context)

    def get_suite(self, suite_id: str, context: TenantContext):
        authorize(context, Permission.AGENT_READ)
        row = self.db.fetch_one(
            "SELECT * FROM evaluation_suites WHERE id=? AND tenant_id=? AND project_id=?",
            (suite_id, context.tenant_id, context.project_id),
        )
        if not row:
            raise NotFoundError("Evaluation suite not found")
        if digest(row["cases"]) != row["suite_hash"]:
            raise ConflictError("Evaluation suite integrity check failed")
        return row

    def list_suites(self, context: TenantContext, limit: int = 100):
        authorize(context, Permission.AGENT_READ)
        return self.db.fetch_all(
            """SELECT id,name,suite_hash,created_by,created_at FROM evaluation_suites
               WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC LIMIT ?""",
            (context.tenant_id, context.project_id, min(200, max(1, limit))),
        )

    def policy(self, context: TenantContext):
        authorize(context, Permission.AGENT_READ)
        return self.db.fetch_one(
            "SELECT * FROM evaluation_policies WHERE tenant_id=? AND project_id=?",
            (context.tenant_id, context.project_id),
        ) or {"version": 0, "suite_id": None}

    def update_policy(self, payload: EvaluationPolicyUpdate, context: TenantContext):
        with authorized_write(self.db, context, Permission.EVALUATION_MANAGE) as context:
            suite = self.get_suite(payload.suite_id, context)
            if BASE_CATEGORIES - {case["category"] for case in suite["cases"] if case["category"] == "safety" or case["expected_status"] == "SUCCEEDED"}:
                raise ConflictError("A release suite must cover functional, safety, recovery and cost cases")
            now = self.db.current_time().isoformat()
            if payload.version == 0:
                changed = self.db.execute_count(
                    """INSERT OR IGNORE INTO evaluation_policies
                       (tenant_id,project_id,suite_id,version,max_age_seconds,updated_by,updated_at)
                       VALUES(?,?,?,1,?,?,?)""",
                    (context.tenant_id, context.project_id, suite["id"], payload.max_age_seconds, context.user_id, now),
                )
            else:
                changed = self.db.execute_count(
                    """UPDATE evaluation_policies SET suite_id=?,version=version+1,max_age_seconds=?,
                       updated_by=?,updated_at=? WHERE tenant_id=? AND project_id=? AND version=?""",
                    (suite["id"], payload.max_age_seconds, context.user_id, now,
                     context.tenant_id, context.project_id, payload.version),
                )
            if changed != 1:
                raise ConflictError("Evaluation policy changed; reload before updating")
            self._audit(context, "evaluation.policy.updated", suite["id"], payload.model_dump())
            return self.policy(context)

    def _revision_plan(self, revision_id, context, *, lock=False):
        suffix = " FOR UPDATE" if lock and self.db.dialect == "postgresql" else ""
        row = self.db.fetch_one(
            "SELECT id FROM agent_revisions WHERE id=? AND tenant_id=? AND project_id=?" + suffix,
            (revision_id, context.tenant_id, context.project_id),
        )
        if not row:
            raise NotFoundError("Agent revision not found")
        plan = self.db.fetch_one("SELECT * FROM resolved_execution_plans WHERE agent_revision_id=?", (revision_id,))
        if not plan:
            raise ConflictError("Revision has no compiled execution plan")
        return plan

    def evaluate(self, revision_id: str, payload: EvaluationRequest, context: TenantContext, idempotency_key=None):
        with authorized_write(self.db, context, Permission.AGENT_PUBLISH) as context:
            plan_row = self._revision_plan(revision_id, context, lock=True)
            scope = f"evaluation:{context.project_id}:{revision_id}"
            request_hash = digest(payload.model_dump())
            if idempotency_key:
                previous = self.db.fetch_one(
                    "SELECT response_json FROM idempotency_records WHERE tenant_id=? AND scope=? AND key=?",
                    (context.tenant_id, scope, idempotency_key),
                )
                if previous:
                    saved = json.loads(previous["response_json"])
                    if saved["request_hash"] != request_hash:
                        raise ConflictError("Evaluation idempotency key was used for a different request")
                    return self.get_result(saved["evaluation_id"], context)
            suite = self.get_suite(payload.suite_id, context)
            cases = [EvaluationCase.model_validate(case) for case in suite["cases"]]
            if set(payload.case_runs) != {case.id for case in cases}:
                raise ConflictError("Provide exactly one Run for every case in the immutable suite")
            # Lock in a stable order while copying output, usage and the event boundary.
            samples = {}
            suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
            for run_id in sorted(payload.case_runs.values()):
                run = self.db.fetch_one(
                    "SELECT * FROM runs WHERE id=? AND tenant_id=? AND project_id=?" + suffix,
                    (run_id, context.tenant_id, context.project_id),
                )
                if not run:
                    raise NotFoundError("Evaluation Run not found")
                ResourceAccess(self.db).require_run(run_id, context)
                if run["resolved_plan_id"] != plan_row["id"]:
                    raise ConflictError("Evaluation Run does not use the requested revision's exact plan")
                if run["created_at"] < suite["created_at"]:
                    raise ConflictError("Evaluation Runs must be created after the suite is frozen")
                if run["status"] not in TERMINAL_RUN_STATUSES | {"WAITING_FOR_APPROVAL"}:
                    raise ConflictError("Evaluation Run has not reached a stable result boundary")
                samples[run_id] = run
            results, evidence = [], []
            for case in cases:
                run = samples[payload.case_runs[case.id]]
                if run["input"] != case.input:
                    raise ConflictError(f"Run input does not match evaluation case {case.id}")
                checks, facts = self._grade(case, run, plan_row["plan"])
                results.append({"case_id": case.id, "category": case.category, "passed": all(checks.values()), "checks": checks})
                evidence.append(facts)
            needed = set(BASE_CATEGORIES)
            if (plan_row["plan"].get("coding_profile") or {}).get("enabled"):
                needed.add("coding")
            if plan_row["plan"].get("knowledge_bindings"):
                needed.add("knowledge")
            missing = sorted(needed - {case.category for case in cases if case.category == "safety" or case.expected_status == "SUCCEEDED"})
            passed = all(result["passed"] for result in results)
            eligible = not missing and all(item["production_evidence"] for item in evidence) and any(item["model_calls"] for item in evidence)
            sequence = self.db.fetch_one(
                "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM evaluation_results WHERE agent_revision_id=?", (revision_id,),
            )["n"]
            record = {
                "id": new_id("eval"), "tenant_id": context.tenant_id, "project_id": context.project_id,
                "agent_revision_id": revision_id, "sequence": sequence, "plan_hash": plan_row["plan_hash"],
                "suite_id": suite["id"], "suite_hash": suite["suite_hash"],
                "status": "PASSED" if passed else "FAILED", "score": sum(item["passed"] for item in results) / len(results),
                "production_eligible": int(passed and eligible), "checks": results,
                "evidence": {"schema_version": 1, "missing_categories": missing, "runs": evidence},
                "created_by": context.user_id, "created_at": self.db.current_time().isoformat(),
            }
            record["result_hash"] = digest(record)
            self.db.execute(
                """INSERT INTO evaluation_results
                   (id,tenant_id,project_id,agent_revision_id,sequence,plan_hash,suite_id,suite_hash,
                    status,score,production_eligible,checks_json,evidence_json,created_by,created_at,result_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(self.db.encode(record[key]) if key in {"checks", "evidence"} else record[key] for key in RESULT_FIELDS)
                + (record["result_hash"],),
            )
            self._audit(context, "evaluation.completed", record["id"], {
                "result_hash": record["result_hash"], "status": record["status"], "plan_hash": record["plan_hash"],
            })
            if idempotency_key:
                self.db.execute(
                    "INSERT INTO idempotency_records(tenant_id,scope,key,response_json,created_at) VALUES(?,?,?,?,?)",
                    (context.tenant_id, scope, idempotency_key,
                     self.db.encode({"request_hash": request_hash, "evaluation_id": record["id"]}), record["created_at"]),
                )
            return record

    def _event_facts(self, run_id):
        sequence, count, event_types = 0, 0, set()
        checksum = hashlib.sha256()
        runtime_contract = None
        reported_models = set()
        while True:
            rows = self.db.fetch_all(
                "SELECT event_json FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 500",
                (run_id, sequence),
            )
            if not rows:
                break
            for row in rows:
                event = row["event"]
                sequence = event["sequence"]
                count += 1
                checksum.update((digest(event) + "\n").encode())
                event_types.add(event["type"])
                if event["type"] == "runtime.execution.bound":
                    runtime_contract = event["payload"]
                if event["type"] == "model.completed" and event["payload"].get("model"):
                    reported_models.add(event["payload"]["model"])
        return {"last_sequence": sequence, "event_count": count, "event_hash": checksum.hexdigest(),
                "event_types": sorted(event_types), "runtime_contract": runtime_contract,
                "reported_models": sorted(reported_models)}

    def _grade(self, case, run, plan):
        output = run["output"] or ""
        facts = self._event_facts(run["id"])
        event_types = set(facts["event_types"])
        usage = self.db.fetch_all("SELECT * FROM usage_ledger WHERE run_id=? ORDER BY id", (run["id"],))
        model_rows = [row for row in usage if row["model_calls"] and row.get("purpose", "run_model") == "run_model"]
        cost = sum(float(row["cost"]) for row in usage)
        checks = {"status": run["status"] == case.expected_status,
                  "single_input_sample": not (run.get("metadata") or {}).get("resume_input") and "run.input_received" not in event_types}
        interruptions = self.db.fetch_all("SELECT decision_json FROM interrupts WHERE run_id=? ORDER BY created_at,id", (run["id"],))
        decisions = [
            {key: decision.get(key) for key in ("type", "edited_arguments", "message")}
            for item in interruptions for decision in (item.get("decision") or {}).get("decisions", [])
        ]
        checks["approval_decisions_match"] = decisions == [decision.model_dump() for decision in case.expected_decisions]
        facts["approval_decisions_hash"] = digest(decisions)
        for index, text in enumerate(case.output_contains):
            checks[f"output_contains_{index}"] = text in output
        for index, text in enumerate(case.output_not_contains):
            checks[f"output_excludes_{index}"] = text not in output
        for name in case.required_event_types:
            checks[f"event:{name}"] = name in event_types
        if case.expected_status == "SUCCEEDED":
            checks["nonempty_output"] = bool(output.strip())
        if case.max_cost is not None:
            checks["cost_ceiling"] = math.isfinite(cost) and 0 <= cost <= case.max_cost
        duration = (datetime.fromisoformat(run["updated_at"]) - datetime.fromisoformat(run["created_at"])).total_seconds()
        if case.max_duration_seconds is not None:
            checks["duration_ceiling"] = 0 <= duration <= case.max_duration_seconds
        if case.category == "safety":
            checks["approval_boundary_observed"] = "interrupt.created" in event_types
        if case.category == "recovery":
            checks["resume_observed"] = "graph.resumed" in event_types and bool({"run.resumed", "run.orphaned"} & event_types)
        if case.category == "cost":
            checks["metered_execution"] = bool(model_rows)
        if case.required_document_ids:
            documents = set()
            audits = self.db.fetch_all("SELECT hits_json FROM knowledge_retrieval_audits WHERE run_id=?", (run["id"],))
            for audit in audits:
                for hit in audit["hits"]:
                    chunk = self.db.fetch_one("SELECT document_id FROM knowledge_chunks WHERE id=?", (hit["chunk_id"],))
                    if chunk:
                        documents.add(chunk["document_id"])
            checks["expected_documents_retrieved"] = set(case.required_document_ids).issubset(documents)
            checks["citations_present"] = bool(re.search(r"\[cite_\d+\]", output))
            facts["retrieved_document_ids"] = sorted(documents)
        if case.category == "coding":
            report = self.db.fetch_one("SELECT * FROM verification_reports WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run["id"],))
            source = self.db.fetch_one(
                """SELECT s.archive_sha256 FROM coding_workspaces w
                   JOIN repository_snapshots s ON s.id=w.repository_snapshot_id WHERE w.id=?""", (run["coding_workspace_id"],),
            )
            checks["source_snapshot_matches"] = bool(source and source["archive_sha256"] == case.expected_source_sha256)
            checks["verification_passed"] = bool(report and report["status"] == "PASSED" and report["checks"])
            facts["verification_report_hash"] = report["content_hash"] if report else None
        issues = []
        if not plan["model_snapshot"].get("runtime_binding"):
            issues.append("model_has_no_approved_immutable_binding")
        contract = facts["runtime_contract"] or {}
        if contract.get("evidence_version") != 2 or contract.get("source") != "live":
            issues.append("runtime_evidence_is_legacy_or_simulated")
        if any(row.get("billing_status") != "ACTUAL" or not row.get("metering_version") for row in usage if row["model_calls"]):
            issues.append("model_usage_is_unsettled_or_unverified")
        if any(model != plan["model_snapshot"]["model"] for model in facts["reported_models"]):
            issues.append("reported_model_does_not_match_plan")
        for row in model_rows:
            identity = row.get("model_identity") or {}
            if identity.get("provider") in {None, "", "test_double", "fake"} or identity.get("model") != plan["model_snapshot"]["model"]:
                issues.append("model_identity_is_simulated_or_does_not_match_plan")
                break
            binding = plan["model_snapshot"].get("runtime_binding")
            if binding:
                from packages.billing.models import model_key
                if model_key(identity) != model_key(binding.get("identity") or {}):
                    issues.append("provider_endpoint_or_model_does_not_match_plan")
        coding = plan.get("coding_profile") or {}
        if coding.get("enabled") and (coding.get("sandbox") or {}).get("provider") == "fake":
            issues.append("sandbox_is_simulated")
        if not coding.get("enabled") and plan.get("subagent_bindings"):
            issues.append("reference_subagent_bindings_are_not_executable")
        facts.update({
            "case_id": case.id, "run_id": run["id"], "attempt_id": run["current_attempt_id"],
            "status": run["status"], "input_hash": digest(run["input"]), "output_hash": digest(output),
            "sample_created_at": run["created_at"], "sample_updated_at": run["updated_at"],
            "runtime_input_hash": digest({"input": run["input"], "metadata": run.get("metadata"), "checkpoint": run.get("checkpoint")}),
            "usage_hash": digest(usage), "cost": cost, "duration_seconds": duration,
            "model_calls": sum(row["model_calls"] for row in model_rows),
            "production_evidence": not issues, "eligibility_issues": issues,
        })
        return checks, facts

    def get_result(self, result_id, context):
        row = self._verified_result(result_id, context)
        for sample in row["evidence"]["runs"]:
            ResourceAccess(self.db).require_run(sample["run_id"], context)
        return row

    def _verified_result(self, result_id, context):
        authorize(context, Permission.AGENT_READ)
        row = self.db.fetch_one(
            "SELECT * FROM evaluation_results WHERE id=? AND tenant_id=? AND project_id=?",
            (result_id, context.tenant_id, context.project_id),
        )
        if not row:
            raise NotFoundError("Evaluation result not found")
        if digest({key: row[key] for key in RESULT_FIELDS}) != row["result_hash"]:
            raise ConflictError("Evaluation result integrity check failed")
        return row

    def require_production_result(self, revision_id, context):
        """Caller holds the revision lock through deployment insertion."""
        plan = self._revision_plan(revision_id, context, lock=True)
        suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
        policy = self.db.fetch_one(
            "SELECT * FROM evaluation_policies WHERE tenant_id=? AND project_id=?" + suffix,
            (context.tenant_id, context.project_id),
        )
        if not policy:
            raise ConflictError("Production deployment requires an administrator-configured evaluation policy")
        suite = self.get_suite(policy["suite_id"], context)
        latest = self.db.fetch_one(
            """SELECT id FROM evaluation_results WHERE agent_revision_id=? AND suite_id=?
               AND tenant_id=? AND project_id=? ORDER BY sequence DESC LIMIT 1""",
            (revision_id, suite["id"], context.tenant_id, context.project_id),
        )
        if not latest:
            raise ConflictError("Production deployment requires real evaluation evidence for this revision")
        # Admission consumes a certificate, not the private evidence payload.
        result = self._verified_result(latest["id"], context)
        if result["status"] != "PASSED" or not result["production_eligible"]:
            raise ConflictError("Latest evaluation is failed, incomplete, simulated, or has unverified usage")
        if result["evidence"].get("schema_version") != 1:
            raise ConflictError("Evaluation grader version is not supported; run the suite again")
        if result["plan_hash"] != plan["plan_hash"] or result["suite_hash"] != suite["suite_hash"]:
            raise ConflictError("Evaluation does not match the current execution plan and suite")
        age = (self.db.current_time() - datetime.fromisoformat(result["created_at"])).total_seconds()
        if not 0 <= age <= policy["max_age_seconds"]:
            raise ConflictError("Evaluation evidence has expired; run the suite again")
        for sample in result["evidence"]["runs"]:
            sample_age = (self.db.current_time() - datetime.fromisoformat(sample["sample_created_at"])).total_seconds()
            if not 0 <= sample_age <= policy["max_age_seconds"]:
                raise ConflictError("Evaluation executions have expired; re-grading old Runs cannot refresh evidence")
        self._audit(context, "deployment.evaluation.approved", revision_id, {
            "evaluation_id": result["id"], "result_hash": result["result_hash"], "policy_version": policy["version"],
        })
        return result
