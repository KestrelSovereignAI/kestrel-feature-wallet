from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kestrel_feature_wallet.wallet_feature import WalletFeature


class FakeHandler:
    def __init__(self, session):
        self.on_deposit_complete = None
        self.session = session

    async def handle_webhook(self, payload, signature):
        assert payload == b"{}"
        assert signature == "sig"
        await self.on_deposit_complete(self.session)
        return SimpleNamespace(success=True, message="ok")


class FakeAgent:
    def __init__(self, did):
        self.did = did
        self.deposits = []

    async def on_stripe_deposit_complete(self, session):
        self.deposits.append(session)


def test_wallet_feature_exposes_stripe_webhook_router():
    feature = WalletFeature(FakeAgent("did:agent:fallback"))

    router = feature.get_router()

    paths = {route.path for route in router.routes}
    assert "/webhooks/stripe/crypto" in paths


def test_wallet_feature_webhook_routes_completed_deposit_to_owner_agent():
    fallback_agent = FakeAgent("did:agent:fallback")
    owner_agent = FakeAgent("did:agent:owner")
    session = SimpleNamespace(
        session_id="session-1",
        agent_did=owner_agent.did,
    )
    feature = WalletFeature(fallback_agent)
    feature._stripe_webhook_handler = FakeHandler(session)

    app = FastAPI()
    app.state.agent = fallback_agent
    app.state.agent_manager = SimpleNamespace(
        list_agents=lambda: {"owner": owner_agent}
    )
    app.include_router(feature.get_router())

    response = TestClient(app).post(
        "/webhooks/stripe/crypto",
        content=b"{}",
        headers={"Stripe-Signature": "sig"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "received", "message": "ok"}
    assert owner_agent.deposits == [session]
    assert fallback_agent.deposits == []


@pytest.mark.asyncio
async def test_stripe_deposit_resolution_falls_back_to_feature_agent():
    feature_agent = FakeAgent("did:agent:feature")
    feature = WalletFeature(feature_agent)
    session = SimpleNamespace(session_id="session-2", agent_did="missing")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    await feature._dispatch_stripe_deposit_complete(request, session)

    assert feature_agent.deposits == [session]
