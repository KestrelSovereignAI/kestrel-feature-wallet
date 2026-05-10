from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kestrel_feature_wallet.wallet_feature import WalletFeature


class DummyAgent:
    did = "did:kestrel:test-agent"


class FakeEVMAdapter:
    def __init__(self, network):
        self.network = network

    def get_address_from_private_key(self, private_key):
        assert private_key == b"\x01" * 32
        return "0x1111111111111111111111111111111111111111"


class FakeTransactionManager:
    def __init__(self, agent_id, storage_dir):
        self.agent_id = agent_id
        self.storage_dir = storage_dir
        self.closed = False

    async def initialize(self):
        return None

    async def get_token_balance(self, network, token_symbol, address):
        assert network.value == "base_sepolia"
        assert token_symbol == "USDC"
        assert address == "0x1111111111111111111111111111111111111111"
        return Decimal("12.34")

    async def get_balance(self, network, address):
        assert network.value == "base_sepolia"
        assert address == "0x1111111111111111111111111111111111111111"
        return Decimal("0.05")

    async def close(self):
        self.closed = True


@pytest.fixture
def wallet_feature(tmp_path, monkeypatch):
    feature = WalletFeature(DummyAgent())
    feature.wallet = SimpleNamespace(
        agent_id="agent-1",
        db_path=str(tmp_path / "wallet.db"),
        filecoin_address="t1testfilecoinaddress",
        get_on_chain_balance=AsyncMock(return_value=Decimal("1.5")),
    )
    monkeypatch.setattr(
        feature,
        "_load_wallet_private_key_bytes",
        lambda storage_dir: b"\x01" * 32,
    )
    return feature


@pytest.mark.asyncio
async def test_wallet_chain_balance_reports_base_usdc(
    wallet_feature,
    monkeypatch,
):
    monkeypatch.setattr(
        "kestrel_feature_wallet.chain_adapters.evm_adapter.EVMAdapter",
        FakeEVMAdapter,
    )
    monkeypatch.setattr(
        "kestrel_feature_wallet.transaction_manager.TransactionManager",
        FakeTransactionManager,
    )

    result = await wallet_feature.wallet_chain_balance("base_sepolia", "USDC")

    assert "Base Sepolia" in result.confirmation
    assert "0x1111111111111111111111111111111111111111" in result.confirmation
    assert "12.34 USDC" in result.confirmation
    assert "sepolia.basescan.org/address/0x1111111111111111111111111111111111111111" in result.confirmation
    assert result.data["network"] == "base_sepolia"
    assert result.data["token"] == "USDC"


@pytest.mark.asyncio
async def test_wallet_chain_balance_reports_native_balance(
    wallet_feature,
    monkeypatch,
):
    monkeypatch.setattr(
        "kestrel_feature_wallet.chain_adapters.evm_adapter.EVMAdapter",
        FakeEVMAdapter,
    )
    monkeypatch.setattr(
        "kestrel_feature_wallet.transaction_manager.TransactionManager",
        FakeTransactionManager,
    )

    result = await wallet_feature.wallet_chain_balance("base_sepolia", "native")

    assert "Base Sepolia" in result.confirmation
    assert "0.05 ETH" in result.confirmation


@pytest.mark.asyncio
async def test_wallet_chain_balance_requires_key(wallet_feature, monkeypatch):
    monkeypatch.setattr(
        wallet_feature,
        "_load_wallet_private_key_bytes",
        lambda storage_dir: None,
    )

    result = await wallet_feature.wallet_chain_balance("base_sepolia", "USDC")

    assert "No wallet key found" in result.error


@pytest.mark.asyncio
async def test_wallet_address_includes_base_address(wallet_feature, monkeypatch):
    monkeypatch.setattr(
        "kestrel_feature_wallet.chain_adapters.evm_adapter.EVMAdapter",
        FakeEVMAdapter,
    )

    result = await wallet_feature.wallet_address()

    assert "Filecoin" in result.confirmation
    assert "t1testfilecoinaddress" in result.confirmation
    assert "EVM / Base" in result.confirmation
    assert "0x1111111111111111111111111111111111111111" in result.confirmation
    assert "!wallet-chain-balance base_sepolia USDC" in result.confirmation
    assert result.data["evm_address"] == "0x1111111111111111111111111111111111111111"
