# Sanad: proof carrying payouts on Arc

**Track 1, cross border payments and remittances.** The Stablecoins Commerce Stack
Challenge, host Arc and Circle.

Sanad gives every USDC payment its own ISO 20022 style instruction on chain: the
purpose of payment code the UAE central bank mandates, the invoice reference, the
counterparty reference, all bound to the exact call by hash. Batch runs settle N
payouts in one transaction, each payee getting their own receipt and each transfer
appearing to come from the payer's own address. The ledger, the regulator view and
every counterparty history rebuild from Arc alone, because they were never stored
anywhere else.

Sanad is Arabic for the deed, in the classical sciences the chain of
transmission that makes a report trustworthy. The payment and its proof travel
together.

## The problem

The Central Bank of the UAE requires a purpose of payment code on every outbound
transfer. HSBC UAE told its own customers: "With effect from 1st September 2018, PoP
codes will become mandatory for all outbound cross border payments initiated from HSBC
UAE accounts." Nordea documents where the code goes: "A 3-letter purpose code should be
used for all payments to UAE, irrespectively of currency. The code must be placed in
the first line of the details of payment / message to the beneficiary field."

So a field a central bank mandates has no structured home in the message. It gets typed
into the first line of a free text box. A plain USDC transfer does not even have the
free text box, which is why every stablecoin rail today keeps the instruction in a
private database, where the audit trail becomes a row somebody can edit.

## What is live right now, on Arc testnet 5042002

| What | Transaction |
|---|---|
| `SanadMandate`, our contract | `0xC73090627ac5ed6fb651807c6A3E64D2FFa34194` |
| A four payee run, self submitted | `0x296796c8689eb4e53972271f2bb8f143c629eed9dd889ae35596d3b6eb8520f4` |
| A run authorized by the payer, submitted by an operator | `0x721e4e3b0cabebb9c834013ad884222356e4bf60609612728ec2caf73a107dcf` |
| The same, with the payer's key inside a Circle developer controlled wallet | `0x98128c1128d7582424ae07ec899730cd22e137a636769ba920b18cb1e40c8e72` |

Every receipt is committed under `evidence/`. Cost measured rather than estimated:
**0.00164 to 0.00179 USDC per payee**, including the mandate anchor and the full
instruction, in a single transaction.

## The Arc primitives this is built on, plus the finding

Arc ships contracts that are not in the developer docs. They live in Circle's node
repository and the contract address book:

- **Memo**, `0x5294E9927c3306DcBaDb03fe70b92e01cCede505`, attaches structured data to a
  call.
- **Multicall3From**, `0x522fAf9A91c41c443c66765030741e4AaCe147D0`, batches calls while
  preserving the original sender.
- **The CallFrom precompile**, which is what makes that preservation possible.
- **The post quantum precompile**, which verifies SLH-DSA signatures.

**The composition is the finding.** Memo and Multicall3From are both documented as EOA
only, which reads as uncomposable. Nested, they compose: the EOA survives both CallFrom
hops. That is what makes batched on chain payment instructions possible at all. It
is the first item in our Circle product feedback because the documentation currently
tells you it cannot be done.

## Circle products: how each one is used

**USDC on Arc**, `0x3600000000000000000000000000000000000000`, is both the settlement
asset and the gas token. Used through `transfer` and, in authorized mode, through the
EIP-3009 `transferWithAuthorization` path that lets a payer authorize without
broadcasting. The EIP-712 domain was verified against the live contract: name `USDC`,
version `2`, chainId `5042002`.

**Circle Wallets, developer controlled.** In authorized mode the payer's key lives in
Circle's custody and this application never holds it. The EIP-712 payload goes to
`POST /v1/w3s/developer/sign/typedData`, Circle signs inside its own custody and returns
65 bytes. The entity secret is re-encrypted for every request. Exercised live:
transaction `0x98128c11…0c8e72` moved USDC out of Circle wallet
`0x5074B92189e295f46597037F3b972786578D05d2` while that wallet's own transaction count
stayed at zero.

**EURC** is deployed on Arc at `0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a` and is the
same FiatToken build, so the same instruction path works for a euro leg. Not exercised
in this build. The code does not pretend otherwise.

## The web app

`uvicorn sanad.api.app:app --app-dir src --port 8099`, then `http://127.0.0.1:8099`.
Three tabs. **Build a run** takes a payee table where each row carries its amount,
invoice reference, ISO 20022 purpose code and CBUAE code, screens it against a
denylist, encodes the instruction, prices the run and shows the exact bytes that will
go on chain. Nothing sends until a simulation has passed. **Audit from the chain**
rebuilds every run from Arc with three log queries and one transaction read per run,
reports whether each mandate digest recomputes, breaks totals down by purpose code,
derives a counterparty history that reads as credit history, then finds any payment by
its invoice reference through an indexed memo topic rather than a scan. **What this
uses** reads the Arc and Circle addresses from the chain at page load rather than
hardcoding them, so the page cannot claim an integration that is not live.

Of the five API routes only settlement needs a key, so the entire audit half runs
against Arc with no credentials.

## The audit, which is the point

Drop the database and rebuild every invoice, purpose code and payee receipt from Arc
alone. `ledger.py` reads the chain, recomputes each mandate digest and reports whether
it matches what was anchored on chain, per run. The committed rebuild in
`evidence/ledger-rebuild.json` ends on three of three runs having their authorization
recomputed and matched. Nothing in that path reads a database, because there is no
database.

## Tests and how to reproduce them

```
pytest        25 passed
forge test    10 passed
```

Both re-run and confirmed green on 2026-07-31. Two of the pytest cases point at real
receipts in `evidence/` and are asked to reproduce what the chain recorded, rather than
agreeing with the encoder that produced them. `forge-std` is vendored at its pinned
version, so `forge test` runs from a clean clone with no extra install step.

## What this does not do

- **Testnet only.** Arc mainnet is not live for this, plus Circle lists Arc as testnet
  only for Gateway and nanopayments.
- **The post quantum path is built but never exercised with a real signature.** The
  contract verifies SLH-DSA through Arc's precompile and the length and failure paths
  are tested, but generating a real FIPS 205 keypair is outside this build, so every run
  reads `no post quantum signature offered`. Measured gas of 287,133 is why it is
  optional rather than default.
- **The mandate does not recompute its digest on chain.** It is anchored on chain and
  verified off chain, because passing forty payees again would double the calldata for a
  check anyone can perform against the events in the same transaction.
- **No Gateway or CCTP integration.** Both are real options for funding a payout
  treasury and both are in the architecture, but neither is wired.
- **The CBUAE code table is dated, not authoritative.** It came from bank publications
  on 2026-07-31. Production would take it from the central bank directly.

## Links

- Repository: https://github.com/zkasuran/sanad
- Demo video: PASTE YOUTUBE LINK
- Contract on Arc testnet: https://testnet.arcscan.app/address/0xC73090627ac5ed6fb651807c6A3E64D2FFa34194
- Circle product feedback: `docs/CIRCLE-PRODUCT-FEEDBACK.md`
- Architecture diagram: `docs/ARCHITECTURE.svg`

## Disclosure

Apache-2.0. Educational and testnet demo purposes only, not a licensed payment service.

AI assistance (Claude, Anthropic) was used in developing this project. The design, the
review and the verification were done by the author. Every number, address and
transaction hash above was produced by running the code against Arc testnet.

