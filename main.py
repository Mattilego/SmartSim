#!/usr/bin/env python3
"""
Generate minimal profileset .simc files (only changed lines) for each gear combo.
Also builds a batch file to run them.
"""

import sys
import json
import os
import itertools
import re
import time
import shutil
import glob
from collections import defaultdict

# ---------- Constants ----------
CLASSES = [
    "death_knight", "demon_hunter", "druid", "hunter", "mage",
    "monk", "paladin", "priest", "shaman", "rogue", "warlock", "warrior"
]
SLOTS = [
    "head", "neck", "shoulders", "back", "chest", "wrists",
    "hands", "waist", "legs", "feet",
    "finger1", "finger2", "trinket1", "trinket2",
    "main_hand", "off_hand", "1_hand", "2_hand"
]

# ---------- Cleanup Function ----------
def cleanup_previous_runs(profile_dir="profiles"):
    """
    Remove all artifacts from previous runs:
      - the entire profile directory (containing profile_*.simc files)
      - any batch_*.simc and results_*.json files in the current directory
    """
    # Remove profile directory
    if os.path.exists(profile_dir):
        shutil.rmtree(profile_dir)
        print(f"Removed previous profile directory: {profile_dir}")

    # Remove generated batch and result files
    patterns = ["batch_*.simc", "results_*.json"]
    for pat in patterns:
        for f in glob.glob(pat):
            os.remove(f)
            print(f"Removed {f}")

# ---------- Parsing Functions ----------
def is_single_word(s):
    return ' ' not in s and '\t' not in s

