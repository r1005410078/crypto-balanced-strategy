---
name: crypto-balanced-strategy
description: Build and run a relatively stable crypto rotation strategy using dual-momentum + trend filter + regime switch + volatility-targeted risk control. Use when user asks for balanced crypto strategy design, repeated backtesting, walk-forward optimization, and actionable allocation signals.
---

# Crypto Balanced Strategy

Use this skill for: 策略设计、反复回测、参数优化、当前仓位信号。

## Architecture

- `scripts/engine.py`: shared data + backtest engine (single source of truth)
- `scripts/run_strategy.py`: run one profile backtest and emit current allocation
- `scripts/optimize.py`: walk-forward optimization and profile update
- `scripts/multi_strategy_advisor.py`: multi-strategy ensemble allocator (local optimum)
- `scripts/backtest_governance.py`: robustness governance (stress/sensitivity/walk-forward + decision)
- `scripts/profile_switcher.py`: state-machine profile switcher (stable/short-balanced/shield + confirmation)
- `profiles.json`: parameter profiles (`stable`, `stable_short_balanced`, `stable_shield`, ...)
- `cache/`: local kline cache (auto-managed)
- `results/`: optimization result snapshots
- `tests/test_engine.py`: lightweight regression tests
- `tests/test_optimize.py`: optimization scoring regression tests
- `tests/test_multi_strategy.py`: ensemble scoring/allocation regression tests
- `tests/test_governance.py`: governance decision regression tests
- `tests/test_profile_switcher.py`: profile-switch rule and confirmation regression tests

## New Session Bootstrap (Anti-Amnesia)

When a new chat/window starts, do this **before** giving strategy advice:

1. Read this file (`SKILL.md`) and confirm current workflow.
2. Read `profiles.json` to load the latest active parameters.
3. Read the latest files under `results/` (both `optimize_*.json` and `summary_*.json`) to recover recent optimization context.
4. Run a fresh switch + signal check:
```bash
python3 scripts/profile_switcher.py --capital-cny 10000 --confirmations 2
```
5. Only then provide recommendations, and always include:
   - `active_profile` used
   - key `params_used`
   - latest `latest_alloc`

If `results/` is empty, run quick optimization first:
```bash
python3 scripts/optimize.py --quick-grid --jobs 2 --fold-days 120 --fold-count 2 --min-valid-folds 2
```

## Workflow

1. Walk-forward optimize (OOS-first ranking, robust score):
```bash
python3 scripts/optimize.py --jobs 4 --fold-days 180 --fold-count 3 --min-valid-folds 2
```

1.1 Quick optimization (fast iteration, small grid):
```bash
python3 scripts/optimize.py --quick-grid --jobs 2 --fold-days 120 --fold-count 2 --min-valid-folds 2
```

2. Run default stable profile:
```bash
python3 scripts/run_strategy.py --profile stable --capital-cny 10000 --window-days 365
```

3. Run balanced/aggressive profile:
```bash
python3 scripts/run_strategy.py --profile balanced --capital-cny 10000 --window-days 365
```

3.1 Multi-strategy local-optimal merge recommendation:
```bash
python3 scripts/multi_strategy_advisor.py \
  --profiles stable,balanced,aggressive \
  --include-latest-opt \
  --capital-cny 10000 \
  --windows 120,365,730 \
  --signal-window 365
```

3.2 Save merged recommendation as reusable ensemble profile:
```bash
python3 scripts/multi_strategy_advisor.py \
  --profiles stable,balanced,aggressive \
  --include-latest-opt \
  --capital-cny 10000 \
  --windows 120,365,730 \
  --signal-window 365 \
  --write-merged-profile ensemble-stable
```

3.3 Reuse a saved ensemble profile in new sessions:
```bash
python3 scripts/multi_strategy_advisor.py \
  --use-merged-profile ensemble-stable \
  --capital-cny 10000
```

3.4 Run governance checks before deployment (Backtest Expert style):
```bash
python3 scripts/backtest_governance.py \
  --profile stable \
  --capital-cny 10000 \
  --window-days 365 \
  --hypothesis "Dual momentum + trend + risk-off can deliver better risk-adjusted return than BTC buy-and-hold."
```

Interpret governance output:
- `decision`: `DEPLOY_CANDIDATE | REFINE | ABANDON`
- `robustness_checks`: friction/sensitivity/window/walk-forward pass rates
- `confidence`: 0~1 confidence score under pessimistic assumptions

3.5 Run profile switch state machine (anti-whipsaw):
```bash
python3 scripts/profile_switcher.py \
  --capital-cny 10000 \
  --confirmations 2 \
  --check-window 120 \
  --signal-window 365
```

Switch output includes:
- `target_profile`: rule-triggered target
- `active_profile`: final active profile after confirmation logic
- `state_before/state_after`: pending target and pending count
- `active_signal.latest_alloc`: current execution allocation

4. Save best optimized params into a profile:
```bash
python3 scripts/optimize.py --jobs 4 --write-profile stable
```

5. Override any profile params explicitly:
```bash
python3 scripts/run_strategy.py \
  --profile stable \
  --window-days 365 \
  --lb-fast 20 --lb-slow 60 --sma-filter 120 \
  --k 1 --atr-mult 2.8 \
  --max-w-core 0.55 --max-w-alt 0.35 \
  --target-vol 0.28 --rebalance-every 7 \
  --risk-off-exposure 0.4
```

6. Run tests:
```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Why This Combines Two Skills

- From `backtesting-frameworks`:
  - unified engine architecture (`engine.py`) as single source of truth
  - walk-forward driven optimization and reusable profiles
  - explicit transaction cost and slippage modeling
- From `backtest-expert`:
  - "try to break the strategy" governance gate before deployment
  - friction stress (1.0x/1.5x/2.0x costs), parameter sensitivity plateaus
  - multi-window robustness + out-of-sample fold checks
  - final mechanical decision (`DEPLOY_CANDIDATE/REFINE/ABANDON`)

## Strategy Rules (current stable profile)

- Universe: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, LINKUSDT
- Signal:
  - Dual momentum score = 0.6 * fast window + 0.4 * slow window
  - Trend filter: close > SMA(filter)
  - Keep top `k` assets
- Sizing:
  - inverse-vol weighting (vol_lb)
  - tier caps: core(BTC/ETH) / alt cap
  - target-vol scaling
  - residual in USDT
- Risk:
  - ATR trailing stop (`atr_period` * `atr_mult`)
  - regime switch (preferred: BTC): below regime SMA => cap gross exposure
- Execution:
  - rebalance every N days
  - include fee + slippage in backtest

## Output Interpretation

- `return_pct`, `cagr_pct`:收益
- `max_drawdown_pct`:最大回撤（越接近0越稳）
- `sharpe`:风险收益比
- `avg_daily_turnover`:换手（越低越省成本）
- `latest_alloc`:当前建议仓位
- `params_used`:实际参数

If all assets fail filters, strategy should stay mostly/fully USDT.

## Profile Playbook (Production)

- `stable`: default production profile, best long/medium balance.
- `stable_short_balanced`: use when you want better short-window stability with moderate return sacrifice.
- `stable_shield`: use in high-uncertainty/risk-off phases; strongest drawdown and turnover control, lowest return target.

Suggested switching rule:
- If 120-day backtest return is below `-3%`, switch from `stable` to `stable_short_balanced`.
- If 120-day backtest return is below `-1.5%` and macro/news risk is rising, switch to `stable_shield`.
- Use `confirmations=2` to avoid one-day noise switches.
