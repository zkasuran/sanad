// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.29;

/// @title IPQ
/// @notice Arc's post quantum signature precompile, as the node declares it in
///         `contracts/src/pq/IPQ.sol`. SLH-DSA-SHA2-128s, FIPS 205. The verifying key
///         is exactly 32 bytes and the signature exactly 7,856. Verification costs
///         230,000 gas plus 6 per word of message, which measured at 287,133 for a
///         short message on testnet.
interface IPQ {
    function verifySlhDsaSha2128s(bytes memory vk, bytes memory message, bytes memory sig)
        external
        view
        returns (bool);
}

/// @title SanadMandate
/// @notice Anchors the authorization for a payout run, in the same transaction that
///         pays it.
///
/// @dev This contract is called through Arc's Memo contract, which routes the call
///      through the CallFrom precompile. So `msg.sender` here is the payer's own EOA
///      and not the Memo contract, which means the transaction itself is the classical
///      authorization and no ECDSA signature has to be passed in or replayed.
///
///      On top of that, a run can carry a post quantum signature over its digest. Arc
///      exposes SLH-DSA verification as a precompile, so the check happens on chain
///      rather than being asserted in a slide. The pairing is deliberate: the PQ
///      interface itself warns against relying on post quantum signatures alone while
///      they are new, so the classical path stays load bearing and PQ is additive.
///
///      What this contract does NOT do is recompute the digest from the payee list.
///      Passing forty payees again would double the calldata for a check that anyone
///      can already perform, because the Memo events for the run sit in this same
///      transaction. So the digest is anchored here and verified off chain against the
///      events, and `sanad.ledger` does exactly that during a rebuild.
contract SanadMandate {
    IPQ internal constant PQ = IPQ(0x1800000000000000000000000000000000000004);

    uint256 internal constant PQ_VK_LEN = 32;
    uint256 internal constant PQ_SIG_LEN = 7856;

    struct Mandate {
        address payer;
        address submitter;
        uint64 openedAt;
        uint32 payeeCount;
        bool pqVerified;
        uint256 totalMinor;
        bytes32 digest;
    }

    mapping(bytes32 => Mandate) private _mandates;

    /// @notice Emitted once per run. `pqVerified` records whether a post quantum
    ///         signature was supplied and accepted, so a reader can tell the difference
    ///         between "not offered" and "offered and valid".
    event MandateOpened(
        bytes32 indexed runId,
        address indexed payer,
        address indexed submitter,
        bytes32 digest,
        uint256 totalMinor,
        uint32 payeeCount,
        bool pqVerified
    );

    error MandateExists(bytes32 runId);
    error NoPayer();
    error EmptyRun();
    error ZeroDigest();
    error PqLengthWrong(uint256 vkLen, uint256 sigLen);
    error PqSignatureInvalid();

    /// @notice Open a run. Reverts if the run id has been used, so a batch cannot be
    ///         replayed under the same authorization.
    /// @param runId       keccak256 of the caller's run identifier.
    /// @param digest      The mandate digest, covering payer, token, chain, and every
    ///                    payee with its amount and memo id, in order.
    /// @param totalMinor  Sum of the run, in token minor units.
    /// @param payeeCount  How many payouts the run authorizes.
    /// @param payer       Whose money this run moves. Pass the zero address to mean the
    ///                    caller, which is the self submitted case. In the authorized case
    ///                    the payer signed EIP-3009 authorizations and an operator submits,
    ///                    so the two addresses differ and both belong in the record.
    /// @param pqVk        SLH-DSA verifying key, or empty to skip the PQ check.
    /// @param pqSig       SLH-DSA signature over `digest`, or empty to skip.
    /// @return pqVerified True when a post quantum signature was supplied and verified.
    function open(
        bytes32 runId,
        bytes32 digest,
        uint256 totalMinor,
        uint32 payeeCount,
        address payer,
        bytes calldata pqVk,
        bytes calldata pqSig
    ) external returns (bool pqVerified) {
        if (_mandates[runId].openedAt != 0) revert MandateExists(runId);
        address authorizer = payer == address(0) ? msg.sender : payer;
        if (authorizer == address(0)) revert NoPayer();
        if (payeeCount == 0 || totalMinor == 0) revert EmptyRun();
        if (digest == bytes32(0)) revert ZeroDigest();

        if (pqVk.length != 0 || pqSig.length != 0) {
            if (pqVk.length != PQ_VK_LEN || pqSig.length != PQ_SIG_LEN) {
                revert PqLengthWrong(pqVk.length, pqSig.length);
            }
            if (!PQ.verifySlhDsaSha2128s(pqVk, abi.encodePacked(digest), pqSig)) {
                revert PqSignatureInvalid();
            }
            pqVerified = true;
        }

        _mandates[runId] = Mandate({
            payer: authorizer,
            submitter: msg.sender,
            openedAt: uint64(block.timestamp),
            payeeCount: payeeCount,
            pqVerified: pqVerified,
            totalMinor: totalMinor,
            digest: digest
        });

        emit MandateOpened(runId, authorizer, msg.sender, digest, totalMinor, payeeCount, pqVerified);
    }

    function mandate(bytes32 runId) external view returns (Mandate memory) {
        return _mandates[runId];
    }

    function isOpen(bytes32 runId) external view returns (bool) {
        return _mandates[runId].openedAt != 0;
    }
}
