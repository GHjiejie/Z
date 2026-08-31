"""Controlled integration fixtures, never evidence of live-provider acceptance."""
from packages.auth.models import UserCreate
from packages.runtime.model_registry import ModelProfile


def user_headers(services, name, role="operator", environment="env_development"):
    user = services.auth.create_user(UserCreate(username=name, display_name=name,
        password="Release-Fixture-2026!", roles=[role], environment_id=environment))
    return {"X-Tenant-ID": "tenant_demo", "X-Project-ID": "project_atlas",
        "X-Environment-ID": environment, "X-User-ID": user["id"], "X-Roles": role}


def authorities(client):
    services = client.app.state.services
    if hasattr(services, "test_release_authorities"):
        return services.test_release_authorities
    admin = user_headers(services, "release_tenant_admin", "tenant_admin")
    requester = user_headers(services, "release_requester")
    reviewer = user_headers(services, "release_reviewer")
    for headers, deploy, approve in ((requester, True, True), (reviewer, False, True)):
        response = client.put("/api/v1/deployment-environment-grants", headers=admin,
            json={"user_id": headers["X-User-ID"], "environment": "production",
                "can_deploy": deploy, "can_approve": approve, "version": 0, "reason": "Reviewed fixture release authority"})
        assert response.status_code == 200, response.text
    services.test_release_authorities = admin, requester, reviewer
    return services.test_release_authorities


def bind_controlled_model(client, gateway):
    services = client.app.state.services
    profile = ModelProfile(id="evaluation-fixture", name="Controlled evaluation fixture",
        tenant_id="tenant_demo", project_id="project_atlas", model="qwen3-235b-a22b",
        base_url="https://evaluation.test/v1", credential_env="DEEPAGENT_MODEL_KEY_EVALUATION",
        input_per_million="1", output_per_million="2")
    services.models.profiles[profile.id] = profile
    original = services.models.gateway
    gateway.identity = profile.identity

    def resolve(plan):
        approved = services.models.validate_plan(plan)
        return gateway if approved and approved.id == profile.id else original(plan)

    services.models.gateway = resolve
    model = client.post("/api/v1/model-deployments", json={
        "profile_id": profile.id, "reason": "Register controlled evaluation fixture"})
    assert model.status_code == 201, model.text
    return model.json()["id"]
