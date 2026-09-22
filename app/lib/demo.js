// What the new-pool form starts with: the demo pool (CTO, 21 Sep 2026), sized to what the testnet
// faucet can pay for. 2,000 funded, 200 per challenge and a price of 20; the platform fee of 2 is
// set on the factory by ops/deploy_testnet.py. Such a pool needs 2,201 on HyperCore spot before it
// can sell a challenge (both capitals and 1 USDC for the challenge's new account), and a buyer
// needs 22 on HyperEVM.
//
// Percentages and days as the form takes them. The challenge capital is a tenth of the funded
// capital because that is the pool the model prices: at another ratio the form shows no floor.
export const DEMO_POOL = {
  daily: 3,
  dd: 6,
  lev: 5,
  price: 20,
  capital: 200,
  target: 8,
  days: 7,
  challengeShare: 0,
  fundedShare: 80,
  funded: 2000,
};
