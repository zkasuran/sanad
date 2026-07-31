# Circle Product Feedback

Written from actually building Sanad on Arc testnet over 2026-07-30 and 2026-07-31, not
from reading marketing pages. Every claim here is something we hit. Where a number
appears it was measured.

## Why we chose these products

**USDC on Arc** was not really a choice, it was the point. The product is a cross border
payout rail for a UAE SME, and the thing that makes it possible is that on Arc the
settlement asset and the gas token are the same dollar denominated balance. A payments
person can be told "a payout costs a sixth of a cent" and that sentence stays true
tomorrow. On a chain with a volatile gas token it does not.

**Arc's own primitives** decided the architecture. The problem we set out to solve is that
a mandatory regulatory field, the CBUAE purpose of payment code, has no structured home in
a payment message and no home at all in a stablecoin transfer. Arc's `Memo` contract is
exactly the missing field, and `Multicall3From` makes it affordable across a whole payout
run. Nothing else in the stack we looked at has this.

**Circle Wallets, developer controlled** was chosen because the honest version of a
treasury product cannot hold the treasury's key. Being able to say "this application never
sees the payer's key, Circle signs and an operator relays" is the difference between a demo
and something a finance team would pilot.

## What worked well

- **EIP-3009 on Arc USDC is clean and it worked first try.** The EIP-712 domain we
  computed (`name "USDC"`, `version "2"`, chainId `5042002`, verifyingContract
  `0x3600...0000`) matched the live `DOMAIN_SEPARATOR()` with no guessing, and
  `transferWithAuthorization` behaved exactly as specified. This is the single most useful
  thing in the token for a payments builder, because it is what lets a payer authorize
  without broadcasting.
- **`aggregate3` returning a per call `Result` made a real preflight possible.** We
  simulate the whole run with `eth_call` and show an operator, line by line, which payee
  would fail before anything is signed. That UX would be much worse with an all or nothing
  batch.
- **Deterministic sub second finality changes what you build.** Our settle then audit loop
  is a single script that sends a transaction and immediately rebuilds the ledger from
  logs. On a probabilistic chain that flow needs confirmation handling and a spinner. Here
  it just reads back.
- **Fees priced in USDC removed a whole class of code.** No gas token balance to monitor,
  no swap, no separate faucet. One balance funds both the payouts and the gas.
- **The Denylist being a contract is genuinely better than a vendor API.** Screening a run
  is `isDenylisted(address)` per payee, on the same chain, in the same breath as the
  simulation. No key, no subscription, no rate limit, no vendor to trust.

## What could be improved

Ordered by how much time each one cost us.

### 1. Arc's most differentiating primitives are not in the developer docs

`Memo`, the `CallFrom` precompile, `Multicall3From`, the `PQ` precompile and the
`Denylist` are all live on testnet. We did not find them in `docs.arc.network`. We found
them by cloning `circlefin/arc-node` and reading `contracts/scripts/Addresses.sol`,
`contracts/src/Precompiles.sol` and the `Zero7` hardfork line in the changelog.

This is the single highest leverage fix available to Arc. On chain payment metadata and
sender preserving batch are the features that distinguish Arc from any other EVM chain
with a stablecoin on it, and a builder reading the docs would never learn they exist. A
page listing the five addresses with one paragraph each, plus one sample app that attaches
a reference to a transfer, would change what people build.

### 2. Memo and Multicall3From read as uncomposable, and they are not

Both contracts carry a doc comment saying they are EOA only, because `CallFrom` requires
the `sender` argument to equal either the precompile caller or `tx.origin`. Read
separately, the obvious conclusion is that you cannot put a Memo call inside a
Multicall3From batch.

You can. Nest them and the EOA survives both hops, because Multicall3From passes
`sender = tx.origin`, so Memo sees the EOA as its own `msg.sender` and its own `CallFrom`
call then passes the same test. We proved it on chain: three payees, three separate memos,
one transaction, 140,248 gas.

