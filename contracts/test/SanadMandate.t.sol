// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.29;

import {Test} from "forge-std/Test.sol";
import {SanadMandate} from "../src/SanadMandate.sol";

/// @dev The PQ precompile only exists on Arc, so a local run mocks it. The mock is the
///      honest kind: it asserts the exact calldata the contract should be sending, so a
///      wrong message or a swapped argument fails here rather than on chain.
contract SanadMandateTest is Test {
    address internal constant PQ = 0x1800000000000000000000000000000000000004;

    SanadMandate internal mandate;
    address internal payer = address(0xA11CE);
    bytes32 internal runId = keccak256("RUN-2026-07-31-A");
    bytes32 internal digest = keccak256("digest");

    function setUp() public {
        mandate = new SanadMandate();
    }

    function _vk() internal pure returns (bytes memory out) {
        out = new bytes(32);
    }

    function _sig() internal pure returns (bytes memory out) {
        out = new bytes(7856);
    }

    function _mockPq(bool result) internal {
        vm.mockCall(
            PQ,
            abi.encodeWithSelector(
                bytes4(keccak256("verifySlhDsaSha2128s(bytes,bytes,bytes)")),
                _vk(),
                abi.encodePacked(digest),
                _sig()
            ),
            abi.encode(result)
        );
    }

    function test_open_records_the_payer_as_msg_sender() public {
        vm.prank(payer);
        bool pqVerified = mandate.open(runId, digest, 10_000, 3, address(0), "", "");

        assertFalse(pqVerified, "no PQ signature was offered");
        assertTrue(mandate.isOpen(runId));

        SanadMandate.Mandate memory stored = mandate.mandate(runId);
        assertEq(stored.payer, payer, "CallFrom preserves the EOA, so this is the payer");
        assertEq(stored.submitter, payer, "self submitted, so payer and submitter match");
        assertEq(stored.digest, digest);
        assertEq(stored.totalMinor, 10_000);
        assertEq(stored.payeeCount, 3);
        assertFalse(stored.pqVerified);
        assertEq(stored.openedAt, uint64(block.timestamp));
    }

    function test_open_emits_the_run() public {
        vm.expectEmit(true, true, true, true, address(mandate));
        emit SanadMandate.MandateOpened(runId, payer, payer, digest, 10_000, 3, false);
        vm.prank(payer);
        mandate.open(runId, digest, 10_000, 3, address(0), "", "");
    }

    function test_a_run_cannot_be_replayed() public {
        vm.prank(payer);
        mandate.open(runId, digest, 10_000, 3, address(0), "", "");

        vm.expectRevert(abi.encodeWithSelector(SanadMandate.MandateExists.selector, runId));
        vm.prank(payer);
        mandate.open(runId, digest, 10_000, 3, address(0), "", "");
    }

    function test_an_empty_run_is_refused() public {
        vm.expectRevert(SanadMandate.EmptyRun.selector);
        mandate.open(runId, digest, 10_000, 0, address(0), "", "");

        vm.expectRevert(SanadMandate.EmptyRun.selector);
        mandate.open(runId, digest, 0, 3, address(0), "", "");
    }

    function test_a_zero_digest_is_refused() public {
        vm.expectRevert(SanadMandate.ZeroDigest.selector);
        mandate.open(runId, bytes32(0), 10_000, 3, address(0), "", "");
    }

    function test_a_valid_post_quantum_signature_is_recorded() public {
        _mockPq(true);
        vm.prank(payer);
        bool pqVerified = mandate.open(runId, digest, 10_000, 3, address(0), _vk(), _sig());

        assertTrue(pqVerified);
        assertTrue(mandate.mandate(runId).pqVerified);
    }

    function test_an_invalid_post_quantum_signature_reverts_the_whole_run() public {
        _mockPq(false);
        vm.expectRevert(SanadMandate.PqSignatureInvalid.selector);
        vm.prank(payer);
        mandate.open(runId, digest, 10_000, 3, address(0), _vk(), _sig());

        assertFalse(mandate.isOpen(runId), "a rejected signature leaves no mandate behind");
    }

    function test_wrong_post_quantum_lengths_are_refused_before_the_precompile() public {
        bytes memory shortVk = new bytes(31);
        vm.expectRevert(abi.encodeWithSelector(SanadMandate.PqLengthWrong.selector, 31, 7856));
        mandate.open(runId, digest, 10_000, 3, address(0), shortVk, _sig());

        bytes memory shortSig = new bytes(10);
        vm.expectRevert(abi.encodeWithSelector(SanadMandate.PqLengthWrong.selector, 32, 10));
        mandate.open(runId, digest, 10_000, 3, address(0), _vk(), shortSig);
    }

    function test_a_signature_without_a_key_is_refused() public {
        vm.expectRevert(abi.encodeWithSelector(SanadMandate.PqLengthWrong.selector, 0, 7856));
        mandate.open(runId, digest, 10_000, 3, address(0), "", _sig());
    }

    function test_an_unopened_run_reads_as_empty() public view {
        assertFalse(mandate.isOpen(keccak256("nope")));
        assertEq(mandate.mandate(keccak256("nope")).payer, address(0));
    }
}
