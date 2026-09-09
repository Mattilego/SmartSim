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
import math
import subprocess
from collections import defaultdict

# ---------- Constants ----------
CLASSES = [
    "death_knight", "demon_hunter", "druid", "hunter", "mage",
    "monk", "paladin", "priest", "shaman", "rogue", "warlock", "warrior"
]
SLOTS = [
    "head", "neck", "shoulder", "back", "chest", "wrists",
    "hands", "waist", "legs", "feet",
    "finger1", "finger2", "trinket1", "trinket2",
    "main_hand", "off_hand", "1_hand", "2_hand"
]

def get_item_id(item):
    return item.get("fields", {}).get("id") if item else None

# ---------- Checkpoint Functions ----------
CHECKPOINT_FILE = "checkpoint.json"

def save_checkpoint(master_list, loop_count, descriptions):
    data = {
        "version": 1,
        "master_list": master_list,
        "loop_count": loop_count,
        "descriptions": descriptions
    }
    with open(CHECKPOINT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    print(f"Checkpoint saved to {CHECKPOINT_FILE}")

def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return None
    try:
        with open(CHECKPOINT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get("version") != 1:
            print("Warning: checkpoint version mismatch, ignoring.")
            return None
        master_list = data["master_list"]
        loop_count = data["loop_count"]
        descriptions = data["descriptions"]
        print(f"Loaded checkpoint: loop_count={loop_count}, combos={len(master_list)}")
        return master_list, loop_count, descriptions
    except Exception as e:
        print(f"Failed to load checkpoint: {e}")
        return None

def delete_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print(f"Checkpoint {CHECKPOINT_FILE} deleted.")

# ---------- Cleanup Function ----------
def cleanup_previous_runs(profile_dir="profiles"):
    if os.path.exists(profile_dir):
        shutil.rmtree(profile_dir)
        print(f"Removed previous profile directory: {profile_dir}")
    patterns = ["batch_*.simc", "results_*.json"]
    for pat in patterns:
        for f in glob.glob(pat):
            os.remove(f)
            print(f"Removed {f}")

# ---------- Parsing Functions ----------
def is_single_word(s):
    return ' ' not in s and '\t' not in s

def parse_profile(file_path):
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
        for i in range(len(finger_pool)):
            for j in range(i+1, len(finger_pool)):
                a, b = finger_pool[i], finger_pool[j]
                if get_item_id(a) == get_item_id(b):
                    continue
                finger_combos.append(('finger1', a, 'finger2', b))
    else:
        f1 = collect_items_for_slot('finger1', profile_data) or [None]
        f2 = collect_items_for_slot('finger2', profile_data) or [None]
        for a in f1:
            for b in f2:
                if a is None or b is None:
                    continue
                if get_item_id(a) == get_item_id(b):
                    continue
                finger_combos.append(('finger1', a, 'finger2', b))

    trinket_pool = collect_items_for_slot('trinket1', profile_data) + collect_items_for_slot('trinket2', profile_data)
    trinket_combos = []
    if len(trinket_pool) >= 2:
        for i in range(len(trinket_pool)):
            for j in range(i+1, len(trinket_pool)):
                a, b = trinket_pool[i], trinket_pool[j]
                if get_item_id(a) == get_item_id(b):
                    continue
                trinket_combos.append(('trinket1', a, 'trinket2', b))
    else:
        t1 = collect_items_for_slot('trinket1', profile_data) or [None]
        t2 = collect_items_for_slot('trinket2', profile_data) or [None]
        for a in t1:
            for b in t2:
                if a is None or b is None:
                    continue
                if get_item_id(a) == get_item_id(b):
                    continue
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
            if get_item_id(m) == get_item_id(o):
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
def get_sockets(gear_combo, gems_settings):
    sockets = []
    for slot, item in gear_combo.items():
        gem_ids = item["fields"].get("gem_id", "")
        gem_list = gem_ids.split('/') if gem_ids else []
        for idx in range(len(gem_list)):
            gid = gem_list[idx]
            is_meta = False
            if gid in gems_settings:
                is_meta = gems_settings[gid].get("meta", False)
            sockets.append((slot, idx, is_meta))
    return sockets

def generate_gem_placements(gear_combo, gems_settings):
    sockets = get_sockets(gear_combo, gems_settings)
    if not sockets:
        yield {}
        return

    meta_sockets = [s for s in sockets if s[2]]
    non_meta_sockets = [s for s in sockets if not s[2]]
    total_meta_sockets = len(meta_sockets)
    total_non_meta_sockets = len(non_meta_sockets)

    gem_ids = list(gems_settings.keys())
    if not gem_ids:
        yield {}
        return

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

    dfs(0, len(sockets), {}, False)

    for vec in vectors:
        total_meta = sum(cnt for gid, cnt in vec.items() if gems_settings[gid].get("meta", False))
        total_non_meta = sum(cnt for gid, cnt in vec.items() if not gems_settings[gid].get("meta", False))
        if total_meta > total_meta_sockets or total_non_meta > total_non_meta_sockets:
            continue

        items = [(gid, cnt) for gid, cnt in vec.items() if cnt > 0]
        items.sort(key=lambda x: (
            - (1 if gems_settings[x[0]].get('meta', False) else 0),
            - len(gems_settings[x[0]].get('slots', []))
        ))

        meta_socket_pool = meta_sockets[:]
        non_meta_socket_pool = non_meta_sockets[:]

        placement = {}
        for gid, count in items:
            spec = gems_settings[gid]
            is_meta = spec.get('meta', False)
            pref_slots = spec.get('slots', [])
            if is_meta:
                pref_slots = ['head']
                pool = meta_socket_pool
            else:
                pool = non_meta_socket_pool

            placed = 0
            for slot, idx, _ in pool[:]:
                if placed >= count:
                    break
                if slot in pref_slots:
                    placement[(slot, idx)] = gid
                    pool.remove((slot, idx, True if is_meta else False))
                    placed += 1
            if placed < count:
                for slot, idx, _ in pool[:]:
                    if placed >= count:
                        break
                    placement[(slot, idx)] = gid
                    pool.remove((slot, idx, True if is_meta else False))
                    placed += 1
            if placed < count:
                break
        else:
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
    os.makedirs(output_dir, exist_ok=True)

    profile_data, active_pairs_orig, _ = parse_profile(profile_file)
    settings_data = parse_settings(settings_file)
    gems_settings = settings_data.get('gems', {})
    enchants_settings = settings_data.get('enchants', {})

    base_path = os.path.join(output_dir, "profile_0.simc")
    with open(base_path, 'w', encoding='utf-8') as f:
        f.write("")
    descriptions = {0: "base gear (no changes)"}

    gear_combos = list(generate_gear_combinations(profile_data))
    combo_counter = 1
    seen = set()

    for gear_combo in gear_combos:
        gem_placements = list(generate_gem_placements(gear_combo, gems_settings))
        for gem_placement in gem_placements:
            enchant_combos = list(generate_enchant_combinations(gear_combo, enchants_settings))
            for enchant_assignment in enchant_combos:
                final_gear = []
                slot_items = {}

                for slot, item in gear_combo.items():
                    new_item = {
                        "base": item["base"],
                        "fields": item["fields"].copy()
                    }

                    num_sockets = get_socket_count(item)
                    slot_gems = []
                    for idx in range(num_sockets):
                        gid = gem_placement.get((slot, idx))
                        slot_gems.append(gid)
                    if any(g is not None for g in slot_gems):
                        set_gems(new_item, slot_gems)

                    enchant = enchant_assignment.get(slot)
                    if enchant is not None:
                        set_enchant(new_item, enchant)

                    formatted = format_item_string(new_item)
                    slot_items[slot] = formatted
                    final_gear.append((slot, formatted))

                key = tuple(sorted(final_gear))
                if key in seen:
                    continue
                seen.add(key)

                new_pairs = {}
                for slot, formatted in slot_items.items():
                    if slot in active_pairs_orig and formatted != active_pairs_orig[slot]:
                        new_pairs[slot] = formatted
                    elif slot not in active_pairs_orig:
                        new_pairs[slot] = formatted

                if new_pairs:
                    prefix = f'profileset."Combo {combo_counter}"+='
                    lines = [f"{prefix}{k}={v}" for k, v in sorted(new_pairs.items())]
                    desc_parts = [f"{k}={v}" for k, v in sorted(new_pairs.items())]
                    desc = "; ".join(desc_parts)
                    out_path = os.path.join(output_dir, f"profile_{combo_counter}.simc")
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(lines) + '\n')
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
    generate_profiles(profile_file, settings_file, output_dir)

    import re, os
    max_id = -1
    for fname in os.listdir(output_dir):
        m = re.match(r'profile_(\d+)\.simc', fname)
        if m:
            max_id = max(max_id, int(m.group(1)))
    if max_id < 0:
        print("No profiles were generated.")
        return None

    iterations_dict = {i: -4 for i in range(max_id + 1)}
    build_batch_simc(iterations_dict, output_dir, batch_file, profile_file, options_file)
    return batch_file

def create_batch_file(iterations_dict, folder_name, profile_file, options_file, output_file):
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

    for combo_id in sorted(iterations_dict.keys()):
        iters = abs(iterations_dict[combo_id])
        lines.append(f'input="profile_{combo_id}.simc"')
        lines.append(f'profileset."Combo {combo_id}"+=iterations={iters}')

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"Batch file written to {output_file}")
    return output_file

