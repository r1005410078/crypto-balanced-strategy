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
- `scripts/tune_risk_layer.py`: constrained risk-parameter optimizer for switch layer profiles
- `scripts/daily_execution_report.py`: one-command daily report (profile + action checklist + guardrails)
- `scripts/okx_auto_executor.py`: OKX spot execution bridge (reads latest signal, builds rebalance plan, dry-run/live execution with guardrails)
- `scripts/auto_state.py`: unattended cycle state/idempotency/lock helpers
- `scripts/risk_guard.py`: unattended live-trading risk gates and circuit-breakers
- `scripts/notifier.py`: webhook notification helper for unattended runs
- `scripts/auto_cycle.py`: one-shot unattended cycle (switch -> transfer -> plan -> risk gate -> execute -> persist -> notify)
- `scripts/auto_daemon.py`: scheduler/daemon wrapper for unattended cycles
- `scripts/auto_tier_cycle.py`: adaptive tier wrapper (conservative/balanced/aggressive auto-switch + auto_cycle execution)
- `scripts/health_check_dryrun.py`: daily connectivity health check (1 USDT dry-run plan, no live order)
- `scripts/trade_decision_scorecard.py`: trade-decision scorecard (fills + realized PnL + cost/discipline scoring + strategy-context recommendations)
- `scripts/preflight_check.py`: portability self-check (python/version/files/env/OKX read permission)
- `profiles.json`: parameter profiles (`stable`, `stable_short_balanced`, `stable_shield`, ...)
- `dependencies.json`: machine-readable dependency declaration for cross-machine install
- `requirements.txt`: pip dependency placeholder (currently stdlib-only runtime)
- `portfolio_snapshot.json`: latest real holdings snapshot (for holdings-aware advice)
- `cache/`: local kline cache (auto-managed)
- `results/`: optimization result snapshots
- `tests/test_engine.py`: lightweight regression tests
- `tests/test_optimize.py`: optimization scoring regression tests
- `tests/test_multi_strategy.py`: ensemble scoring/allocation regression tests
- `tests/test_governance.py`: governance decision regression tests
- `tests/test_profile_switcher.py`: profile-switch rule and confirmation regression tests
- `tests/test_auto_state.py`: unattended state/idempotency regression tests
- `tests/test_risk_guard.py`: unattended risk-gate regression tests
- `tests/test_auto_cycle.py`: unattended cycle helper/status regression tests
- `tests/test_auto_tier_cycle.py`: adaptive tier decision regression tests
- `tests/test_health_check_dryrun.py`: health check summary/normalization regression tests

## Dependencies (Portable Install)

- `requires_skills`:
  - `backtesting-frameworks` (optional, conceptual reference only)
  - `backtest-expert` (optional, conceptual reference only)
- `python`: `>=3.10`
- `pip`: none required currently (stdlib-only)
- `env` (required for OKX features):
  - `OKX_API_KEY`
  - `OKX_API_SECRET`
  - `OKX_API_PASSPHRASE`
- `OKX API permissions`:
  - required: `Read`
  - required for live orders: `Trade`
  - forbidden: `Withdraw`

Install checklist on another machine:
```bash
cd /path/to/crypto-balanced-strategy
python3 -m pip install -r requirements.txt
python3 scripts/preflight_check.py --format text
python3 scripts/preflight_check.py --check-okx --format text
```

## Current Strategy (Single Source of Truth)

Current production strategy definition:
- Strategy family: dual-momentum + trend filter + regime switch + volatility-targeted risk control
- Profile universe: `stable`, `stable_short_balanced`, `stable_shield`
- Execution policy: run switch signal first, then execute `dry-run`, and only place `--live` orders after dry-run validation

Authoritative files (in priority order):
1. `profiles.json`:
   - defines all parameter sets (what each profile means)
2. `results/profile_switch_state.json`:
   - records current active profile state machine (`active_profile`, pending state)
3. latest `results/switch_*.json`:
   - authoritative latest signal (`active_profile`, `params_used`, `latest_alloc`, guardrails)
4. latest `results/okx_exec_*.json`:
   - authoritative execution output (planned/submitted/failed orders)
5. latest `results/decision_scorecard_*.json`:
   - authoritative decision-quality scoring (PnL/cost/discipline)

Quick command to print current strategy snapshot:
```bash
latest=$(ls -t results/switch_*.json | head -n 1) && \
jq '{active_profile,target_profile,latest_alloc:.active_signal.latest_alloc,params_used:.active_signal.params_used}' "$latest"
```

Documentation sync rule:
- If docs and runtime output differ, runtime files above are the source of truth.
- Update this section when profile set, switching logic, or execution policy changes.

## New Session Bootstrap (Anti-Amnesia)

When a new chat/window starts, do this **before** giving strategy advice:

1. Read this file (`SKILL.md`) and confirm current workflow.
2. Read `profiles.json` to load the latest active parameters.
3. Read `portfolio_snapshot.json` (if present) as current real holdings baseline.
4. Read the latest files under `results/` (both `optimize_*.json` and `summary_*.json`) to recover recent optimization context.
5. Run a fresh switch + signal check:
```bash
python3 scripts/profile_switcher.py --capital-cny 10000 --confirmations 2
```
6. Only then provide recommendations, and always include:
   - `active_profile` used
   - key `params_used`
   - latest `latest_alloc`
   - holdings diff: `portfolio_snapshot.json` vs `latest_alloc` (what to keep/add/reduce)

