from types import SimpleNamespace

import pytest

from kestrel_feature_wallet.x402_buyer import (
    X402Buyer,
    X402PaidResponse,
    X402PaymentReceipt,
)


class FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.status_code = 200
        self.text = "ok"


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.response


class FakePaymentClient:
    def __init__(self, settle_response=None):
        self.settle_response = settle_response

    def get_payment_settle_response(self, get_header):
        if get_header("X-PAYMENT-RESPONSE") is None:
            raise ValueError("Payment response header not found")
        return self.settle_response


def test_x402_payment_receipt_serializes_model_dump():
    class ModelLike:
        def model_dump(self, mode):
            assert mode == "json"
            return {"txHash": "0xabc"}

    response = FakeResponse(headers={"X-PAYMENT-RESPONSE": "encoded"})
    buyer = X402Buyer(
        private_key=b"\x01" * 32,
        payment_client_factory=lambda client: FakePaymentClient(ModelLike()),
    )

    receipt = buyer._extract_receipt(response, buyer._build_payment_client(object()))

    assert isinstance(receipt, X402PaymentReceipt)
    assert receipt.settle_response == {"txHash": "0xabc"}
    assert receipt.response_headers == {"X-PAYMENT-RESPONSE": "encoded"}


def test_x402_payment_receipt_absent_without_header():
    response = FakeResponse()
    buyer = X402Buyer(
        private_key=b"\x01" * 32,
        payment_client_factory=lambda client: FakePaymentClient(),
    )

    receipt = buyer._extract_receipt(response, buyer._build_payment_client(object()))

    assert receipt is None


def test_x402_policy_filters_base_mainnet_without_opt_in():
    buyer = X402Buyer(private_key=b"\x01" * 32, allow_mainnet=False)

    filtered = buyer._payment_policy(
        2,
        [
            SimpleNamespace(network="eip155:8453", amount="10000"),
            SimpleNamespace(network="eip155:84532", amount="10000"),
        ],
    )

    assert [req.network for req in filtered] == ["eip155:84532"]


def test_x402_policy_filters_oversized_usdc_invoice():
    buyer = X402Buyer(
        private_key=b"\x01" * 32,
        max_usdc_per_request="0.25",
    )

    filtered = buyer._payment_policy(
        2,
        [
            SimpleNamespace(network="eip155:84532", amount="250000"),
            SimpleNamespace(network="eip155:84532", amount="250001"),
        ],
    )

    assert [req.amount for req in filtered] == ["250000"]


@pytest.mark.asyncio
async def test_request_with_payment_uses_injected_clients(monkeypatch):
    fake_response = FakeResponse(headers={"X-PAYMENT-RESPONSE": "encoded"})
    fake_http = FakeHttpClient(fake_response)
    fake_payment = FakePaymentClient({"transaction": "0xabc"})

    buyer = X402Buyer(
        private_key=b"\x01" * 32,
        http_client_factory=lambda client, kwargs: fake_http,
        payment_client_factory=lambda client: fake_payment,
    )
    monkeypatch.setattr(buyer, "_build_x402_client", lambda: SimpleNamespace())

    paid = await buyer.request_with_payment(
        "POST",
        "https://example.test/paid",
        content=b"hello",
    )

    assert isinstance(paid, X402PaidResponse)
    assert paid.response is fake_response
    assert paid.receipt.settle_response == {"transaction": "0xabc"}
    assert fake_http.requests == [
        ("POST", "https://example.test/paid", {"content": b"hello"})
    ]
