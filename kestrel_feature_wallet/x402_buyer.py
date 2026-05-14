"""
x402 buyer client for wallet-backed paid HTTP requests.

This module keeps x402 dependencies lazy so the wallet package remains usable
without the x402 extra installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
from typing import Any, Callable, Mapping, Optional


class X402BuyerError(RuntimeError):
    """Raised when x402 buyer setup or payment handling fails."""


@dataclass(frozen=True)
class X402PaymentReceipt:
    """Serializable payment metadata for audit trails."""

    response_headers: dict[str, str]
    settle_response: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class X402PaidResponse:
    """HTTP response plus optional payment receipt metadata."""

    response: Any
    receipt: Optional[X402PaymentReceipt] = None


HttpClientFactory = Callable[[Any, Mapping[str, Any]], Any]
PaymentClientFactory = Callable[[Any], Any]

BASE_SEPOLIA_NETWORK = "eip155:84532"
BASE_MAINNET_NETWORKS = {"eip155:8453", "base"}
DEFAULT_MAX_USDC_PER_REQUEST = Decimal("1")
USDC_DECIMALS = Decimal("1000000")


def _serialize_settle_response(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": repr(value)}


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def _coerce_max_usdc(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        env_value = os.environ.get("KESTREL_X402_MAX_USDC_PER_REQUEST")
        if not env_value:
            return DEFAULT_MAX_USDC_PER_REQUEST
        value = env_value

    if isinstance(value, Decimal):
        return value

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise X402BuyerError("Invalid KESTREL_X402_MAX_USDC_PER_REQUEST value") from exc


def _network_values(networks: str | list[str] | None) -> list[str]:
    if networks is None:
        return []
    if isinstance(networks, str):
        return [networks]
    return list(networks)


def _network_is_mainnet(network: str) -> bool:
    return network in BASE_MAINNET_NETWORKS or network.endswith(":8453")


class X402Buyer:
    """
    Make HTTP requests with automatic x402 payment handling.

    The default implementation uses the Python x402 SDK's httpx transport. Tests
    can inject lightweight factories to avoid network calls or x402 imports.
    """

    def __init__(
        self,
        private_key: bytes | str,
        *,
        networks: str | list[str] | None = BASE_SEPOLIA_NETWORK,
        allow_mainnet: Optional[bool] = None,
        max_usdc_per_request: Decimal | str | int | float | None = None,
        http_client_kwargs: Optional[dict[str, Any]] = None,
        http_client_factory: Optional[HttpClientFactory] = None,
        payment_client_factory: Optional[PaymentClientFactory] = None,
    ) -> None:
        self.private_key = private_key
        self.networks = networks
        self.allow_mainnet = (
            _truthy_env(os.environ.get("KESTREL_ALLOW_MAINNET"))
            if allow_mainnet is None
            else allow_mainnet
        )
        self.max_usdc_per_request = _coerce_max_usdc(max_usdc_per_request)
        self.http_client_kwargs = http_client_kwargs or {}
        self._http_client_factory = http_client_factory
        self._payment_client_factory = payment_client_factory

    def _build_x402_client(self) -> Any:
        try:
            from eth_account import Account
            from x402 import x402Client
            from x402.mechanisms.evm import EthAccountSigner
            from x402.mechanisms.evm.exact.register import register_exact_evm_client
        except ImportError as exc:
            raise X402BuyerError(
                "x402 buyer support requires the x402 extra: "
                "pip install 'kestrel-feature-wallet[x402]'"
            ) from exc

        account = Account.from_key(self.private_key)
        signer = EthAccountSigner(account)
        client = x402Client()
        register_exact_evm_client(
            client,
            signer,
            networks=self.networks,
            policies=[self._payment_policy],
        )

        try:
            from x402.mechanisms.evm.upto.client import UptoEvmScheme

            upto_networks = _network_values(self.networks) or ["eip155:*"]
            for network in upto_networks:
                client.register(network, UptoEvmScheme(signer))
        except ImportError:
            # Older x402 versions may not expose the upto client; exact still
            # supports fixed-price payment flows.
            pass

        return client

    def _payment_policy(self, version: int, requirements: list[Any]) -> list[Any]:
        filtered = []
        for requirement in requirements:
            network = str(getattr(requirement, "network", ""))
            if not self.allow_mainnet and _network_is_mainnet(network):
                continue
            if self.max_usdc_per_request is not None:
                try:
                    amount_usdc = Decimal(str(requirement.amount)) / USDC_DECIMALS
                except (AttributeError, InvalidOperation):
                    continue
                if amount_usdc > self.max_usdc_per_request:
                    continue
            filtered.append(requirement)
        return filtered

    def _build_http_client(self, client: Any) -> Any:
        if self._http_client_factory:
            return self._http_client_factory(client, self.http_client_kwargs)

        try:
            from x402.http.clients import x402HttpxClient
        except ImportError as exc:
            raise X402BuyerError(
                "x402 HTTP support requires x402[httpx]; install "
                "'kestrel-feature-wallet[x402]'"
            ) from exc

        return x402HttpxClient(client, **self.http_client_kwargs)

    def _build_payment_client(self, client: Any) -> Any:
        if self._payment_client_factory:
            return self._payment_client_factory(client)

        try:
            from x402.http import x402HTTPClient
        except ImportError as exc:
            raise X402BuyerError(
                "x402 HTTP support requires x402[httpx]; install "
                "'kestrel-feature-wallet[x402]'"
            ) from exc

        return x402HTTPClient(client)

    async def request_with_payment(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> X402PaidResponse:
        """Make a paid request and return response plus payment metadata."""

        client = self._build_x402_client()
        payment_client = self._build_payment_client(client)

        async with self._build_http_client(client) as http:
            response = await http.request(method, url, **kwargs)

        receipt = self._extract_receipt(response, payment_client)
        return X402PaidResponse(response=response, receipt=receipt)

    async def post_with_payment(self, url: str, **kwargs: Any) -> X402PaidResponse:
        return await self.request_with_payment("POST", url, **kwargs)

    async def get_with_payment(self, url: str, **kwargs: Any) -> X402PaidResponse:
        return await self.request_with_payment("GET", url, **kwargs)

    def _extract_receipt(
        self,
        response: Any,
        payment_client: Any,
    ) -> Optional[X402PaymentReceipt]:
        try:
            settle_response = payment_client.get_payment_settle_response(
                lambda name: response.headers.get(name)
            )
        except ValueError:
            return None

        return X402PaymentReceipt(
            response_headers=dict(response.headers),
            settle_response=_serialize_settle_response(settle_response),
        )