def run_simc_and_parse_results(batch_file, simc_path, json_output_file="results.json"):
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

def compute_ucb_lcb(mean, stddev, confidence):
    factor = 1.0 / (16.0 * (1-confidence) 0 0.5) + 1.1
    ucb = mean + stddev * factor
    lcb = mean - stddev * factor
    return ucb, lcb

def allocate_iterations(combos, batch_size, target_abs, target_rel):
    alloc_list = [
        {
            'id': c['id'],
            'mean': c['mean'],
            'ucb': c['ucb'],
            'iterations': c['iterations']
        }
        for c in combos
    ]

    total_iters = batch_size * 1000
    allocated = 0
    allocations = []

    while allocated < total_iters:
        all_precise = True
        for a in alloc_list:
            interval_width = (a['ucb'] - a['mean']) * 2
            if interval_width > target_abs and (interval_width / a['mean']) > target_rel:
                all_precise = False
                break
        if all_precise:
            break

        best = None
        best_ucb = -float('inf')
        for a in alloc_list:
            interval_width = (a['ucb'] - a['mean']) * 2
            if interval_width > target_abs and (interval_width / a['mean']) > target_rel:
                if a['ucb'] > best_ucb:
                    best_ucb = a['ucb']
                    best = a
        if best is None:
            break

        old_iter = best['iterations']
        new_iter = old_iter + 1000
        old_ucb = best['ucb']
        mean = best['mean']
        new_ucb = mean + (old_ucb - mean) * math.sqrt(old_iter / new_iter)
        best['ucb'] = new_ucb
        best['iterations'] = new_iter
        allocated += 1000
        allocations.append((best['id'], 1000))

    combined = {}
    for cid, add_iters in allocations:
        combined[cid] = combined.get(cid, 0) + add_iters

    return [(cid, iters) for cid, iters in combined.items() if iters > 0]

