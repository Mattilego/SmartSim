# SmartSim

SmartSim compares SimulationCraft gear combinations without simulating every variant to full precision. It expands item, gem, and enchant options from a SimC profile, then repeatedly simulates only the combinations that could still be best until the remaining set is statistically decided.

## Requirements

- Python 3 (standard library only)
- [SimulationCraft](https://www.simulationcraft.org/) (`simc.exe` on Windows, or any SimC binary)

Put the SimC executable next to `main.py`, or set `simc_path` in `settings.json` to its location.

## Input files

| File | Role |
| --- | --- |
| `profile.simc` | Character, talents, and gear. Commented extra lines are treated as alternatives. |
| `options.simc` | Fight options, APL, consumables, and other SimC settings shared by every combo. |
| `settings.json` | Path to SimC, batch size, stopping error, gem rules, and enchant options. |

### Gear alternatives

Active `key=value` lines are the base loadout. Any commented line with the same slot (or `talents` / `apl_variable.*`) is an extra option.

Rings, trinkets, and weapons are pooled:

- Rings and trinkets: every unordered pair of distinct item IDs
- Weapons: each `2_hand`, plus each `main_hand`/`1_hand` with each `off_hand`/`1_hand`

Duplicate identical loadouts are dropped. Combo `0` is always the unchanged base profile.

### `settings.json`

```json
{
  "simc_path": "simc.exe",
  "batch_size": 20,
  "target_absolute_error": 1,
  "target_relative_error": 0.00000005,
  "confidence": 0.95,
  "gems": {
    "32196": {},
    "213488": { "max": 1, "slots": ["neck", "wrist"] },
    "68780": { "min": 1, "max": 1, "meta": true, "slots": ["head"] }
  },
  "enchants": {}
}
```

- **`batch_size`**: how many combos get extra iterations in each allocation pass (each grant is 1000 iterations).
- **`target_absolute_error` / `target_relative_error`**: a combo is “precise enough” when the width of the confidence interva is either < target_absolute_error or < taret_relative_error * the mean
- **`confidence`**: used for upper/lower confidence bounds (UCB/LCB).
- **`gems`**: keys are gem IDs. Optional fields:
  - `min` / `max`: how many copies may be used (`max: -1` means fill remaining sockets)
  - `slots`: preferred slots
  - `meta`: meta gems only go in sockets that already had a meta gem on the item
- **`enchants`**: map of slot name → list of `enchant_id` integers. Each listed enchant is tried in addition to leaving the item’s existing enchant.

Gems are placed only in sockets that already exist on the item (`gem_id` in the profile).

## Run

From the project directory:

```bash
python main.py
```

Each run deletes previous `profiles/`, `batch_*.simc`, and `results_*.json` files first.

What happens:

1. Generate one `.simc` profileset file per unique combo under `profiles/`.
2. Simulate every combo for 100 iterations (in chunks of 200 to limit SimC memory use).
3. Drop combos whose UCB is below the current best mean DPS.
4. Allocate more iterations to remaining imprecise combos and merge results.
5. Repeat until nothing extra is allocated, or every remaining combo is precise.

Progress prints after each allocation batch (top mean, highest UCB, remaining count, estimated time). At the end it prints **Final Survivors**: mean, std, iterations, UCB/LCB, and a description of what changed vs the base profile.

## License

Use and modify as you like for personal SimulationCraft work.
