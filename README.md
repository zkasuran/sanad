# Sanad

**Proof carrying payouts on Arc.** Every USDC payment carries its own ISO 20022
instruction on chain, batches settle from the payer's own address, and the entire
ledger, the regulator view and every counterparty history rebuild from Arc alone
because they were never stored anywhere else.

[![Arc testnet](https://img.shields.io/badge/live_on-Arc_testnet_5042002-1f1f1f)](https://testnet.arcscan.app/address/0xC73090627ac5ed6fb651807c6A3E64D2FFa34194)
[![pytest](https://img.shields.io/badge/pytest-25_passing-0f8a56)](#tests)
[![forge](https://img.shields.io/badge/forge-10_passing-0f8a56)](#tests)
[![Circle](https://img.shields.io/badge/Circle-USDC_%2B_Wallets-2775CA)](#circle-products-and-how-they-are-used)

Sanad is Arabic for the deed, and in the classical sciences for the chain of
transmission that makes a report trustworthy. That is the whole thesis: the payment
and its proof travel together.

## The problem, in one paragraph

The Central Bank of the UAE requires a purpose of payment code on every outbound
transfer. HSBC UAE, to its own customers: "With effect from 1st September 2018, PoP
codes will become mandatory for all outbound cross border payments initiated from
HSBC UAE accounts." Nordea, on where the code goes: "A 3-letter purpose code should
be used for all payments to UAE, irrespectively of currency. The code must be placed
in the first line of the details of payment / message to the beneficiary field."

So a field the central bank mandates has no structured home in the message. It is
typed into the first line of a free text box. A plain USDC transfer does not even
have the free text box, which is why every stablecoin rail today keeps the
instruction in a private database and the audit trail becomes a row somebody can
edit.

Arc closes that gap in the protocol, and almost nobody has noticed.

## What is live right now

| What | Where |
|--|--|
| `SanadMandate`, ours | [`0xC73090627ac5ed6fb651807c6A3E64D2FFa34194`](https://testnet.arcscan.app/address/0xC73090627ac5ed6fb651807c6A3E64D2FFa34194) |
| A four payee run, self submitted | [`0x296796c8…8520f4`](https://testnet.arcscan.app/tx/0x296796c8689eb4e53972271f2bb8f143c629eed9dd889ae35596d3b6eb8520f4) |
| A run authorized by a payer, submitted by an operator | [`0x721e4e3b…107dcf`](https://testnet.arcscan.app/tx/0x721e4e3b0cabebb9c834013ad884222356e4bf60609612728ec2caf73a107dcf) |
| The same, with the payer's key inside a **Circle developer controlled wallet** | [`0x98128c11…0c8e72`](https://testnet.arcscan.app/tx/0x98128c1128d7582424ae07ec899730cd22e137a636769ba920b18cb1e40c8e72) |

Every receipt is committed under `evidence/`. Cost, measured rather than estimated:
**0.00164 to 0.00179 USDC per payee**, including the mandate anchor and the full
instruction, in a single transaction.

## The Arc primitives this is built on

These ship with Arc, they are live on testnet, and they are in the node repository
(`circlefin/arc-node`, the Zero7 hardfork) rather than in the developer docs.

| Primitive | Address | What it gives us |
|--|--|--|
| **Memo** | `0x5294E9927c3306DcBaDb03fe70b92e01cCede505` | `memo(target, data, memoId, memoData)` runs the inner call through the CallFrom precompile, so the target still sees the payer's EOA as `msg.sender`, then emits the payload with `memoId` indexed and a `callDataHash` binding the memo to the exact call it describes |
| **Multicall3From** | `0x522fAf9A91c41c443c66765030741e4AaCe147D0` | batching that preserves the from address, so N payouts settle in one transaction and every USDC `Transfer` still reads as coming from the payer |
| **CallFrom precompile** | `0x1800000000000000000000000000000000000003` | sender preservation. Its allowlist is hardcoded to Memo and Multicall3From, so those two are the only doors |
| **PQ precompile** | `0x1800000000000000000000000000000000000004` | `verifySlhDsaSha2128s(vk, message, sig)`, SLH-DSA-SHA2-128s, FIPS 205. A run can carry a post quantum signature over its digest, verified on chain |
| **Denylist** | `0x360b451bb0490637F52fa1794961455615777757` | the chain's own compliance list. Screening a run is a read, not a vendor subscription |

### The composition, which is the actual finding

Memo and Multicall3From both document themselves as EOA only, because CallFrom
insists the `sender` argument equals either the precompile caller or `tx.origin`. Read
separately they look uncomposable. Nest them and the EOA survives both hops:
Multicall3From passes `sender = tx.origin`, so Memo sees the EOA as its own
`msg.sender`, and Memo's own CallFrom call then passes the same test.

What falls out is a batch in which **every payment carries its own instruction**. We
have not found another project doing this. A GitHub code search on 2026-07-31 found
Memo used by four repositories and Multicall3From by six, with no overlap, and the PQ
precompile and the Denylist used by nobody outside Circle's own repository.

## Two settlement modes, one audit

**Self submitted.** The payer sends the transaction. One `aggregate3` carries the
mandate plus one memo wrapped `transfer` per payee.

**Payer authorized, operator submitted.** The payer signs one EIP-3009
`TransferWithAuthorization` per payout and never broadcasts anything. An operator
batches them and pays the gas. Because `transferWithAuthorization` takes the sender
from the signature rather than from `msg.sender`, the USDC `Transfer` events still name
the payer. In the receipt for `0x98128c11…0c8e72` the payer's transaction count is
**zero** and its money moved anyway.

Both modes produce the same audit, because the audit is derived from the chain and not
from the code path that produced it.

## The audit, which is the point

`python scripts/rebuild_ledger.py` takes an RPC URL and the mandate address. That is
all. No key, no database, no local state. It runs three log queries plus one
transaction read per run, and then does arithmetic:

1. **The instruction belongs to its payment.** Memo records `callDataHash`. The rebuild
   reads the batch back out of the transaction input, hashes each inner call and checks
   the memo committed to exactly that call. A memo cannot be re paired with a different
   transfer after the fact.
2. **The payments match what was authorized.** The mandate anchored a digest over the
   payer, the token, the chain and every payee with its amount and memo id in order.
   Every input is recoverable from chain data, so the digest is recomputed and compared.
   A run that paid someone outside its mandate fails here.
3. **The payer is the payer.** Every USDC `Transfer` carries the payer's own address, so
   there is no pool, bridge or operator wallet to take on trust.

The current state of the chain, from the rebuild:

```
3 run(s), 8 payment(s), 0.076250 USDC
verdict: every run reconciles against its own mandate
         3 of 3 run(s) had the authorization recomputed and matched
```

## Circle products, and how they are used

**USDC on Arc**, `0x3600000000000000000000000000000000000000`. The settlement asset and
the gas token. Used through both `transfer` and, in the authorized mode, the EIP-3009
`transferWithAuthorization` path that lets a payer authorize without broadcasting. The
EIP-712 domain was verified against the live contract: name `USDC`, version `2`,
chainId `5042002`.

**Circle Wallets, developer controlled.** In the authorized mode the payer's key lives
in Circle's custody and this application never holds it. `src/sanad/circle/signers.py`
sends the EIP-712 payload to `POST /v1/w3s/developer/sign/typedData`, Circle signs
inside its own custody and returns 65 bytes, and the entity secret is re-encrypted for
every request. Exercised live: transaction `0x98128c11…0c8e72` moved USDC out of Circle
wallet `0x5074B92189e295f46597037F3b972786578D05d2` while that wallet's transaction
count stayed at zero.

**EURC** is deployed on Arc at `0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a` and is the
same FiatToken build, so the same instruction path works for a euro leg. Not exercised
in this build, and the code does not pretend otherwise.

Product feedback for Circle is in [`docs/CIRCLE-PRODUCT-FEEDBACK.md`](docs/CIRCLE-PRODUCT-FEEDBACK.md).

## The instruction format

Packed, big endian, no padding, because memo bytes are calldata and event data and every
byte is paid for on every payment. A supplier payment with a structured invoice reference
and both purpose codes fits in about 60 bytes. The first eight bytes read as ASCII, so a
human looking at raw hex on Arcscan sees `SNAD`, then the ISO 20022 purpose code, then
the end to end id in the clear:

```
0x534e4144 01 09 53555050 0b 5745422d544553542d3031 00 474449 ...
   S N A D  v  fl  S U P P  11  W E B - T E S T - 0 1     G D I
```

Field names, the four character ExternalPurpose1Code and the Max35Text limits are the
real ISO 20022 ones. The 112 CBUAE purpose codes in `src/sanad/uaefts.py` were extracted
from the published tables of HSBC UAE and Nordea and cross checked against Citi and
National Bank of Oman UAE, with the codes the Central Bank withdrew in 2018 kept so a
stale input is named rather than just rejected. Full layout in
[`src/sanad/iso20022.py`](src/sanad/iso20022.py).

`memoId` is `keccak256(endToEndId)`, and it is an indexed topic, so looking a payment up
by its invoice reference is one `eth_getLogs` filter rather than a scan.

## The web app, which is where you see all of it

```
uvicorn sanad.api.app:app --app-dir src --port 8099
```

Then open `http://127.0.0.1:8099`. Three tabs. The second one is the point.

**Build a run.** A payee table where each row carries the amount, the invoice
reference, the ISO 20022 purpose code and the CBUAE three letter code. "Load a Dubai
SME run" fills a realistic four payee run. "Screen and simulate" runs the denylist
screening, encodes the instruction, prices the whole run and shows you the exact bytes
that will go on chain, all without sending anything. "Settle on Arc" stays disabled
until a simulation has passed, so it is the only button in the app that spends.

**Audit from the chain.** Press rebuild and the page reconstructs every run from Arc
with three log queries and one transaction read per run, then arithmetic. No database
is consulted because there is not one. It reports whether each mandate digest
recomputes to what was anchored, breaks the totals down by purpose code, then derives a
counterparty history, which is the thing a lender actually reads as credit. There is
also a lookup that finds a payment by its invoice reference: the memo id is the keccak
of the ISO 20022 end to end id and it is an indexed topic, so that is one log filter
rather than a scan.

**What this uses.** The Arc primitives and the Circle products, with the addresses read
from the chain when the page loads rather than hardcoded into it, so the page cannot
claim an integration that is not there.

The API behind it is five routes (`/api/config`, `/api/ledger`, `/api/runs/preview`,
`/api/runs/settle`, `/api/docs`). Only the settle route needs a key, so the whole audit
half of the app runs against Arc with no credentials at all.

## Quickstart

```bash
git clone <this repo> && cd sanad
uv venv --python 3.11 && . .venv/bin/activate
uv pip install -e '.[dev]'

cp .env.example .env          # then fill in the keys below
python scripts/settle_run.py            # dry run: screens, simulates, prices, sends nothing
python scripts/settle_run.py --send     # settles one run on Arc testnet
python scripts/rebuild_ledger.py        # the audit. no key needed
uvicorn sanad.api.app:app --app-dir src --port 8099   # the web app on :8099
```

Environment:

| Variable | For |
|--|--|
| `SANAD_PAYER_KEY` | the payer, in self submitted mode. `DEPLOYER_PRIVATE_KEY` also works |
| `SANAD_RELAYER_KEY` | the operator that submits and pays gas, in authorized mode |
| `SANAD_SIGNER` | `local` or `circle` |
| `CIRCLE_API_KEY`, `CIRCLE_ENTITY_SECRET`, `CIRCLE_WALLET_ID` | the Circle developer controlled wallet, when `SANAD_SIGNER=circle` |

Testnet USDC comes from [faucet.circle.com](https://faucet.circle.com). USDC is also the
gas token on Arc, so one balance covers both.

Contracts:

```bash
cd contracts && forge test          # 10 tests, the PQ precompile is mocked locally
forge create src/SanadMandate.sol:SanadMandate --rpc-url https://rpc.testnet.arc.network \
  --private-key $DEPLOYER_PRIVATE_KEY --broadcast
```

## Architecture

Diagram: [`docs/ARCHITECTURE.svg`](docs/ARCHITECTURE.svg), with the walkthrough in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
src/sanad/
  iso20022.py      the on chain instruction and its wire format
  uaefts.py        112 CBUAE purpose of payment codes, with provenance
  payouts.py       a run, its mandate digest, and the calls it becomes
  mandate.py       SanadMandate calldata, the run header, event decoding
  ledger.py        rebuild every run from Arc and prove it
  chain.py         reaching Arc, signing, log reads. The key is optional
  arc/
    addresses.py   the Arc address book, every entry verified live
    memo.py        Memo calldata in, Memo events out, both directions
    batch.py       Multicall3From, and the nesting that makes it work
    denylist.py    screening against the chain's own list
  circle/
    eip3009.py     authorizations, so the payer signs and an operator submits
    signers.py     local key, or a Circle developer controlled wallet
  api/app.py       the API, and it serves the web app
contracts/src/SanadMandate.sol   anchors a run, verifies a PQ signature
web/                            the operator and auditor UI
```

## Tests

```
pytest        25 passed
forge test    10 passed
```

Two of the pytest cases are pointed at real receipts in `evidence/` and asked to
reproduce what the chain recorded, rather than agreeing with the encoder that produced
them. The codec's golden vector ties `memo_id("INV-2026-0001")` to the `memoId` Arc
actually stored, and the byte level vectors are hand written so the wire format cannot
drift silently.

## What this does not do

Stated plainly, because a demo that overclaims is worse than a smaller one that does not.

- **Testnet only.** Arc mainnet is not live for this, and Circle lists Arc as testnet only
  for Gateway and nanopayments.
- **The post quantum path is built but not exercised with a real signature.** The contract
  verifies SLH-DSA through Arc's precompile and the length and failure paths are tested,
  but generating a real FIPS 205 keypair is outside this build, so every run so far reads
  `no post quantum signature offered`. The gas cost, 287,133 measured, is the reason it is
  optional rather than default.
- **The mandate does not recompute its digest on chain.** Passing forty payees again would
  double the calldata for a check anyone can already perform against the events in the same
  transaction, so the digest is anchored on chain and verified off chain. `ledger.py` does
  that verification and reports it per run.
- **No Gateway or CCTP integration in this build.** Both are real options for funding a
  payout treasury and both are described in the architecture, but neither is wired, and
  the code claims only what it does.
- **The CBUAE code table is dated, not authoritative.** It came from bank publications on
  2026-07-31. A production system would take it from the Central Bank directly.

## Licence and disclosure

Apache-2.0. Educational and testnet demo purposes only, not a licensed payment service.

AI assistance (Claude, Anthropic) was used in developing this project. The design, the
review and the verification were done by the author. Every number, address and
transaction hash in this README was produced by running the code against Arc testnet and
is reproducible from the committed evidence.