def merge_stats(old_mean, old_stddev, old_iter, new_mean, new_stddev, new_iter):
    total_iter = old_iter + new_iter
    total_mean = (old_mean * old_iter + new_mean * new_iter) / total_iter

    var1 = old_stddev ** 2
    var2 = new_stddev ** 2
    total_var = ((old_iter - 1) * var1 + (new_iter - 1) * var2 +
                 old_iter * new_iter * (old_mean - new_mean) ** 2 / total_iter) / (total_iter - 1)
    total_stddev = math.sqrt(total_var)
    total_mean_stddev = total_stddev / math.sqrt(total_iter)
    return total_mean, total_stddev, total_mean_stddev, total_iter

def estimate_remaining_batches(combos, batch_size, target_abs, target_rel):
    if not combos:
        return 0, 0

    max_mean = max(c['mean'] for c in combos)

    factor = None
    for c in combos:
        if c.get('mean_stddev', 0) > 0:
            factor = (c['ucb'] - c['mean']) / c['mean_stddev']
            break
    if factor is None:
        factor = 1.96

    max_batch_iters = batch_size * 1000

    sim = []
    for c in combos:
        sim.append({
            'id': c['id'],
            'mean': c['mean'],
            'mean_stddev': c['mean_stddev'],
            'iterations': c['iterations'],
            'ucb': c['ucb'],
            'lcb': c['lcb']
        })

    total_added = 0
    batches = 0

    while True:
        active = [c for c in sim if c['ucb'] >= max_mean]
        if not active:
            break

        needs_iter = []
        for c in active:
            width = c['ucb'] - c['lcb']
            if width > target_abs and (width / c['mean']) > target_rel:
                needs_iter.append(c)

        if not needs_iter:
            break

        budget = max_batch_iters
        batches += 1

        while budget > 0 and needs_iter:
            best = max(needs_iter, key=lambda c: c['ucb'])

            add = 1000
            if add > budget:
                add = budget

            old_n = best['iterations']
            new_n = old_n + add
            old_se = best['mean_stddev']
            new_se = old_se * (old_n / new_n) ** 0.5
            best['mean_stddev'] = new_se
            best['iterations'] = new_n
            best['ucb'] = best['mean'] + factor * new_se
            best['lcb'] = best['mean'] - factor * new_se

            total_added += add
            budget -= add

            active = [c for c in sim if c['ucb'] >= max_mean]
            needs_iter = []
            for c in active:
                width = c['ucb'] - c['lcb']
                if width > target_abs and (width / c['mean']) > target_rel:
                    needs_iter.append(c)

            if not needs_iter:
                break

    return total_added, batches

