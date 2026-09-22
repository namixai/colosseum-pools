// What the new-pool form starts with: the demo pool, sized to the mock USDC we hold. The faucet
// pays at most 1,000 per wallet and ours has had its 1,000, so the whole demo lives on 890 (22 Sep
// 2026, measured). 700 funded, 70 per challenge and a price of 7; the platform fee of 0.7 is set on
// the factory by ops/deploy_testnet.py. Such a pool needs 771 on HyperCore spot before it can sell
// a challenge (both capitals and 1 USDC for the challenge's new account), and a buyer needs 7.70 on
// HyperEVM. That 1 USDC does not scale with the pool: here it is 14% of what a challenge sells for.
//
// Percentages and days as the form takes them. The challenge capital is a tenth of the funded
// capital because that is the pool the model prices: at another ratio the form shows no floor.
export const DEMO_POOL = {
  daily: 3,
  dd: 6,
  lev: 5,
  price: 7,
  capital: 70,
  target: 8,
  days: 7,
  challengeShare: 0,
  fundedShare: 80,
  funded: 700,
};
