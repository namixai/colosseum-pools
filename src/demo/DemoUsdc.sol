// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @title DemoUsdc
/// @notice The testnet demo's own USDC on HyperEVM. Its operator mints any amount, and a faucet the
///         keeper runs mints up to a daily limit. The chain's testnet USDC is not ours to mint, and
///         the demo's traders held 0.30 of it (the CTO, 19 October 2026). A factory deployed with
///         this token prices challenges and pays the platform in it; capital stays on HyperCore, in
///         the chain's own USDC. Never on mainnet: it refuses to be deployed there.
contract DemoUsdc is ERC20 {
    /// HyperEVM mainnet, where the factory takes the chain's USDC.
    uint256 public constant MAINNET = 999;

    address public operator;
    address public faucet;
    /// What the faucet may mint in one UTC day (6 decimals).
    uint256 public faucetDaily;
    mapping(uint256 day => uint256) public faucetMinted;

    event OperatorSet(address indexed operator);
    event FaucetSet(address indexed faucet, uint256 daily);

    error NotOnMainnet();
    error NotOperator();
    error NotMinter();
    error OverDaily(uint256 left);

    constructor(address operator_) ERC20("Demo USDC", "dUSDC") {
        if (block.chainid == MAINNET) revert NotOnMainnet();
        operator = operator_;
        emit OperatorSet(operator_);
    }

    modifier onlyOperator() {
        if (msg.sender != operator) revert NotOperator();
        _;
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function setOperator(address operator_) external onlyOperator {
        operator = operator_;
        emit OperatorSet(operator_);
    }

    function setFaucet(address faucet_, uint256 daily) external onlyOperator {
        faucet = faucet_;
        faucetDaily = daily;
        emit FaucetSet(faucet_, daily);
    }

    /// @notice Mints `amount` to `to`: any amount by the operator, and by the faucet no more than
    ///         `faucetDaily` in a UTC day.
    function mint(address to, uint256 amount) external {
        if (msg.sender != operator) {
            if (msg.sender != faucet) revert NotMinter();
            uint256 day = block.timestamp / 1 days;
            uint256 minted = faucetMinted[day];
            // The limit may have been lowered below what the day has minted already.
            if (minted + amount > faucetDaily) revert OverDaily(minted < faucetDaily ? faucetDaily - minted : 0);
            faucetMinted[day] = minted + amount;
        }
        _mint(to, amount);
    }
}