def print_progress(batch_num, all_combos, remaining_combos, target_abs, target_rel,
                   session_start_time, session_batch_count):
    """
    Print progress summary.
    Uses session_start_time and session_batch_count to compute average time per batch
    only from batches run in this session (so that resuming doesn't skew the estimate).
    """
    if not remaining_combos:
        print("No remaining combos.")
        return

    top_combo = max(remaining_combos, key=lambda c: c['mean'])
    top_mean = top_combo['mean']
    top_precision_abs = (top_combo['ucb'] - top_combo['lcb'])
    top_precision_rel = top_precision_abs / top_mean if top_mean != 0 else float('inf')

    highest_ucb_combo = max(remaining_combos, key=lambda c: c['ucb'])
    highest_ucb = highest_ucb_combo['ucb']
    highest_ucb_id = highest_ucb_combo['id']

    remaining_count = len(remaining_combos)

    _, remaining_batches = estimate_remaining_batches(remaining_combos, batch_size, target_abs, target_rel)

    total_est = batch_num + remaining_batches
    progress_pct = (batch_num / total_est) * 100 if total_est > 0 else 0

    # Compute average time per batch only from this session's batches
    if session_batch_count > 0:
        elapsed = time.time() - session_start_time
        avg_time_per_batch = elapsed / session_batch_count
        est_time_remaining = avg_time_per_batch * remaining_batches
        time_str = f"{est_time_remaining/60:.1f} min"
    else:
        time_str = "unknown"

    print("\n" + "="*60)
    print(f"Batch {batch_num}")
    print(f"  Top mean DPS: {top_mean:.2f}")
    print(f"  Highest UCB: {highest_ucb:.2f} (combo {highest_ucb_id})")
    print(f"  Remaining combos: {remaining_count}")
    print(f"  Top combo precision (abs): {top_precision_abs:.2f}, (rel): {top_precision_rel:.2%}")
    print(f"  Estimated remaining batches: {remaining_batches}")
    print(f"  Progress: {progress_pct:.1f}%")
    print(f"  Est. time remaining: {time_str}")
    print("="*60)