def parse_profile(file_path):
    """
    Returns (result_dict, active_pairs, commented_pairs)
    where active_pairs: key->value for all active key=value lines (except class? we keep class too)
    """
    active_pairs = {}
    commented_pairs = {}
    with open(file_path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith('#'):
                content = line[1:].lstrip()
                if '=' in content and is_single_word(content.split('=', 1)[0].strip()):
                    key, value = content.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    commented_pairs.setdefault(key, []).append(value)
            else:
                if '=' in line and is_single_word(line.split('=', 1)[0].strip()):
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    active_pairs[key] = value

    result = {
        "player_name": None,
        "talents": None,
        "commented_talents": [],
        "equipment": {},
        "commented_equipment": {},
        "apl_variables": {},
        "commented_apl_variables": {}
    }
    for cls in CLASSES:
        if cls in active_pairs:
            result["player_name"] = active_pairs[cls]
            break
    result["talents"] = active_pairs.get("talents")
    result["commented_talents"] = commented_pairs.get("talents", [])
    for slot in SLOTS:
        if slot in active_pairs:
            result["equipment"][slot] = active_pairs[slot]
        if slot in commented_pairs:
            result["commented_equipment"][slot] = commented_pairs[slot]
    for key, value in active_pairs.items():
        if key.startswith("apl_variable."):
            result["apl_variables"][key[len("apl_variable."):]] = value
    for key, values in commented_pairs.items():
        if key.startswith("apl_variable."):
            result["commented_apl_variables"][key[len("apl_variable."):]] = values
    return result, active_pairs, commented_pairs

def parse_settings(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    required_keys = ["simc_path", "batch_size", "confidence",
                     "target_absolute_error", "target_relative_error",
                     "gems", "enchants"]
    for key in required_keys:
        if key not in data:
            raise ValueError(f"Missing required key: {key}")
    if not isinstance(data["gems"], dict) or not isinstance(data["enchants"], dict):
        raise ValueError("gems and enchants must be objects")
    processed_gems = {}
    for gem_id, spec in data["gems"].items():
        if not isinstance(spec, dict):
            raise ValueError(f"Gem spec for '{gem_id}' must be an object")
        processed_gems[gem_id] = {
            "min": spec.get("min", 0),
            "max": spec.get("max", -1),
            "slots": spec.get("slots", []),
            "meta": spec.get("meta", False)
        }
    data["gems"] = processed_gems
    for slot, vals in data["enchants"].items():
        if not isinstance(vals, list) or not all(isinstance(v, int) for v in vals):
            raise ValueError(f"Enchant values for '{slot}' must be a list of integers")
    return data

# ---------- Item Handling ----------
def parse_item_string(s):
    if not s:
        return None
    parts = s.split(',')
    base = parts[0].strip()
    fields = {}
    for part in parts[1:]:
        if '=' in part:
            k, v = part.split('=', 1)
            fields[k.strip()] = v.strip()
    return {"base": base, "fields": fields}

def format_item_string(item):
    if not item:
        return ""
    parts = [item["base"]]
    for k, v in item["fields"].items():
        parts.append(f"{k}={v}")
    return ",".join(parts)

def get_socket_count(item):
    if item and "gem_id" in item["fields"]:
        gems = item["fields"]["gem_id"]
        return len(gems.split('/')) if gems else 0
    return 0

def set_gems(item, gem_list):
    if gem_list:
        item["fields"]["gem_id"] = "/".join(str(g) for g in gem_list if g is not None)
    else:
        item["fields"].pop("gem_id", None)

def set_enchant(item, enchant_id):
    if enchant_id is not None:
        item["fields"]["enchant_id"] = str(enchant_id)
    else:
        item["fields"].pop("enchant_id", None)

# ---------- Gear Combination Generation ----------
def collect_items_for_slot(slot, profile_data):
    items = []
    if slot in profile_data["equipment"]:
        items.append(parse_item_string(profile_data["equipment"][slot]))
    if slot in profile_data["commented_equipment"]:
        for comment in profile_data["commented_equipment"][slot]:
            items.append(parse_item_string(comment))
    return [it for it in items if it is not None]

def generate_gear_combinations(profile_data):
    normal_slots = [s for s in SLOTS if s not in ('main_hand','off_hand','1_hand','2_hand',
                                                  'finger1','finger2','trinket1','trinket2')]
    normal_options = {}
    for slot in normal_slots:
        opts = collect_items_for_slot(slot, profile_data)
        normal_options[slot] = opts if opts else [None]

    finger_pool = collect_items_for_slot('finger1', profile_data) + collect_items_for_slot('finger2', profile_data)
    finger_combos = []
    if len(finger_pool) >= 2:
        for (i, a), (j, b) in itertools.combinations(enumerate(finger_pool), 2):
            if a["base"] == b["base"]:
                continue
            finger_combos.append(('finger1', a, 'finger2', b))
    else:
        f1 = collect_items_for_slot('finger1', profile_data) or [None]
        f2 = collect_items_for_slot('finger2', profile_data) or [None]
        for a in f1:
            for b in f2:
                if a is None or b is None or a["base"] != b["base"]:
                    finger_combos.append(('finger1', a, 'finger2', b))

    trinket_pool = collect_items_for_slot('trinket1', profile_data) + collect_items_for_slot('trinket2', profile_data)
    trinket_combos = []
    if len(trinket_pool) >= 2:
        for (i, a), (j, b) in itertools.combinations(enumerate(trinket_pool), 2):
            if a["base"] == b["base"]:
                continue
            trinket_combos.append(('trinket1', a, 'trinket2', b))
    else:
        t1 = collect_items_for_slot('trinket1', profile_data) or [None]
        t2 = collect_items_for_slot('trinket2', profile_data) or [None]
        for a in t1:
            for b in t2:
                if a is None or b is None or a["base"] != b["base"]:
                    trinket_combos.append(('trinket1', a, 'trinket2', b))

    two_hand_pool = collect_items_for_slot('2_hand', profile_data)
    main_pool = collect_items_for_slot('main_hand', profile_data) + collect_items_for_slot('1_hand', profile_data)
    off_pool = collect_items_for_slot('off_hand', profile_data) + collect_items_for_slot('1_hand', profile_data)

    weapon_combos = []
    for item in two_hand_pool:
        if item is not None:
            weapon_combos.append(('main_hand', item, 'off_hand', None))
    for m in main_pool:
        for o in off_pool:
            if m is None or o is None:
                continue
            if m["base"] == o["base"]:
                continue
            weapon_combos.append(('main_hand', m, 'off_hand', o))
    if not weapon_combos:
        weapon_combos.append(('main_hand', None, 'off_hand', None))

    normal_product = itertools.product(*(normal_options[s] for s in normal_slots))
    for normal_items in normal_product:
        normal_dict = dict(zip(normal_slots, normal_items))
        for finger_combo in finger_combos:
            finger_dict = {finger_combo[0]: finger_combo[1], finger_combo[2]: finger_combo[3]}
            for trinket_combo in trinket_combos:
                trinket_dict = {trinket_combo[0]: trinket_combo[1], trinket_combo[2]: trinket_combo[3]}
                for w_combo in weapon_combos:
                    weapon_dict = {w_combo[0]: w_combo[1], w_combo[2]: w_combo[3]}
                    combo = {}
                    combo.update(normal_dict)
                    combo.update(finger_dict)
                    combo.update(trinket_dict)
                    combo.update(weapon_dict)
                    combo = {k: v for k, v in combo.items() if v is not None}
                    yield combo

# ---------- Gem Placement ----------
def get_sockets(gear_combo):
    sockets = []
    for slot, item in gear_combo.items():
        count = get_socket_count(item)
        for idx in range(count):
            sockets.append((slot, idx))
    return sockets

def generate_gem_placements(gear_combo, gems_settings):
    sockets = get_sockets(gear_combo)
    if not sockets:
        yield {}
        return

    gem_ids = list(gems_settings.keys())
    if not gem_ids:
        # No gem replacements specified; keep original gems
        yield {}
        return


    total_sockets = len(sockets)
    gem_ids = list(gems_settings.keys())
    vectors = []
    def dfs(gem_idx, remaining, counts, meta_used):
        if gem_idx == len(gem_ids):
            if remaining == 0:
                vectors.append(counts.copy())
            return
        gid = gem_ids[gem_idx]
        spec = gems_settings[gid]
        min_c = spec.get('min', 0)
        max_c = spec.get('max', -1)
        if max_c == -1:
            max_c = remaining
        else:
            max_c = min(max_c, remaining)
        if spec.get('meta', False):
            max_c = min(max_c, 1)
            if meta_used:
                max_c = 0
        for cnt in range(min_c, max_c + 1):
            counts[gid] = cnt
            dfs(gem_idx + 1, remaining - cnt, counts, meta_used or (spec.get('meta', False) and cnt > 0))
        counts.pop(gid, None)

    dfs(0, total_sockets, {}, False)

    for vec in vectors:
        items = [(gid, cnt) for gid, cnt in vec.items() if cnt > 0]
        items.sort(key=lambda x: (
            - (1 if gems_settings[x[0]].get('meta', False) else 0),
            - len(gems_settings[x[0]].get('slots', []))
        ))
        socket_list = sockets[:]
        placement = {}
        for gid, count in items:
            spec = gems_settings[gid]
            pref_slots = spec.get('slots', [])
            if spec.get('meta', False):
                pref_slots = ['head']
            placed = 0
            for slot, idx in socket_list[:]:
                if placed >= count:
                    break
                if slot in pref_slots:
                    placement[(slot, idx)] = gid
                    socket_list.remove((slot, idx))
                    placed += 1
            if placed < count:
                for slot, idx in socket_list[:]:
                    if placed >= count:
                        break
                    placement[(slot, idx)] = gid
                    socket_list.remove((slot, idx))
                    placed += 1
        yield placement

# ---------- Enchant Combinations ----------
def generate_enchant_combinations(gear_combo, enchants_settings):
    slots_with_items = list(gear_combo.keys())
    options_per_slot = {}
    for slot in slots_with_items:
        opts = [None]
        if slot in enchants_settings:
            opts.extend(enchants_settings[slot])
        options_per_slot[slot] = opts
    for combo in itertools.product(*(options_per_slot[s] for s in slots_with_items)):
        yield dict(zip(slots_with_items, combo))

# ---------- Main Generator (minimal profileset files) ----------
def generate_profiles(profile_file, settings_file, output_dir):
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    profile_data, active_pairs_orig, _ = parse_profile(profile_file)
    settings_data = parse_settings(settings_file)
    gems_settings = settings_data.get('gems', {})
    enchants_settings = settings_data.get('enchants', {})

    # ----- Write the base (no‑change) profile as profile_0.simc -----
    base_path = os.path.join(output_dir, "profile_0.simc")
    with open(base_path, 'w', encoding='utf-8') as f:
        f.write("")   # empty file – no overrides
    descriptions = {0: "base gear (no changes)"}

    gear_combos = list(generate_gear_combinations(profile_data))
    combo_counter = 1          # start numbering changed combos from 1
    seen = set()               # stores final gear strings for deduplication

    for gear_combo in gear_combos:
        gem_placements = list(generate_gem_placements(gear_combo, gems_settings))
        for gem_placement in gem_placements:
            enchant_combos = list(generate_enchant_combinations(gear_combo, enchants_settings))
            for enchant_assignment in enchant_combos:
                # Build the final gear strings for each slot
                final_gear = []
                slot_items = {}

                for slot, item in gear_combo.items():
                    new_item = {
                        "base": item["base"],
                        "fields": item["fields"].copy()
                    }

                    # Apply gems
                    num_sockets = get_socket_count(item)
                    slot_gems = []
                    for idx in range(num_sockets):
                        gid = gem_placement.get((slot, idx))
                        slot_gems.append(gid)
                    if any(g is not None for g in slot_gems):
                        set_gems(new_item, slot_gems)

                    # Apply enchant
                    enchant = enchant_assignment.get(slot)
                    if enchant is not None:
                        set_enchant(new_item, enchant)

                    formatted = format_item_string(new_item)
                    slot_items[slot] = formatted
                    final_gear.append((slot, formatted))

                # Deduplicate
                key = tuple(sorted(final_gear))
                if key in seen:
                    continue
                seen.add(key)

                # Build change set (only lines that differ from original)
                new_pairs = {}
                for slot, formatted in slot_items.items():
                    if slot in active_pairs_orig and formatted != active_pairs_orig[slot]:
                        new_pairs[slot] = formatted
                    elif slot not in active_pairs_orig:
                        new_pairs[slot] = formatted

                # Write profile file only if there are changes
                if new_pairs:
                    desc_parts = [f"{k}={v}" for k, v in sorted(new_pairs.items())]
                    desc = "; ".join(desc_parts)
                    out_path = os.path.join(output_dir, f"profile_{combo_counter}.simc")
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(f"{k}={v}" for k, v in sorted(new_pairs.items())) + '\n')
                    descriptions[combo_counter] = desc
                    combo_counter += 1

    print(f"Generated {combo_counter - 1} changed profiles + base profile 0 in {output_dir}")
    return descriptions

# ---------- Batch Builder ----------
def build_batch_simc(iterations_dict, folder_name, output_file='batch.simc',
                     profile_file='profile.simc', options_file='options.simc'):
    profile_data, _, _ = parse_profile(profile_file)
    player_name = profile_data.get('player_name')
    if not player_name:
        raise ValueError(f"Player name not found in {profile_file}")

    deterministic = any(it < 0 for it in iterations_dict.values())

    lines = []
    lines.append("iterations=1")
    lines.append("target_error=0")
    if deterministic:
        lines.append("seed=123456")
    lines.append(f"input={profile_file}")
    lines.append(f"input={options_file}")
    lines.append(f"active={player_name}")
    lines.append(f'path=".\\{folder_name}"')

    for combo_id in sorted(iterations_dict.keys()):
        iters = abs(iterations_dict[combo_id])
        lines.append(f'input="profile_{combo_id}.simc"')
        lines.append(f'profileset."Combo {combo_id}"+=iterations={iters}')

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"Batch file written to {output_file}")

