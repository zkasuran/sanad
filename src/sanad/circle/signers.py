"""Where the payer's key lives, behind one narrow interface.

Two backends. `LocalSigner` holds a key in this process, which is right for a demo and
for a self custodying payer. `CircleWalletSigner` holds nothing: the key sits in a Circle
developer controlled wallet and signing is an API call, so a treasury can authorize a
payout run without the application ever being able to move its money on its own.

The interface is deliberately one method. Everything Sanad asks a payer to authorize is
EIP-712 typed data, either an EIP-3009 transfer authorization or a run mandate, so a
backend that can sign typed data can drive the whole rail and nothing else in the codebase
learns where the key is.

Circle requires the entity secret to be re-encrypted for every request, which is why the
ciphertext arrives here as a factory rather than a value. That detail, and the shape of
the SDK call, were established in the sibling MoonWalk build
(`work/moonwalk/src/circle/wallets.py`) where this path was exercised live against Circle.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from web3 import Web3

from .eip3009 import TypedData


class SigningError(RuntimeError):
    """A signer that could not sign, with the reason a human needs."""


def jsonable_typed_data(typed_data: TypedData) -> TypedData:
    """Circle wants the payload as JSON, so every value has to survive `json.dumps`.

    Only `bytes` and integers wider than JSON's comfort zone need care. Bytes become hex
    strings, everything else passes through untouched.
    """

    def convert(value: Any) -> Any:
        if isinstance(value, bytes):
            return "0x" + value.hex()
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return dict(convert(typed_data))


@runtime_checkable
class Signer(Protocol):
    """An address, a name for where the key is, and a way to sign typed data."""

    @property
    def backend(self) -> str: ...

    @property
    def address(self) -> str: ...

    def sign_typed_data(self, typed_data: TypedData) -> bytes: ...


class LocalSigner:
    """The key is in this process."""

    def __init__(self, account: LocalAccount) -> None:
        self._account = account

    @property
    def backend(self) -> str:
        return "local"

    @property
    def address(self) -> str:
        return str(self._account.address)

    @property
    def account(self) -> LocalAccount:
        """The underlying eth_account signer, for the paths that need to send a
        transaction rather than sign a message."""
        return self._account

    def sign_typed_data(self, typed_data: TypedData) -> bytes:
        signed = self._account.sign_message(encode_typed_data(full_message=typed_data))
        return bytes(signed.signature)

    @classmethod
    def from_key(cls, private_key: str) -> LocalSigner:
        key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        return cls(Account.from_key(key))

    def __repr__(self) -> str:
        # Never let a key reach a log line through a repr.
        return f"LocalSigner(address={self.address})"


class TypedDataSigningApi(Protocol):
    """The one Circle SDK call this module makes, narrowed so a test can stand in for
    Circle without a network."""

    def sign_typed_data(self, sign_typed_data_request: Any) -> Any: ...


class CircleWalletSigner:
    """The key lives in Circle's infrastructure. This process never holds it.

    Signing is `POST /v1/w3s/developer/sign/typedData`. Sanad sends the EIP-712 payload as
    JSON, Circle signs with the wallet's key inside its own custody and returns 65 bytes.
    """

    def __init__(
        self,
        api: TypedDataSigningApi,
        *,
        wallet_id: str,
        address: str,
        entity_secret_ciphertext: Callable[[], str],
        memo: str = "Sanad payout authorization",
    ) -> None:
        self._api = api
        self._wallet_id = wallet_id
        self._address = Web3.to_checksum_address(address)
        self._ciphertext = entity_secret_ciphertext
        self._memo = memo

    @property
    def backend(self) -> str:
        return "circle-developer-controlled"

    @property
    def address(self) -> str:
        return str(self._address)

    @property
    def wallet_id(self) -> str:
        return self._wallet_id

    def sign_typed_data(self, typed_data: TypedData) -> bytes:
        from circle.web3 import developer_controlled_wallets as dcw

        payload = json.dumps(jsonable_typed_data(typed_data), separators=(",", ":"))
        request = dcw.SignTypedDataRequest.from_dict(
            {
                "walletId": self._wallet_id,
                "data": payload,
                "memo": self._memo,
                "entitySecretCiphertext": self._ciphertext(),
            }
        )
        try:
            response = self._api.sign_typed_data(sign_typed_data_request=request)
        except dcw.ApiException as exc:
            raise SigningError(f"Circle refused to sign: {exc.status} {exc.reason}") from exc
        signature = getattr(getattr(response, "data", None), "signature", None)
        if not isinstance(signature, str) or not signature.startswith("0x"):
            raise SigningError(f"Circle returned no usable signature: {response!r}")
        raw = bytes.fromhex(signature[2:])
        if len(raw) != 65:
            raise SigningError(f"expected a 65 byte signature, got {len(raw)}")
        return raw

    @classmethod
    def from_env(cls, *, memo: str = "Sanad payout authorization") -> CircleWalletSigner:
        """Build from the CIRCLE_* variables.

        This talks to Circle on construction, because the SDK fetches the entity public
        key so the entity secret can be encrypted. That is why it is not test safe, and
        why tests inject an api object instead.
        """
        from circle.web3 import developer_controlled_wallets as dcw
        from circle.web3 import utils as circle_utils  # type: ignore[attr-defined]

        missing = [
            name
            for name in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET", "CIRCLE_WALLET_ID")
            if not os.getenv(name)
        ]
        if missing:
            raise SigningError(f"missing environment: {', '.join(missing)}")
        api_key = os.environ["CIRCLE_API_KEY"]
        entity_secret = os.environ["CIRCLE_ENTITY_SECRET"]
        wallet_id = os.environ["CIRCLE_WALLET_ID"]
        client = circle_utils.init_developer_controlled_wallets_client(
            api_key=api_key, entity_secret=entity_secret
        )
        address = os.getenv("CIRCLE_WALLET_ADDRESS", "")
        if not address:
            wallet = dcw.WalletsApi(client).get_wallet(id=wallet_id)  # type: ignore[no-untyped-call]
            address = str(wallet.data.wallet.address)
        return cls(
            dcw.SigningApi(client),  # type: ignore[no-untyped-call]
            wallet_id=wallet_id,
            address=address,
            # Circle mandates a fresh ciphertext per request, so this runs per signature
            # and is never cached.
            entity_secret_ciphertext=lambda: str(
                circle_utils.generate_entity_secret_ciphertext(api_key, entity_secret)
            ),
            memo=memo,
        )

    def __repr__(self) -> str:
        # No api key, no entity secret, no ciphertext. Only public identifiers.
        return f"CircleWalletSigner(wallet_id={self._wallet_id}, address={self.address})"


def signer_from_env(backend: str | None = None) -> Signer:
    """Pick a backend. `SANAD_SIGNER=circle` puts the payer's key in Circle's custody,
    anything else signs locally."""
    choice = (backend or os.environ.get("SANAD_SIGNER", "local")).strip().lower()
    if choice in {"circle", "circle-developer-controlled"}:
        return CircleWalletSigner.from_env()
    if choice not in {"local", ""}:
        raise ValueError(f"unknown signer backend {choice!r}, expected 'local' or 'circle'")
    key = os.getenv("SANAD_PAYER_KEY", "") or os.getenv("DEPLOYER_PRIVATE_KEY", "")
    if not key:
        raise ValueError("the local signer needs SANAD_PAYER_KEY or DEPLOYER_PRIVATE_KEY")
    return LocalSigner.from_key(key)