That composition is the most useful thing about either contract and nothing says it. One
sentence in `IMemo` and `IMulticall3From` would do it.

### 3. `CallFrom` reverts with a bare string, and the allowlist is invisible

A direct call from an EOA reverts with `unauthorized caller`, a plain string. The
allowlist itself is hardcoded in `crates/execution-config/src/call_from.rs` to the Memo
and Multicall3From addresses, which is a sensible design and completely undiscoverable
from the outside. A custom error carrying the caller, plus a doc line naming the two
allowlisted contracts, would save the next person the hour it cost us.

### 4. USDC being both native and ERC-20, at different decimals, is a real footgun

The same balance reads as 18 decimals through `eth_getBalance` and 6 decimals through
`balanceOf`. The factor of 1e12 between the two views is easy to get wrong in exactly the
place it hurts, fee arithmetic, and a receipt that is out by 1e12 looks plausible. We ended
up with a single `fee_minor` property in one module to stop the conversion appearing
anywhere else. Worth a prominent callout in the Arc docs rather than a footnote, and worth
a helper in any SDK that touches Arc.

### 5. The public RPC rate limits, and the limit is not published

A rebuild that walks logs across a range gets `-32011 request limit reached` with no
documented threshold and no `Retry-After`. We worked around it with response caching and
by dropping web3's per call validation middleware, which is fine, but we were guessing.
Publishing the limit, or returning a header, turns guesswork into a retry policy.

### 6. `ProtocolConfig` on chain disagrees with the node repo's own testnet config

`assets/testnet/config.json` says `kRate: 25`, `minBaseFee: 1`, `maxBaseFee: 1000`. The
live contract returns `kRate 200`, `minBaseFee 20e9`, `maxBaseFee 20e12`. We read the
contract, which is the right answer, but a builder who trusts the file will quote wrong
numbers in a UI. Either the file is stale or the units differ, and either way it should
say so.

### 7. The faucet cannot be scripted

`faucet.circle.com` is browser only with a reCAPTCHA and 20 USDC per asset per network
every two hours. That is reasonable for a human and a wall for CI. An API keyed faucet
with a low quota, or a documented way to request a one off top up for a hackathon team,
would remove a recurring source of manual work.

### 8. Circle developer controlled wallets: the entity secret ciphertext is easy to get wrong

The ciphertext has to be regenerated for every request. That is a good security property
and it is unusual enough that the natural implementation, computing it once and reusing
it, fails in a way that does not obviously point at the cause. A sentence in the quickstart
saying "never cache this" plus a distinct error when a stale ciphertext arrives would help.

### 9. Circle's Python SDKs are a major version behind the JavaScript ones

`circle-developer-controlled-wallets` is 9.6.0 on PyPI while
`@circle-fin/developer-controlled-wallets` is 10.8.0 on npm. Our backend is Python because
the chain work is, and there is no Python equivalent of `@circle-fin/x402-batching` at all.
For a payments company whose customers include a lot of Python backends, parity would be
worth it.

### 10. Arc is not a supported chain for wallet transaction sending

A developer controlled wallet can sign typed data for Arc but cannot broadcast on it, so
the working pattern is sign off chain then relay. That pattern is actually the right one
for our use case, so this is not a complaint about the outcome, but it took an experiment
to discover. Saying plainly which Arc operations a Circle wallet supports today, and which
it does not, would set expectations correctly.

## Recommendations, shortest version

1. Document Memo, CallFrom, Multicall3From, PQ and the Denylist, with addresses, and say
   that Memo nests inside Multicall3From.
2. Put the USDC dual decimal representation at the top of the Arc docs, not in a footnote.
3. Publish the RPC rate limit, and return a retry hint.
4. Ship a Python `x402-batching` and bring the Python wallet SDKs to parity.
5. Add one sample app that attaches a payment reference to a transfer. It is four lines of
   calldata and it would show off the thing Arc has that nobody else does.