def create_batch_with_deterministic(profile_file, settings_file, output_dir='profiles',
                                    options_file='options.simc', batch_file='batch.simc'):
    """
    Generate all profile variants and create a combined .simc file with iterations=-4
    for every combo (deterministic mode).
    Returns the path to the generated batch file.
    """
    # 1. Generate the profileset files (minimal overrides)
    generate_profiles(profile_file, settings_file, output_dir)

    # 2. Find all profile_<id>.simc files that were created
    import re, os
    max_id = -1
    for fname in os.listdir(output_dir):
        m = re.match(r'profile_(\d+)\.simc', fname)
        if m:
            max_id = max(max_id, int(m.group(1)))
    if max_id < 0:
        print("No profiles were generated.")
        return None

    # 3. Build iteration dict: every combo gets -4 (deterministic)
    iterations_dict = {i: -4 for i in range(max_id + 1)}

    # 4. Create the batch file
    build_batch_simc(iterations_dict, output_dir, batch_file, profile_file, options_file)
    return batch_file

import subprocess
import json

def run_simc_and_parse_results(batch_file, simc_path, json_output_file="results.json"):
    """
    Execute simc with the given batch file and JSON output.
    Returns a list of dicts: [{'name': profileset_name, 'mean': dps_mean}, ...]
    """
    # Build command: simc batch_file.simc json=results.json
    cmd = [simc_path, batch_file, f"json={json_output_file}"]
    print(f"Running: {' '.join(cmd)}")
    try:
        # Run simc; it will write the JSON file
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"SimC error (stderr):\n{e.stderr}")
        raise RuntimeError("SimulationCraft execution failed") from e

    # Parse the JSON results
    with open(json_output_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # The structure: data['sim']['profilesets']['results'] is a list of dicts with 'name' and 'mean'
    results = data.get('sim', {}).get('profilesets', {}).get('results', [])
    extracted = []
    for entry in results:
        # entry is like {'name': 'Combo 0', 'mean': 12345.6, ...}
        extracted.append({
            'name': entry.get('name'),
            'mean': entry.get('mean')
        })
    return extracted

def process_results(results):
    """
    Accepts a list of dicts: [{'name': 'Combo X', 'mean': dps}, ...]
    Returns:
      - unique_list: list of dicts with keys 'id', 'mean', 'iterations' (0), 'ucb' (Inf), 'lcb' (0)
      - duplicate_list: list of dicts with keys 'id', 'same_as' (the id of the first combo with that mean)
    """
    seen_means = {}  # mean -> first combo id
    unique_list = []
    duplicate_list = []

    for entry in results:
        # Extract combo id from name (assumes name is "Combo <id>")
        name = entry.get('name', '')
        try:
            combo_id = int(name.split()[1])  # "Combo 0" -> 0
        except (IndexError, ValueError):
            print(f"Warning: unexpected profileset name format: {name}")
            continue

        mean = entry.get('mean')
        if mean is None:
            print(f"Warning: no mean for {name}")
            continue

        if mean in seen_means:
            # Duplicate mean
            duplicate_list.append({
                'id': combo_id,
                'same_as': seen_means[mean]
            })
        else:
            # First occurrence of this mean
            seen_means[mean] = combo_id
            unique_list.append({
                'id': combo_id,
                'mean': mean,
                'iterations': 0,
                'ucb': float('inf'),
                'lcb': 0.0
            })

    return unique_list, duplicate_list

def create_batch_file(iterations_dict, folder_name, profile_file, options_file, output_file):
    """
    Create a batch .simc file with given iterations per combo.
    iterations_dict: {combo_id: iterations} (negative -> deterministic)
    """
    # Get player name
    profile_data, _, _ = parse_profile(profile_file)
    player_name = profile_data.get('player_name')
    if not player_name:
        raise ValueError(f"Player name not found in {profile_file}")

    deterministic = any(it < 0 for it in iterations_dict.values())

    lines = []
    lines.append("iterations=0")
    if deterministic:
        lines.append("seed=123456")
    lines.append(f"input={profile_file}")
    lines.append(f"input={options_file}")
    lines.append(f"active={player_name}")
    #lines.append(f'path=".\\{folder_name}"')

    for combo_id in sorted(iterations_dict.keys()):
        iters = abs(iterations_dict[combo_id])
        lines.append(f'input="profile_{combo_id}.simc"')
        lines.append(f'profileset."Combo {combo_id}"+=iterations={iters}')

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"Batch file written to {output_file}")
    return output_file


