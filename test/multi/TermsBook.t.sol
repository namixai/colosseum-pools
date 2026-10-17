// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";

import {TermsBook} from "../../src/multi/TermsBook.sol";
import {CodeStore} from "../../src/lib/CodeStore.sol";

contract CodeStoreUser {
    function write(bytes memory data) external returns (address) {
        return CodeStore.write(data);
    }

    function read(address a) external view returns (bytes memory) {
        return CodeStore.read(a);
    }
}

/// Terms kept once under their hash, read back as written, by anyone, with one call.
contract TermsBookTest is Test {
    TermsBook book = new TermsBook();

    function test_terms_publishedOnce_andReadBackAsWritten() public {
        string memory text = "1. The money is locked for the term.\n2. A term nobody stops runs on for 90 days.";
        vm.expectEmit(true, false, false, false, address(book));
        emit TermsBook.Published(keccak256(bytes(text)), address(0), 0);
        bytes32 hash = book.publish(text);
        assertEq(hash, keccak256(bytes(text)));
        assertEq(book.text(hash), text);
        address at = book.textAt(hash);
        assertTrue(at != address(0));
        // Published again: the same hash, nothing kept twice.
        vm.recordLogs();
        assertEq(book.publish(text), hash);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(book.textAt(hash), at);
        vm.expectRevert(TermsBook.Unknown.selector);
        book.text(keccak256("never published"));
    }

    function test_terms_aLongTextAndEveryByteSurvive() public {
        bytes memory long = new bytes(20_000);
        for (uint256 i = 0; i < long.length; ++i) {
            long[i] = bytes1(uint8(i % 251));
        }
        bytes32 hash = book.publish(string(long));
        assertEq(keccak256(bytes(book.text(hash))), keccak256(long));
        // The kept code starts with STOP, so it cannot be run.
        assertEq(uint8(book.textAt(hash).code[0]), 0);
    }

    /// The longest a contract's code may be is 24,576 bytes, STOP included: a longer text is refused
    /// rather than kept cut short (the audit's Info), and the longest that fits is kept whole.
    function test_codeStore_aTextTooLongToKeepIsRefused() public {
        CodeStoreUser u = new CodeStoreUser();
        vm.expectRevert(CodeStore.TooLong.selector);
        u.write(new bytes(24_576));
        vm.expectRevert(CodeStore.TooLong.selector);
        u.write(new bytes(65_536)); // its length would not fit the creation code's two bytes
        bytes memory longest = new bytes(24_575);
        longest[24_574] = 0x01;
        assertEq(keccak256(u.read(u.write(longest))), keccak256(longest));
    }

    function test_codeStore_roundTrip_andNothingStoredIsRefused() public {
        CodeStoreUser u = new CodeStoreUser();
        bytes memory data = abi.encode(uint256(7), "plan", address(this));
        address a = u.write(data);
        assertEq(u.read(a), data);
        assertEq(u.read(u.write("")), "");
        vm.expectRevert(CodeStore.NotStored.selector);
        u.read(address(0xdead));
    }
}