if __name__ == "__main__":
    import sys, subprocess, json, os, re, math

    profile_file = "profile.simc"
    settings_file = "settings.json"
    out_dir = "profiles"
    opts_file = "options.simc"

    # Session timing: start now, and count batches run in this session
    session_start_time = time.time()
    session_batch_count = 0

    checkpoint_data = load_checkpoint()
    if checkpoint_data is not None:
        master_list, loop_count, descriptions = checkpoint_data
        print("Resuming from checkpoint. Skipping cleanup, profile generation, and initial chunks.")
        settings = parse_settings(settings_file)
        simc_path = settings.get('simc_path')
        confidence = settings.get('confidence')
        batch_size = settings.get('batch_size')
        target_abs = settings.get('target_absolute_error')
        target_rel = settings.get('target_relative_error')
        # session_batch_count remains 0 – we haven't run any batches in this session yet
    else:
        cleanup_previous_runs(out_dir)

        settings = parse_settings(settings_file)
        simc_path = settings.get('simc_path')
        confidence = settings.get('confidence')
        batch_size = settings.get('batch_size')
        target_abs = settings.get('target_absolute_error')
        target_rel = settings.get('target_relative_error')

        descriptions = generate_profiles(profile_file, settings_file, out_dir)

        max_id = -1
        for fname in os.listdir(out_dir):
            m = re.match(r'profile_(\d+)\.simc', fname)
            if m:
                max_id = max(max_id, int(m.group(1)))
        if max_id < 0:
            print("No profiles generated.")
            sys.exit(1)

        all_ids = list(range(max_id + 1))
        print(f"Total combos: {len(all_ids)} (including base profile 0)")

        # Initial batches in chunks
        CHUNK_SIZE = 200
        master_list = []

        for chunk_start in range(0, len(all_ids), CHUNK_SIZE):
            chunk_ids = all_ids[chunk_start:chunk_start + CHUNK_SIZE]
            print(f"\n--- Initial batch for chunk {chunk_start//CHUNK_SIZE + 1}: {len(chunk_ids)} combos ---")

            iter_dict = {cid: 100 for cid in chunk_ids}
            batch_file = create_batch_file(
                iter_dict, out_dir, profile_file, opts_file,
                f"batch_initial_chunk_{chunk_start//CHUNK_SIZE + 1}.simc"
            )
            json_file = f"results_initial_chunk_{chunk_start//CHUNK_SIZE + 1}.json"
            raw_results = run_simc_and_parse_results(batch_file, simc_path, json_file)

            result_map = {r['name']: r for r in raw_results}
            for cid in chunk_ids:
                name = f"Combo {cid}"
                if name in result_map:
                    r = result_map[name]
                    mean = r['mean']
                    mean_stddev = r['mean_stddev']
                    iterations = 100
                    stddev = mean_stddev * math.sqrt(iterations)
                    ucb, lcb = compute_ucb_lcb(mean, mean_stddev, confidence)
                    master_list.append({
                        'id': cid,
                        'mean': mean,
                        'stddev': stddev,
                        'mean_stddev': mean_stddev,
                        'iterations': iterations,
                        'ucb': ucb,
                        'lcb': lcb
                    })
                else:
                    print(f"Warning: no result for {name}")

        loop_count = 0
        # Save checkpoint after initial chunks
        save_checkpoint(master_list, loop_count, descriptions)

    # ========== MAIN LOOP ==========
    while True:
        max_mean = max(u['mean'] for u in master_list)
        remaining = [u for u in master_list if u['ucb'] >= max_mean]
        survivor_ids = {u['id'] for u in remaining}

        if not remaining:
            print("All combos dominated. Stopping.")
            break

        allocations = allocate_iterations(remaining, batch_size, target_abs, target_rel)

        if not allocations:
            print("No additional iterations allocated (all remaining combos precise).")
            break

        print_progress(loop_count, master_list, remaining, target_abs, target_rel,
                       session_start_time, session_batch_count)

        iter_dict = {cid: iters for cid, iters in allocations}
        batch_file = create_batch_file(iter_dict, out_dir, profile_file, opts_file,
                                       f"batch_alloc_{loop_count}.simc")
        raw_results = run_simc_and_parse_results(batch_file, simc_path, f"results_alloc_{loop_count}.json")
        result_map = {r['name']: r for r in raw_results}

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
        session_batch_count += 1   # count this batch in the current session

        save_checkpoint(master_list, loop_count, descriptions)

    # ========== FINAL RESULTS ==========
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

    print("\n=== Final Survivors ===")
    final_results.sort(key=lambda x: x['id'])
    for r in final_results:
        print(f"Combo {r['id']}: mean={r['mean']:.2f}, std={r['stddev']:.2f}, "
              f"iter={r['iterations']}, UCB={r['ucb']:.2f}, LCB={r['lcb']:.2f}")
        print(f"  Changes: {r['changes']}\n")

    delete_checkpoint()