def run_simc_and_parse_results(batch_file, simc_path, json_output_file="results.json"):
    """
    Execute simc with the given batch file and JSON output.
    Returns a list of dicts: [{'name': profileset_name, 'mean': dps, 'mean_stddev': std}, ...]
    """
    cmd = [simc_path, batch_file, f"json={json_output_file}"]
    print(f"Running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"SimC error (stderr):\n{e.stderr}")
        raise RuntimeError("SimulationCraft execution failed") from e

    with open(json_output_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    results = data.get('sim', {}).get('profilesets', {}).get('results', [])
    extracted = []
    for entry in results:
        extracted.append({
            'name': entry.get('name'),
            'mean': entry.get('mean'),
            'mean_stddev': entry.get('mean_stddev')
        })
    return extracted


def process_results(results):
    """
    Accepts results list from simc; returns unique list and duplicate list.
    """
    seen_means = {}
    unique_list = []
    duplicate_list = []

    for entry in results:
        name = entry.get('name', '')
        try:
            combo_id = int(name.split()[1])
        except (IndexError, ValueError):
            print(f"Warning: unexpected profileset name format: {name}")
            continue

        mean = entry.get('mean')
        if mean is None:
            print(f"Warning: no mean for {name}")
            continue

        if mean in seen_means:
            duplicate_list.append({'id': combo_id, 'same_as': seen_means[mean]})
        else:
            seen_means[mean] = combo_id
            unique_list.append({
                'id': combo_id,
                'mean': 0.0,
                'mean_stddev': 0.0,
                'iterations': 0,
                'ucb': float('inf'),
                'lcb': 0.0
            })
    return unique_list, duplicate_list


def compute_ucb_lcb(mean, stddev, confidence):
    """
    Compute UCB and LCB using the formula:
    mean ± mean_stddev * (1/(16*(1-confidence) - 0.5) + 1.1)
    """
    factor = 1.0 / (16.0 * (1-confidence) - 0.5) + 1.1
    ucb = mean + stddev * factor
    lcb = mean - stddev * factor
    return ucb, lcb

import math

def allocate_iterations(combos, batch_size, target_abs, target_rel):
    """
    Given a list of combo dicts (each with 'id', 'mean', 'ucb', 'iterations'),
    allocate up to batch_size*100 iterations among combos that are not precise.
    Returns a list of (combo_id, iterations_to_run).
    """
    # Work on a copy so we don't modify the original
    alloc_list = [
        {
            'id': c['id'],
            'mean': c['mean'],
            'ucb': c['ucb'],
            'iterations': c['iterations']
        }
        for c in combos
    ]

    total_iters = batch_size * 100
    allocated = 0
    allocations = []  # list of (id, additional_iterations)

    while allocated < total_iters:
        # Check if all remaining combos are already precise
        all_precise = True
        for a in alloc_list:
            interval_width = (a['ucb'] - a['mean']) * 2   # ucb - lcb
            if interval_width > target_abs and (interval_width / a['mean']) > target_rel:
                all_precise = False
                break
        if all_precise:
            break

        # Pick the combo with highest UCB that is not precise
        best = None
        best_ucb = -float('inf')
        for a in alloc_list:
            interval_width = (a['ucb'] - a['mean']) * 2
            if interval_width > target_abs and (interval_width / a['mean']) > target_rel:
                if a['ucb'] > best_ucb:
                    best_ucb = a['ucb']
                    best = a
        if best is None:
            break   # no combo needs more iterations

        # Give it 100 iterations
        old_iter = best['iterations']
        new_iter = old_iter + 100
        old_ucb = best['ucb']
        mean = best['mean']
        new_ucb = mean + (old_ucb - mean) * math.sqrt(old_iter / new_iter)
        best['ucb'] = new_ucb
        best['iterations'] = new_iter
        allocated += 100
        # Record that we allocated 100 iterations to this combo
        allocations.append((best['id'], 100))

    # Combine allocations for the same combo
    combined = {}
    for cid, add_iters in allocations:
        combined[cid] = combined.get(cid, 0) + add_iters

    return [(cid, iters) for cid, iters in combined.items() if iters > 0]

def merge_stats(old_mean, old_stddev, old_iter, new_mean, new_stddev, new_iter):
    """
    Merge old and new simulation statistics.
    Returns (total_mean, total_stddev, total_mean_stddev, total_iter)
    """
    total_iter = old_iter + new_iter
    total_mean = (old_mean * old_iter + new_mean * new_iter) / total_iter

    var1 = old_stddev ** 2
    var2 = new_stddev ** 2
    # Combined variance formula
    total_var = ((old_iter - 1) * var1 + (new_iter - 1) * var2 +
                 old_iter * new_iter * (old_mean - new_mean) ** 2 / total_iter) / (total_iter - 1)
    total_stddev = math.sqrt(total_var)
    total_mean_stddev = total_stddev / math.sqrt(total_iter)
    return total_mean, total_stddev, total_mean_stddev, total_iter

def estimate_remaining_batches(combos, batch_size, target_abs, target_rel):
    """
    Estimate total additional iterations needed for all non‑precise combos.
    Returns (total_iterations_needed, estimated_batches).
    """
    total_needed = 0.0
    for c in combos:
        width = (c['ucb'] - c['mean']) * 2  # ucb - lcb
        # Effective target: stop when width <= target_abs OR width <= mean * target_rel
        # i.e., width <= max(target_abs, mean * target_rel)
        target_width = max(target_abs, c['mean'] * target_rel)
        if width <= target_width:
            continue
        old_iter = c['iterations']
        r = target_width / width if width > 0 else 1.0
        if r >= 1.0:
            continue
        # Solve: width * sqrt(old_iter/(old_iter + x)) = target_width
        x = old_iter * ((width / target_width) ** 2 - 1)
        if x > 0:
            total_needed += x

    # Each batch can run batch_size * 100 iterations total (batch_size chunks of 100).
    # Use ceil division.
    if total_needed <= 0:
        estimated_batches = 1
    else:
        estimated_batches = int(total_needed / (batch_size * 100)) + 1
    return total_needed, estimated_batches
def print_progress(batch_num, all_combos, remaining_combos, target_abs, target_rel,
                   total_estimated_batches, start_time):
    """
    Print a progress summary.
    """
    if not remaining_combos:
        print("No remaining combos.")
        return

    # Top mean and top combo
    top_combo = max(remaining_combos, key=lambda c: c['mean'])
    top_mean = top_combo['mean']
    top_precision_abs = (top_combo['ucb'] - top_combo['lcb'])  # = 2*(ucb-mean)
    top_precision_rel = top_precision_abs / top_mean if top_mean != 0 else float('inf')

    # Remaining count
    remaining_count = len(remaining_combos)

    # Estimate remaining batches (if not already computed)
    _, remaining_batches = estimate_remaining_batches(remaining_combos, batch_size, target_abs, target_rel)

    # Progress: done batches = batch_num, total = done + remaining_batches
    total_est = batch_num + remaining_batches
    progress_pct = (batch_num / total_est) * 100 if total_est > 0 else 0

    # Time estimation
    elapsed = time.time() - start_time
    avg_time_per_batch = elapsed / batch_num if batch_num > 0 else 0
    est_time_remaining = avg_time_per_batch * remaining_batches

    print("\n" + "="*60)
    print(f"Batch {batch_num}")
    print(f"  Top mean DPS: {top_mean:.2f}")
    print(f"  Remaining combos: {remaining_count}")
    print(f"  Top combo precision (abs): {top_precision_abs:.2f}, (rel): {top_precision_rel:.2%}")
    print(f"  Estimated remaining batches: {remaining_batches}")
    print(f"  Progress: {progress_pct:.1f}%")
    if avg_time_per_batch > 0:
        print(f"  Est. time remaining: {est_time_remaining/60:.1f} min")
    else:
        print("  Est. time remaining: unknown")
    print("="*60)

if __name__ == "__main__":
    import sys, subprocess, json, os, re, math

    profile_file = "profile.simc"
    settings_file = "settings.json"
    out_dir = "profiles"
    opts_file = "options.simc"

    # Clean up previous runs first
    cleanup_previous_runs(out_dir)

    # Load settings
    settings = parse_settings(settings_file)
    simc_path = settings.get('simc_path')
    confidence = settings.get('confidence')
    batch_size = settings.get('batch_size')
    target_abs = settings.get('target_absolute_error')
    target_rel = settings.get('target_relative_error')

    # Step 1: Generate minimal profiles and get descriptions
    descriptions = generate_profiles(profile_file, settings_file, out_dir)

    # Step 2: Find all generated profile IDs
    max_id = -1
    for fname in os.listdir(out_dir):
        m = re.match(r'profile_(\d+)\.simc', fname)
        if m:
            max_id = max(max_id, int(m.group(1)))
    if max_id < 0:
        print("No profiles generated.")
        sys.exit(1)

    all_ids = list(range(max_id + 1))

    # Step 3: First batch (deterministic) to find unique DPS
    first_iter = {i: -4 for i in all_ids}
    batch_file1 = create_batch_file(first_iter, out_dir, profile_file, opts_file, "batch_det.simc")
    raw_results1 = run_simc_and_parse_results(batch_file1, simc_path, "results_det.json")
    unique_list, duplicate_list = process_results(raw_results1)

    print(f"Found {len(unique_list)} unique DPS values, {len(duplicate_list)} duplicates.")

    # Step 4: Second batch (50 iterations) on unique combos
    unique_ids = [u['id'] for u in unique_list]
    second_iter = {i: 50 for i in unique_ids}
    batch_file2 = create_batch_file(second_iter, out_dir, profile_file, opts_file, "batch_50.simc")
    raw_results2 = run_simc_and_parse_results(batch_file2, simc_path, "results_50.json")
    result_map = {r['name']: r for r in raw_results2}
    for u in unique_list:
        name = f"Combo {u['id']}"
        if name in result_map:
            r = result_map[name]
            u['mean'] = r['mean']
            u['mean_stddev'] = r['mean_stddev']
            u['stddev'] = r['mean_stddev'] * math.sqrt(u['iterations'])
            u['ucb'], u['lcb'] = compute_ucb_lcb(u['mean'], u['mean_stddev'], confidence)
        else:
            print(f"Warning: no result for {name}")

    # Master list of all unique combos
    master_list = unique_list
    survivor_ids = set()

    # Start timing
    start_time = time.time()

    loop_count = 0
    while True:
        # 1. Find highest mean
        max_mean = max(u['mean'] for u in master_list)

        # 2. Filter out dominated (UCB < max_mean)
        remaining = [u for u in master_list if u['ucb'] >= max_mean]
        survivor_ids = {u['id'] for u in remaining}

        if not remaining:
            print("All combos dominated. Stopping.")
            break

        # 3. Allocate iterations
        allocations = allocate_iterations(remaining, batch_size, target_abs, target_rel)

        if not allocations:
            print("No additional iterations allocated (all remaining combos precise).")
            break

        # --- Progress report before running this batch ---
        print_progress(loop_count, master_list, remaining, target_abs, target_rel,
                       None, start_time)  # total_estimated_batches will be computed inside

        # 4. Run batch
        iter_dict = {cid: iters for cid, iters in allocations}
        batch_file = create_batch_file(iter_dict, out_dir, profile_file, opts_file,
                                       f"batch_alloc_{loop_count}.simc")
        raw_results = run_simc_and_parse_results(batch_file, simc_path, f"results_alloc_{loop_count}.json")
        result_map = {r['name']: r for r in raw_results}

        # 5. Merge results
        for u in master_list:
            name = f"Combo {u['id']}"
            if name in result_map:
                r = result_map[name]
                new_mean = r['mean']
                new_mean_stddev = r['mean_stddev']
                new_iter = iter_dict[u['id']]
                new_stddev = new_mean_stddev * math.sqrt(new_iter)

                merged_mean, merged_stddev, merged_mean_stddev, merged_iter = merge_stats(
                    u['mean'], u['stddev'], u['iterations'],
                    new_mean, new_stddev, new_iter
                )

                u['mean'] = merged_mean
                u['stddev'] = merged_stddev
                u['mean_stddev'] = merged_mean_stddev
                u['iterations'] = merged_iter
                u['ucb'], u['lcb'] = compute_ucb_lcb(merged_mean, merged_mean_stddev, confidence)

        loop_count += 1

    # At this point, survivor_ids contains the IDs of combos that survived the final filter.
    # Build final results for survivors and their duplicates.
    final_results = []
    for u in master_list:
        if u['id'] in survivor_ids:
            final_results.append({
                'id': u['id'],
                'mean': u['mean'],
                'stddev': u['stddev'],
                'mean_stddev': u['mean_stddev'],
                'iterations': u['iterations'],
                'ucb': u['ucb'],
                'lcb': u['lcb'],
                'changes': descriptions.get(u['id'], 'unknown')
            })

    # Add duplicates that map to survivors
    for d in duplicate_list:
        if d['same_as'] in survivor_ids:
            same_entry = next((u for u in master_list if u['id'] == d['same_as']), None)
            if same_entry:
                final_results.append({
                    'id': d['id'],
                    'mean': same_entry['mean'],
                    'stddev': same_entry['stddev'],
                    'mean_stddev': same_entry['mean_stddev'],
                    'iterations': same_entry['iterations'],
                    'ucb': same_entry['ucb'],
                    'lcb': same_entry['lcb'],
                    'duplicate_of': d['same_as'],
                    'changes': descriptions.get(d['id'], 'unknown')
                })

    # Output final survivors
    print("\n=== Final Survivors ===")
    final_results.sort(key=lambda x: x['id'])
    for r in final_results:
        if 'duplicate_of' in r:
            print(f"Combo {r['id']}: mean={r['mean']:.2f} (same as Combo {r['duplicate_of']})")
        else:
            print(f"Combo {r['id']}: mean={r['mean']:.2f}, std={r['stddev']:.2f}, "
                  f"iter={r['iterations']}, UCB={r['ucb']:.2f}, LCB={r['lcb']:.2f}")
        print(f"  Changes: {r['changes']}\n")