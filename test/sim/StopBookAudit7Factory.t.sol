// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {PaperExam} from "../../src/exam/PaperExam.sol";
import {SimPool} from "../../src/sim/SimPool.sol";
import {SimPoolFactory} from "../../src/sim/SimPoolFactory.sol";
import {StopBook} from "../../src/sim/StopBook.sol";

/// Races of `setAsset` and the book's `add`/`remove` against the factory's real `watchable`: the assets the book logs
/// never pass MAX_LISTED, and a refusal happens exactly when the rule says.
contract StopBookAudit7FactoryTest is Test {
    SimPoolFactory f;
    StopBook book;
    address[6] exams;
    address pool = address(0xBEEF);
    uint256 rnd;

    function setUp() public {
        f = new SimPoolFactory(
            IERC20(address(0xBEEF)), address(new PaperExam()), address(new SimPool()), address(this), 1 ether
        );
        book = f.stopBook();
        for (uint256 i = 0; i < 6; ++i) {
            exams[i] = address(new Dummy());
            // poolOf[exam] = pool (mapping slot 7)
            vm.store(address(f), keccak256(abi.encode(exams[i], uint256(7))), bytes32(uint256(uint160(pool))));
        }
    }

    function _next(uint256 m) internal returns (uint256) {
        rnd = uint256(keccak256(abi.encode(rnd)));
        return rnd % m;
    }

    function _unlistedLogged() internal view returns (uint256 n) {
        uint32[] memory w = book.watched();
        for (uint256 i = 0; i < w.length; ++i) {
            if (f.assetCapX100(w[i]) == 0) ++n;
        }
    }

    /// forge-config: default.fuzz.runs = 600
    function testFuzz_a7_listAndLogNeverPassTheCap(uint256 seed) public {
        rnd = seed;
        for (uint256 step = 0; step < 220; ++step) {
            uint256 r = _next(100);
            uint32 a = uint32(1 + _next(30));
            if (r < 30) {
                uint32 cap = _next(3) == 0 ? 0 : 500;
                uint256 listed = f.listedAssets();
                bool was = f.assetCapX100(a) != 0;
                // the rule: listing a new asset: listed + 1 + (logged unlisted, not counting a) <= 20
                uint256 unl = _unlistedLogged();
                if (f.assetCapX100(a) == 0 && _logged(a)) unl -= 1;
                bool expectRevert = !was && cap != 0 && listed + 1 + unl > f.MAX_LISTED();
                if (expectRevert) vm.expectRevert(SimPoolFactory.TooManyAssets.selector);
                f.setAsset(a, cap);
            } else if (r < 75) {
                address e = exams[_next(6)];
                bool tk = _next(2) == 0;
                StopBook.Entry memory en = book.entryOf(e, a, tk);
                bool freshAsset = en.level == 0 && !_logged(a);
                bool ok = !freshAsset || f.watchable(a);
                vm.prank(e);
                if (!ok) vm.expectRevert(abi.encodeWithSelector(StopBook.NoPlaceForAsset.selector, a));
                book.add(a, tk, true, 1000 + uint64(_next(500)), uint64(block.number + 1));
            } else {
                vm.prank(exams[_next(6)]);
                book.remove(a, _next(2) == 0);
            }
            uint32[] memory w = book.watched();
            assertLe(w.length, f.MAX_LISTED(), "logged assets");
            assertLe(f.listedAssets() + _unlistedLogged(), f.MAX_LISTED(), "listed + unlisted logged");
        }
    }

    function _logged(uint32 a) internal view returns (bool) {
        uint32[] memory w = book.watched();
        for (uint256 i = 0; i < w.length; ++i) {
            if (w[i] == a) return true;
        }
        return false;
    }
}

contract Dummy {}