If `results/` is empty, run quick optimization first:
```bash
python3 scripts/optimize.py --quick-grid --jobs 2 --fold-days 120 --fold-count 2 --min-valid-folds 2
```

## Workflow

0. Run portability preflight before first use in a new environment:
```bash
python3 scripts/preflight_check.py --format text
```

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
- `execution_checklist`: actionable step list (hold/deploy, capital plan, guardrails)

3.6 Tune risk layer params under constraints (target_vol/risk_off/rebalance/regime_sma):
```bash
python3 scripts/tune_risk_layer.py \
  --profiles stable,stable_short_balanced,stable_shield \
  --windows 120,180,365,730 \
  --write-profiles
```

Tune output includes:
- per-profile `feasible_count` and top candidates
- `role_penalty` to avoid profile-role collapse
- `updated_profiles` and updated `profiles.json`

3.7 Generate one-command daily execution report:
```bash
python3 scripts/daily_execution_report.py --capital-cny 10000 --format text
```
Holdings data source behavior (default):
- `--holdings-source auto`: try OKX live balances first, fallback to `portfolio_snapshot.json` if API/env is unavailable
- live holdings include funding account by default (`--holdings-include-funding`, disable with `--no-holdings-include-funding`)
- when live holdings are used, `portfolio_snapshot.json` is auto-refreshed by default (`--sync-holdings-snapshot`, disable with `--no-sync-holdings-snapshot`)

3.8 Execute strategy allocation on OKX (safe default: dry-run):
```bash
# Dry-run (plan only, no real order)
python3 scripts/okx_auto_executor.py --allow-sell --allow-buy

# Live (real order): only after dry-run result is verified
python3 scripts/okx_auto_executor.py --allow-sell --allow-buy --live
```

Required env vars:
```bash
export OKX_API_KEY=...
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
```

3.9 Generate trade-decision scorecard (JSON + Markdown):
```bash
python3 scripts/trade_decision_scorecard.py --format both
```

3.10 Unattended one-shot cycle (recommended before daemon):
```bash
# Dry-run unattended cycle (includes switch, plan, risk gates, idempotency state)
python3 scripts/auto_cycle.py

# Live unattended cycle (with optional auto transfer from funding -> trading)
python3 scripts/auto_cycle.py --live --auto-transfer-usdt
```

3.11 Run unattended daemon scheduler:
```bash
# Daily run at local 08:05
python3 scripts/auto_daemon.py --run-at 08:05 --live --auto-transfer-usdt

# Or fixed interval
python3 scripts/auto_daemon.py --interval-minutes 60 --live --auto-transfer-usdt
```

3.12 Launchd deployment (macOS, one run daily at 08:05):
```bash
# 1) copy and edit env keys inside plist
cp scripts/com.crypto-balanced-strategy.auto.balanced.plist ~/Library/LaunchAgents/com.crypto-balanced-strategy.auto.plist

# 2) load (or reload)
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.crypto-balanced-strategy.auto.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.crypto-balanced-strategy.auto.plist
launchctl enable gui/$(id -u)/com.crypto-balanced-strategy.auto
launchctl kickstart -k gui/$(id -u)/com.crypto-balanced-strategy.auto

# 3) check
launchctl print gui/$(id -u)/com.crypto-balanced-strategy.auto
tail -n 50 /tmp/crypto-balanced-strategy-auto.out.log
tail -n 50 /tmp/crypto-balanced-strategy-auto.err.log
```

One-command install/reload (read API keys from current shell env):
```bash
export OKX_API_KEY=...
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
bash scripts/install_launchd_agent.sh balanced
# or
bash scripts/install_launchd_agent.sh conservative
bash scripts/install_launchd_agent.sh aggressive
bash scripts/install_launchd_agent.sh adaptive
```

3.13 Adaptive tier auto-switch run (recommended unattended mode):
```bash
# Auto promote conservative -> balanced after 2 deploy days with normal risk
# Optional aggressive promotion after 5 stable deploy days
python3 scripts/auto_tier_cycle.py --live --promote-days 2 --allow-aggressive --aggressive-promote-days 5
```

3.14 Daily dry-run health check (no real orders):
```bash
# Connectivity/auth + market ticker + 1 USDT dry-run plan
python3 scripts/health_check_dryrun.py --symbol BTCUSDT --notional-usdt 1 --format text
```

Output:
- `results/decision_scorecard_*.json`
- `results/decision_scorecard_*.md`

Report includes:
- active/target profile + switched state
- `hold_cash` vs `deploy` decision
- short-window checks and signal metrics
- executable instructions + guardrails + next-check command
- holdings-aware adjustment suggestion based on `portfolio_snapshot.json` (if present)

Ultra-brief command (3 lines only: profile/action/amount):
```bash
python3 scripts/daily_execution_report.py --capital-cny 10000 --format brief
```

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
