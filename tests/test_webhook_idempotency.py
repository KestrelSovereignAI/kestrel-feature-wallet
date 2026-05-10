from decimal import Decimal

import pytest

from kestrel_feature_wallet.onramp.stripe_onramp import OnRampSession, OnRampStatus
from kestrel_feature_wallet.onramp.webhook_handler import StripeWebhookHandler


class FakeOnRamp:
    def __init__(self):
        self.session = OnRampSession(
            session_id="local-session",
            agent_did="did:agent:owner",
            wallet_address="0x123",
            destination_currency="ETH",
            destination_network="ethereum",
            stripe_session_id="stripe-session",
        )

    def get_session_by_stripe_id(self, stripe_session_id):
        if stripe_session_id == self.session.stripe_session_id:
            return self.session
        return None

    async def update_session_status(
        self,
        session_id,
        status,
        crypto_amount=None,
        fiat_amount=None,
    ):
        assert session_id == self.session.session_id
        self.session.status = status
        if crypto_amount is not None:
            self.session.crypto_amount = crypto_amount
        if fiat_amount is not None:
            self.session.fiat_amount = fiat_amount
        return self.session

    def _save_session(self, session):
        self.session = session


def _completed_event():
    return {
        "data": {
            "object": {
                "id": "stripe-session",
                "transaction_details": {
                    "destination_amount": "0.25",
                    "source_amount": "100",
                },
            }
        }
    }


def _succeeded_update_event_without_amounts():
    return {
        "data": {
            "object": {
                "id": "stripe-session",
                "status": "succeeded",
            }
        }
    }


@pytest.mark.asyncio
async def test_completed_webhook_callback_is_idempotent(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    handler = StripeWebhookHandler(FakeOnRamp())
    callbacks = []

    async def on_complete(session):
        callbacks.append(session.session_id)

    handler.on_deposit_complete = on_complete

    first = await handler._handle_session_completed(_completed_event())
    duplicate = await handler._handle_session_completed(_completed_event())

    assert first.success is True
    assert duplicate.success is True
    assert duplicate.message == "Deposit already completed"
    assert callbacks == ["local-session"]
    assert handler.onramp.session.status == OnRampStatus.SUCCEEDED
    assert handler.onramp.session.crypto_amount == Decimal("0.25")
    assert handler.onramp.session.metadata["deposit_callback_dispatched"] is True


@pytest.mark.asyncio
async def test_succeeded_update_claims_completion_before_completed_retry(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    handler = StripeWebhookHandler(FakeOnRamp())
    callbacks = []

    async def on_complete(session):
        callbacks.append(session.session_id)

    handler.on_deposit_complete = on_complete
    updated = await handler._handle_session_updated(
        _succeeded_update_event_without_amounts()
    )
    assert callbacks == []

    completed_retry = await handler._handle_session_completed(_completed_event())

    assert updated.success is True
    assert completed_retry.success is True
    assert callbacks == ["local-session"]
    assert handler.onramp.session.crypto_amount == Decimal("0.25")
    assert handler.onramp.session.fiat_amount == Decimal("100")


@pytest.mark.asyncio
async def test_failed_completion_callback_is_not_marked_dispatched(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    handler = StripeWebhookHandler(FakeOnRamp())

    async def on_complete(session):
        raise RuntimeError("dispatcher unavailable")

    handler.on_deposit_complete = on_complete

    result = await handler._handle_session_completed(_completed_event())

    assert result.success is True
    assert "deposit_callback_dispatched" not in handler.onramp.session.metadata
