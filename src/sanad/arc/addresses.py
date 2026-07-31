"""The Arc address book, every entry verified live on testnet.

Read out of Circle's own node repo (`contracts/scripts/Addresses.sol` and
`contracts/src/Precompiles.sol` in `circlefin/arc-node`) and then checked against
`https://rpc.testnet.arc.network` with `cast code` and a functional read. The
CHANGELOG names Memo, Multicall3From and the CallFrom precompile as the Zero7
hardfork.

Verification notes worth keeping next to the constants, because they cost time to
learn:

* Precompiles hold a one byte code, not `0xef`, so "has code" is the liveness test
  and a byte comparison is not.
* The whole `0x1800...00` to `0x1800...ff` range is reserved in genesis, so nothing
  can ever be deployed there.
* The Denylist sits at a CREATE2 mined address on testnet and at the next system
  contract slot on mainnet, so it is the one address here that moves per network.
* USDC is both the gas token and an ERC-20. One balance, two views: native at 18
  decimals, ERC-20 at 6, so the factor between them is 1e12.
"""

from __future__ import annotations

from typing import Final

CHAIN_ID: Final[int] = 5042002
RPC_URL: Final[str] = "https://rpc.testnet.arc.network"
EXPLORER: Final[str] = "https://testnet.arcscan.app"
FAUCET: Final[str] = "https://faucet.circle.com"

# System contracts. The 0x360 prefix is the reserved system range.
USDC: Final[str] = "0x3600000000000000000000000000000000000000"
PROTOCOL_CONFIG: Final[str] = "0x3600000000000000000000000000000000000001"
VALIDATOR_REGISTRY: Final[str] = "0x3600000000000000000000000000000000000002"
PERMISSIONED_VALIDATOR_MANAGER: Final[str] = "0x3600000000000000000000000000000000000003"

# EURC is the same FiatToken build as USDC, so it carries the same EIP-3009 surface.
EURC: Final[str] = "0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a"

# Denylist. Testnet is a CREATE2 address mined under the 0x360 prefix. Mainnet takes
# the next system slot, which is empty on testnet by design.
DENYLIST_TESTNET: Final[str] = "0x360b451bb0490637F52fa1794961455615777757"
DENYLIST_MAINNET_SLOT: Final[str] = "0x3600000000000000000000000000000000000004"

# Precompiles. Implemented in the node, so they carry a one byte code and no bytecode.
NATIVE_COIN_AUTHORITY: Final[str] = "0x1800000000000000000000000000000000000000"
NATIVE_COIN_CONTROL: Final[str] = "0x1800000000000000000000000000000000000001"
SYSTEM_ACCOUNTING: Final[str] = "0x1800000000000000000000000000000000000002"
CALL_FROM: Final[str] = "0x1800000000000000000000000000000000000003"
PQ: Final[str] = "0x1800000000000000000000000000000000000004"

# Zero7 hardfork contracts. These two are the only addresses the CallFrom allowlist
# accepts, so every sender preserving call has to go through one of them.
MEMO: Final[str] = "0x5294E9927c3306DcBaDb03fe70b92e01cCede505"
MULTICALL3_FROM: Final[str] = "0x522fAf9A91c41c443c66765030741e4AaCe147D0"

# Predeploys inherited from the wider EVM ecosystem.
MULTICALL3: Final[str] = "0xcA11bde05977b3631167028862bE2a173976CA11"
PERMIT2: Final[str] = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
CREATE2_FACTORY: Final[str] = "0x4e59b44847b379578588920cA78FbF26c0B4956C"

# Event topics, read off the chain rather than computed, so a signature typo cannot
# silently produce a filter that matches nothing.
TOPIC_MEMO: Final[str] = "0xeb15ee720798341c37739df41be53acfbbf70ae6802dade35457beec6e47a5e4"
TOPIC_BEFORE_MEMO: Final[str] = (
    "0xb252e055da754c72fbf7542cf424b190808a9b541e912894c5e15b4238c41501"
)
TOPIC_TRANSFER: Final[str] = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

USDC_DECIMALS: Final[int] = 6
NATIVE_DECIMALS: Final[int] = 18
NATIVE_TO_ERC20_FACTOR: Final[int] = 10 ** (NATIVE_DECIMALS - USDC_DECIMALS)

# SLH-DSA-SHA2-128s, FIPS 205, as the PQ precompile implements it.
PQ_VERIFYING_KEY_LEN: Final[int] = 32
PQ_SIGNATURE_LEN: Final[int] = 7856
PQ_VERIFY_BASE_GAS: Final[int] = 230_000


def tx_url(tx_hash: str) -> str:
    return f"{EXPLORER}/tx/{tx_hash}"


def address_url(address: str) -> str:
    return f"{EXPLORER}/address/{address}"


def is_system_address(address: str) -> bool:
    """True for the reserved 0x360 system contract range, matching the node's own
    `_hasSystemAddressPrefix` helper (top 12 bits equal to 0x360)."""
    return (int(address, 16) >> 148) == 0x360
