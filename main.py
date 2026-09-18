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
from functools import lru_cache

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
EXCLUDED_STATS = {
    "stamina",
    "spirit",
    "health",
    "mana",
    "mana regen",
    "armor",
    "run speed",
    "rune",
    "runic power",
    "tank-dodge",
    "tank-parry",
    "tank-block",
    "tank-crit",
    "leech"
}

from collections import Counter, defaultdict  # add Counter to imports

@lru_cache(maxsize=None)
def _split_gems(item_str):
    for part in item_str.split(','):
        if part.startswith('gem_id='):
            return tuple(part[7:].split('/'))   # tuple, not list
    return ()

@lru_cache(maxsize=None)
def _get_enchant_id(item_str):
    """Return the enchant id (as a string) from an item string, or None."""
    for part in item_str.split(','):
        if part.startswith('enchant_id='):
            return part[11:]
    return None

def _prune_slot_items(items, slot, simc_path):
    """
    Drop items that are strictly dominated by another item in the same slot.

    Two items are only comparable when they share:
      - the same effect_identifier (from the item string), and
      - the same socket count.

    Otherwise the extra sockets / effect would make the comparison invalid.
    """
    if not items:
        return items

    groups = defaultdict(list)
    for item in items:
        item_str = format_item_string(item)
        stats, eff = get_item_stats_and_effect(slot, item_str, simc_path)
        groups[(eff, get_socket_count(item))].append((stats, item))

    kept = []
    for _, group in groups.items():
        if len(group) <= 1:
            kept.extend(it for _, it in group)
            continue
        # Collect varying stat names for this group.
        varying = set()
        for stats, _ in group:
            varying.update(stats.keys())
        stat_names = sorted(
            n for n in varying
            if min(s.get(n, 0) for s, _ in group) !=
               max(s.get(n, 0) for s, _ in group)
        )
        if not stat_names:
            # All identical — keep one.
            kept.append(group[0][1])
            continue

        vecs = []
        for stats, item in group:
            vec = tuple(stats.get(n, 0) for n in stat_names)
            vecs.append((vec, item))

        for i, (vi, item_i) in enumerate(vecs):
            dominated = False
            for j, (vj, item_j) in enumerate(vecs):
                if i == j:
                    continue
                # vj dominates vi if it is >= in every varying stat and
                # strictly greater in at least one.
                if all(a >= b for a, b in zip(vj, vi)) and vj != vi:
                    dominated = True
                    break
            if not dominated:
                kept.append(item_i)
    return kept


def precompute_lookup_tables(profile_data, simc_path, gems_settings, enchants_settings):
    """
    Build flat lookup dicts so the per-combo loop is pure dict lookups.

    Returns:
        item_stats:  slot -> item_str -> (Counter(stats), effect_id)
        gem_stats:   gem_id -> (Counter(stats), effect_id)
        ench_stats:  (slot, enchant_id_str) -> (Counter(stats), effect_id)
    """
    item_stats = defaultdict(dict)
    gem_stats = {}
    ench_stats = {}

    # Items (use the pruned pool so we don't pay SimC cost for losers).
    for slot in SLOTS:
        raw = collect_items_for_slot(slot, profile_data)
        kept = _prune_slot_items(raw, slot, simc_path)
        for item in kept:
            s = format_item_string(item)
            stats, eff = get_item_stats_and_effect(slot, s, simc_path)
            item_stats[slot][normalize_item_string(s)] = (Counter(stats), eff)

    # Enchants.
    for slot, specs in enchants_settings.items():
        for spec in specs:
            eid = spec["id"]
            stats, eff = get_enchant_stats_and_effect(
                eid, slot, simc_path, spec.get("effect_identifier", "")
            )
            ench_stats[(slot, str(eid))] = (Counter(stats), eff)

    # Gems.
    for gid, spec in gems_settings.items():
        stats, eff = get_gem_stats_and_effect(
            gid, simc_path, spec.get("effect_identifier", "")
        )
        gem_stats[gid] = (Counter(stats), eff)

    return item_stats, gem_stats, ench_stats


@lru_cache(maxsize=None)
def _placements_for_signature(sig, gems_key):
    """Cached gem placements keyed on the socket signature + gem settings."""
    sockets = list(sig)
    if not sockets:
        return ({},)

    meta_sockets = [s for s in sockets if s[2]]
    non_meta_sockets = [s for s in sockets if not s[2]]
    total_meta_sockets = len(meta_sockets)
    total_non_meta_sockets = len(non_meta_sockets)

    gem_ids = [g for g, *_ in gems_key]
    if not gem_ids:
        return ({},)
    gems_settings = {g: {"min": mn, "max": mx, "meta": meta, "slots": list(sl)}
                     for g, mn, mx, meta, sl in gems_key}

    vectors = []
    def dfs(gem_idx, remaining, counts, meta_used):
        if gem_idx == len(gem_ids):
            if remaining == 0:
                vectors.append(counts.copy())
            return
        gid = gem_ids[gem_idx]
        spec = gems_settings[gid]
        min_c = spec['min']
        max_c = spec['max']
        if max_c == -1:
            max_c = remaining
        else:
            max_c = min(max_c, remaining)
        if spec['meta']:
            max_c = min(max_c, 1)
            if meta_used:
                max_c = 0
        for cnt in range(min_c, max_c + 1):
            counts[gid] = cnt
            dfs(gem_idx + 1, remaining - cnt, counts,
                meta_used or (spec['meta'] and cnt > 0))
        counts.pop(gid, None)

    dfs(0, len(sockets), {}, False)

    out = []
    for vec in vectors:
        total_meta = sum(c for g, c in vec.items() if gems_settings[g]['meta'])
        total_non_meta = sum(c for g, c in vec.items() if not gems_settings[g]['meta'])
        if total_meta > total_meta_sockets or total_non_meta > total_non_meta_sockets:
            continue

        items = [(g, c) for g, c in vec.items() if c > 0]
        items.sort(key=lambda x: (
            -(1 if gems_settings[x[0]]['meta'] else 0),
            -len(gems_settings[x[0]]['slots'])
        ))

        meta_pool = meta_sockets[:]
        non_meta_pool = non_meta_sockets[:]
        placement = {}
        for gid, count in items:
            spec = gems_settings[gid]
            is_meta = spec['meta']
            pref = ['head'] if is_meta else spec['slots']
            pool = meta_pool if is_meta else non_meta_pool
            placed = 0
            for entry in pool[:]:
                if placed >= count:
                    break
                if entry[0] in pref:
                    placement[(entry[0], entry[1])] = gid
                    pool.remove(entry)
                    placed += 1
            if placed < count:
                for entry in pool[:]:
                    if placed >= count:
                        break
                    placement[(entry[0], entry[1])] = gid
                    pool.remove(entry)
                    placed += 1
            if placed < count:
                break
        else:
            out.append(placement)
    return tuple(out)

def _build_effect_key(effect_ids):
    """
    Build a hashable key from a list of effect identifiers.

    Fast path: if no effect id starts with '_', we just need a frozenset
    (duplicates are collapsed automatically).
    Slow path: '_'-prefixed ids keep their occurrence count.
    """
    non_empty = [eff for eff in effect_ids if eff]
    if not non_empty:
        return frozenset()

    for eff in non_empty:
        if eff.startswith('_'):
            break
    else:
        # No '_'-prefixed effect at all
        return frozenset(non_empty)

    counts = Counter(non_empty)
    normalized = set()
    for eff, cnt in counts.items():
        if eff.startswith('_'):
            normalized.add((eff, cnt))
        else:
            normalized.add((eff, 1))
    return frozenset(normalized)

def _parse_stats_from_html(html_path):
    """
    Parse the '<div class="player-section stats">...<table>...</table>' section
    from a SimC HTML dump and return a {stat_name: value} dict.
    """
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return {}

    stats = {}
    stats_section_match = re.search(
        r'<div class="player-section stats">.*?<table.*?>(.*?)</table>',
        html_content,
        re.DOTALL
    )
    if not stats_section_match:
        return {}

    table_content = stats_section_match.group(1)
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL)
    for row_match in row_pattern.finditer(table_content):
        row_html = row_match.group(1)
        th_match = re.search(r'<th class="left">([^<]+)</th>', row_html)
        if not th_match:
            continue
        stat_name = th_match.group(1).strip()

        if stat_name.lower() in EXCLUDED_STATS:
            continue

        td_matches = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
        if not td_matches:
            continue
        last_val = td_matches[-1].strip()
        last_val = re.sub(r'<[^>]+>', '', last_val)
        last_val = last_val.replace('%', '').replace('(', '').replace(')', '').strip()
        num_match = re.search(r'(-?\d+(?:\.\d+)?)', last_val)
        if num_match:
            val = float(num_match.group(1))
            if val.is_integer():
                val = int(val)
            stats[stat_name] = val
        else:
            stats[stat_name] = 0
    return stats

def _parse_weapon_from_html(html_path):
    """
    Parse a weapon line like:
      weapon: { 10 - 14, 3.6 }
    and return (min_damage, max_damage, speed) as floats.
    Returns None if no weapon block is found.
    """
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return None

    m = re.search(
        r'weapon:\s*\{\s*([\d.]+)\s*-\s*([\d.]+)\s*,\s*([\d.]+)\s*\}',
        html_content
    )
    if not m:
        return None

    return float(m.group(1)), float(m.group(2)), float(m.group(3))

@lru_cache(maxsize=None)
def normalize_item_string(item_str):
    """
    Remove gem_id and enchant_id fields from an item string.
    These are overridden by the SimC call anyway, so they should not
    affect the cached result.
    """
    parts = item_str.split(',')
    filtered = [parts[0]]  # base item
    for part in parts[1:]:
        if part.startswith('gem_id=') or part.startswith('enchant_id='):
            continue
        filtered.append(part)
    return ','.join(filtered)

@lru_cache(maxsize=None)
def _get_item_stats_and_effect_cached(slot, item_str, simc_path):
    """
    Cached version. `item_str` must be normalized (no gem_id/enchant_id).
    Runs SimC and extracts gear stats.
    Returns (stats_dict, effect_id) where effect_id is the value of
    effect_identifier= in the item string, or an empty string if absent.
    """
    # Extract effect_identifier from the item string (if present)
    effect_id = ""
    for part in item_str.split(','):
        part = part.strip()
        if part.startswith('effect_identifier='):
            effect_id = part.split('=', 1)[1].strip()
            break

    # Map internal slot names to the ones used in the .simc template
    slot_map = {
        "shoulder": "shoulders",
        "2_hand": "main_hand",
        "1_hand": "main_hand"
    }
    template_slot = slot_map.get(slot, slot)

    # Build the .simc content (fixed enchant and gems are appended)
    lines = [
        "input=profile.simc",
        "head=,",
        "neck=,",
        "shoulders=,",
        "chest=,",
        "waist=,",
        "legs=,",
        "feet=,",
        "wrists=,",
        "hands=,",
        "finger1=,",
        "finger2=,",
        "trinket1=,",
        "trinket2=,",
        "back=,",
        "main_hand=,",
        "off_hand=,",
        f"{template_slot}={item_str},enchant_id=2503,gem_id=32198/32198/32198/32198",
        "max_time=1",
        "html=gear_test.html",
        "report_details=0",
        "calculate_scale_factors=0"
    ]
    content = "\n".join(lines) + "\n"

    with open("gear_test.simc", "w", encoding="utf-8") as f:
        f.write(content)

    # Run SimC
    cmd = [simc_path, "gear_test.simc"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"SimC error (stderr):\n{e.stderr}")
        return {}, effect_id

    stats = _parse_stats_from_html("gear_test.html")

    # --- weapon damage / speed handling ---
    weapon_info = _parse_weapon_from_html("gear_test.html")
    if weapon_info:
        min_dmg, max_dmg, speed = weapon_info
        avg_damage = (min_dmg + max_dmg) / 2.0
        if speed > 0:
            dps = avg_damage / speed
            stats["DPS"] = stats.get("DPS", 0.0) + dps

        # Treat speed as an effect so different weapon speeds are not merged
        speed_str = f"speed={speed}"
        if effect_id:
            effect_id = f"{effect_id}|{speed_str}"
        else:
            effect_id = speed_str

    print(slot, stats, effect_id)
    return stats, effect_id

def get_item_stats_and_effect(slot, item_str, simc_path):
    """
    Public wrapper that normalizes the item string before calling the cached function.
    """
    normalized = normalize_item_string(item_str)
    return _get_item_stats_and_effect_cached(slot, normalized, simc_path)

@lru_cache(maxsize=None)
def get_gem_stats_and_effect(gem_id, simc_path, effect_identifier=""):
    """
    Given a gem ID (string), return (stats_dict, effect_id).

    Runs SimC with a dummy head item (id=8754, ilevel=1) socketed with the gem.
    The dummy item contributes 0 stats, so what we parse is the
    gem's contribution. The effect_id is supplied by the caller (from
    settings.json -> gems[id].effect_identifier) and returned unchanged.
    """
    if not gem_id:
        return {}, effect_identifier

    lines = [
        "input=profile.simc",
        "head=,",
        "neck=,",
        "shoulders=,",
        "chest=,",
        "waist=,",
        "legs=,",
        "feet=,",
        "wrists=,",
        "hands=,",
        "finger1=,",
        "finger2=,",
        "trinket1=,",
        "trinket2=,",
        "back=,",
        "main_hand=,",
        "off_hand=,",
        f"head=,id=8754,ilevel=1,gem_id={gem_id}",
        "max_time=1",
        "html=gem_test.html"
    ]
    content = "\n".join(lines) + "\n"

    with open("gem_test.simc", "w", encoding="utf-8") as f:
        f.write(content)

    cmd = [simc_path, "gem_test.simc"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"SimC error (stderr):\n{e.stderr}")
        return {}, effect_identifier

    stats = _parse_stats_from_html("gem_test.html")
    print("gem", gem_id, stats)
    return stats, effect_identifier
# ------------------------------------------------------------

@lru_cache(maxsize=None)
def get_enchant_stats_and_effect(enchant_id, slot, simc_path, effect_identifier=""):
    """
    Given an enchant ID and the slot it's applied to, return (stats_dict, effect_id).

    Runs SimC with the same helm dummy item used for gem testing, with the
    enchant applied to the given slot. The dummy item contributes ~0 stats,
    so what we parse is essentially the enchant's contribution. The effect_id
    is supplied by the caller (from settings.json -> enchants[slot][*].effect_identifier)
    and returned unchanged.
    """
    if not enchant_id:
        return {}, effect_identifier

    slot_map = {
        "shoulder": "shoulders",
    }
    template_slot = slot_map.get(slot, slot)

    # Always use the same helm dummy item as the gem test, so the enchant's
    # contribution dominates and the baseline is consistent across slots.
    dummy_item = "id=8754,ilevel=1"

    lines = [
        "input=profile.simc",
        "head=,",
        "neck=,",
        "shoulders=,",
        "chest=,",
        "waist=,",
        "legs=,",
        "feet=,",
        "wrists=,",
        "hands=,",
        "finger1=,",
        "finger2=,",
        "trinket1=,",
        "trinket2=,",
        "back=,",
        "main_hand=,",
        "off_hand=,",
        f"{template_slot}=,{dummy_item},enchant_id={enchant_id}",
        "max_time=1",
        "html=enchant_test.html",
    ]
    content = "\n".join(lines) + "\n"

    with open("enchant_test.simc", "w", encoding="utf-8") as f:
        f.write(content)

    cmd = [simc_path, "enchant_test.simc"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"SimC error (stderr):\n{e.stderr}")
        return {}, effect_identifier

    stats = _parse_stats_from_html("enchant_test.html")
    print("enchant", enchant_id, slot, stats)
    return stats, effect_identifier

def compute_combo_stats_and_effect(slot_items, item_stats, gem_stats, ench_stats,
                                   extra_effects=None):
    total_stats = Counter()
    effect_ids = []

    for slot, item_str in slot_items.items():
        entry = item_stats.get(slot, {}).get(normalize_item_string(item_str))
        if entry is None:
            continue
        stats, eff = entry
        if stats:
            total_stats.update(stats)
        if eff:
            effect_ids.append(eff)

        for gem_id in _split_gems(item_str):
            if not gem_id:
                continue
            g_entry = gem_stats.get(gem_id)
            if g_entry is None:
                continue
            g_stats, g_eff = g_entry
            if g_stats:
                total_stats.update(g_stats)
            if g_eff:
                effect_ids.append(g_eff)

        ench_id = _get_enchant_id(item_str)
        if ench_id is not None:
            e_entry = ench_stats.get((slot, ench_id))
            if e_entry is not None:
                e_stats, e_eff = e_entry
                if e_stats:
                    total_stats.update(e_stats)
                if e_eff:
                    effect_ids.append(e_eff)

    if extra_effects:
        effect_ids.extend(extra_effects)

    return dict(total_stats), _build_effect_key(effect_ids)

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
    patterns = [
        "batch_*.simc",
        "results_*.json",
        "gear_test.simc",
        "gear_test.html",
        "gem_test.simc",
        "gem_test.html",
        "enchant_test.simc",
        "enchant_test.html",
    ]
    for pat in patterns:
        for f in glob.glob(pat):
            os.remove(f)

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
        "options": {},
        "commented_options": {},          # was [] -> dict
        "equipment": {},
        "commented_equipment": {}
    }
    for cls in CLASSES:
        if cls in active_pairs:
            result["player_name"] = active_pairs[cls]
            break
    for slot in SLOTS:
        if slot in active_pairs:
            result["equipment"][slot] = active_pairs[slot]
        if slot in commented_pairs:
            result["commented_equipment"][slot] = commented_pairs[slot]
    for key, value in active_pairs.items():
        if key in SLOTS:
            continue
        result["options"][key] = value          # was result[options] -> NameError
    for key, values in commented_pairs.items():
        if key in SLOTS:
            continue
        result["commented_options"][key] = values
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
            "meta": spec.get("meta", False),
            "effect_identifier": spec.get("effect_identifier", "") or ""
        }
    data["gems"] = processed_gems
    for slot, vals in data["enchants"].items():
        if not isinstance(vals, list):
            raise ValueError(f"Enchant values for '{slot}' must be a list")
        processed = []
        for v in vals:
            if isinstance(v, int):
                processed.append({"id": v, "effect_identifier": ""})
            elif isinstance(v, dict):
                if "id" not in v:
                    raise ValueError(f"Enchant entry for '{slot}' must contain 'id'")
                processed.append({
                    "id": v["id"],
                    "effect_identifier": v.get("effect_identifier", "") or ""
                })
            else:
                raise ValueError(
                    f"Enchant entry for '{slot}' must be int or object, got {type(v).__name__}"
                )
        data["enchants"][slot] = processed
    filter_dominated = data.get("filter_dominated_combinations", False)
    if not isinstance(filter_dominated, bool):
        raise ValueError("filter_dominated_combinations must be true or false")
    data["filter_dominated_combinations"] = filter_dominated
    return data

@lru_cache(maxsize=None)
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

def collect_option_values(key, profile_data):
    """Return all distinct values for an option key (active + commented)."""
    values = []
    if key in profile_data["options"]:
        values.append(profile_data["options"][key])
    if key in profile_data["commented_options"]:
        values.extend(profile_data["commented_options"][key])
    seen = set()
    unique = []
    for v in values:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def generate_option_combinations(profile_data):
    """
    Yield dicts mapping option key -> chosen value for every combination of
    option values that have 2+ alternatives. Options with a single value are
    treated as fixed and omitted from the enumeration.

    Only options that the user actually made swappable (by providing commented
    alternatives) participate.
    """
    all_keys = (set(profile_data["options"].keys())
                | set(profile_data["commented_options"].keys()))
    keys_with_choices = []
    for key in sorted(all_keys):
        values = collect_option_values(key, profile_data)
        if len(values) > 1:
            keys_with_choices.append((key, values))

    if not keys_with_choices:
        yield {}
        return

    keys = [k for k, _ in keys_with_choices]
    value_lists = [vals for _, vals in keys_with_choices]
    for combo in itertools.product(*value_lists):
        yield dict(zip(keys, combo))

def generate_gear_combinations(profile_data):
    normal_slots = [
        s for s in SLOTS
        if s not in ('main_hand', 'off_hand', '1_hand', '2_hand',
                     'finger1', 'finger2', 'trinket1', 'trinket2')
    ]
    normal_options = {}
    for slot in normal_slots:
        opts = collect_items_for_slot(slot, profile_data)
        normal_options[slot] = opts if opts else [None]

    # --- Fingers: pre-build the list of dicts once ---
    finger_pool = (collect_items_for_slot('finger1', profile_data)
                   + collect_items_for_slot('finger2', profile_data))
    finger_dicts = []
    if len(finger_pool) >= 2:
        for i in range(len(finger_pool)):
            a = finger_pool[i]
            for j in range(i + 1, len(finger_pool)):
                b = finger_pool[j]
                if get_item_id(a) == get_item_id(b):
                    continue
                finger_dicts.append({'finger1': a, 'finger2': b})
    else:
        f1 = collect_items_for_slot('finger1', profile_data) or [None]
        f2 = collect_items_for_slot('finger2', profile_data) or [None]
        for a in f1:
            for b in f2:
                if a is None or b is None:
                    continue
                if get_item_id(a) == get_item_id(b):
                    continue
                finger_dicts.append({'finger1': a, 'finger2': b})

    # --- Trinkets: same treatment ---
    trinket_pool = (collect_items_for_slot('trinket1', profile_data)
                    + collect_items_for_slot('trinket2', profile_data))
    trinket_dicts = []
    if len(trinket_pool) >= 2:
        for i in range(len(trinket_pool)):
            a = trinket_pool[i]
            for j in range(i + 1, len(trinket_pool)):
                b = trinket_pool[j]
                if get_item_id(a) == get_item_id(b):
                    continue
                trinket_dicts.append({'trinket1': a, 'trinket2': b})
    else:
        t1 = collect_items_for_slot('trinket1', profile_data) or [None]
        t2 = collect_items_for_slot('trinket2', profile_data) or [None]
        for a in t1:
            for b in t2:
                if a is None or b is None:
                    continue
                if get_item_id(a) == get_item_id(b):
                    continue
                trinket_dicts.append({'trinket1': a, 'trinket2': b})

    # --- Weapons: pre-build dicts, already free of None values ---
    two_hand_pool = collect_items_for_slot('2_hand', profile_data)
    main_pool = (collect_items_for_slot('main_hand', profile_data)
                 + collect_items_for_slot('1_hand', profile_data))
    off_pool = (collect_items_for_slot('off_hand', profile_data)
                + collect_items_for_slot('1_hand', profile_data))

    weapon_dicts = []
    for item in two_hand_pool:
        if item is not None:
            weapon_dicts.append({'main_hand': item})
    for m in main_pool:
        for o in off_pool:
            if m is None or o is None:
                continue
            if get_item_id(m) == get_item_id(o):
                continue
            weapon_dicts.append({'main_hand': m, 'off_hand': o})
    if not weapon_dicts:
        weapon_dicts.append({})

    # --- Main loop: dict merge instead of update() ---
    normal_product = itertools.product(
        *(normal_options[s] for s in normal_slots)
    )
    extra_combos = []
    for f in finger_dicts:
        for t in trinket_dicts:
            for w in weapon_dicts:
                extra_combos.append({**f, **t, **w})

    for normal_items in normal_product:
        normal_dict = {s: v for s, v in zip(normal_slots, normal_items) if v is not None}
        for extra in extra_combos:
            yield {**normal_dict, **extra}

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
    sig = tuple(sorted(sockets))
    gems_key = tuple(sorted(
        (g, s.get('min', 0), s.get('max', -1), s.get('meta', False),
         tuple(s.get('slots', ())))
        for g, s in gems_settings.items()
    ))
    for placement in _placements_for_signature(sig, gems_key):
        yield placement

def generate_enchant_combinations(gear_combo, enchants_settings):
    slots_with_enchants = [s for s in gear_combo if s in enchants_settings]
    if not slots_with_enchants:
        yield {}
        return
    options_per_slot = {s: [None] + enchants_settings[s] for s in slots_with_enchants}
    for combo in itertools.product(*(options_per_slot[s] for s in slots_with_enchants)):
        yield dict(zip(slots_with_enchants, combo))

def filter_combinations(combos):
    """
    combos: iterable of (stats_dict, effect_id, payload) triples.
    Returns list of payloads (one per surviving non-dominated combo).
    """
    groups = defaultdict(list)
    for stats, effect_id, payload in combos:
        groups[effect_id].append((stats, payload))

    try:
        import numpy as np
    except ImportError:
        np = None

    n_input = sum(len(g) for g in groups.values())
    n_groups = len(groups)
    t0 = time.time()
    print(
        f"Filtering {n_input} unique combinations across {n_groups} effect group(s)...",
        flush=True,
    )

    filtered = []

    for group_i, (_effect_id, group) in enumerate(groups.items(), start=1):
        if not group:
            continue

        n_before_dedup = len(group)
        kept_before = len(filtered)

        # --- 1. Deduplicate by exact stat vector -------------------------
        seen_stats = set()
        deduped = []
        for stats, payload in group:
            # Create a hashable key from the stats dict
            key = tuple(sorted(stats.items()))
            if key in seen_stats:
                continue
            seen_stats.add(key)
            deduped.append((stats, payload))
        group = deduped

        if len(group) == 1:
            filtered.append(group[0][1])
        else:
            # --- 2. Collect all stat names and remove constants ----------
            all_stats = set()
            for stats, _ in group:
                all_stats.update(stats.keys())

            # Determine which stats vary within this group
            varying_stats = []
            for name in all_stats:
                vals = [s.get(name, 0) for s, _ in group]
                if min(vals) != max(vals):
                    varying_stats.append(name)

            # If no varying stats, all combos are identical in stats
            if not varying_stats:
                filtered.append(group[0][1])
            else:
                stat_names = sorted(varying_stats)
                n_stats = len(stat_names)
                n = len(group)

                # --- 3. Build numpy matrix -------------------------------
                if np is not None and n > 32:
                    # Use float32 for speed; stats are usually small integers/floats
                    mat = np.empty((n, n_stats), dtype=np.float32)
                    for i, (s, _) in enumerate(group):
                        row = mat[i]
                        for j, name in enumerate(stat_names):
                            row[j] = s.get(name, 0)

                    # Sort by total sum descending
                    order = np.argsort(-mat.sum(axis=1), kind='stable')

                    keep_arr = np.empty_like(mat)
                    keep_indices = []
                    count = 0

                    # Optional: block candidates to reduce Python loop overhead
                    # Here we keep the simple per-candidate loop; it's already fast.
                    for idx in order:
                        vec = mat[idx]
                        if count:
                            sub = keep_arr[:count]
                            # Check if any kept vector dominates vec
                            if np.any(np.all(sub >= vec, axis=1)):
                                continue
                        keep_arr[count] = vec
                        keep_indices.append(idx)
                        count += 1

                    for idx in keep_indices:
                        filtered.append(group[idx][1])
                else:
                    # --- Pure-Python fallback (unchanged) ----------------
                    entries = []
                    for stats, payload in group:
                        vec = tuple(stats.get(name, 0) for name in stat_names)
                        entries.append((sum(vec), vec, payload))
                    entries.sort(key=lambda e: e[0], reverse=True)

                    keep_vecs = []
                    for _total, vec, payload in entries:
                        dominated = False
                        for fvec in keep_vecs:
                            if all(fvec[k] >= vec[k] for k in range(n_stats)):
                                dominated = True
                                break
                        if not dominated:
                            keep_vecs.append(vec)
                            filtered.append(payload)

        kept = len(filtered) - kept_before
        if n_before_dedup >= 100 or group_i == n_groups or group_i % 10 == 0:
            print(
                f"  Group {group_i}/{n_groups}: {n_before_dedup} combos -> {kept} kept "
                f"({len(filtered)} total, {time.time() - t0:.1f}s)",
                flush=True,
            )

    print(
        f"Filter complete: {len(filtered)}/{n_input} combinations kept "
        f"in {time.time() - t0:.1f}s",
        flush=True,
    )
    return filtered

def generate_profiles(profile_file, settings_file, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    profile_data, active_pairs_orig, _ = parse_profile(profile_file)
    settings_data = parse_settings(settings_file)
    gems_settings = settings_data.get('gems', {})
    enchants_settings = settings_data.get('enchants', {})
    filter_dominated = settings_data.get('filter_dominated_combinations', True)

    base_path = os.path.join(output_dir, "profile_0.simc")
    with open(base_path, 'w', encoding='utf-8') as f:
        f.write("")
        simc_path = settings_data.get('simc_path')

    # ---- Precompute ----
    print("Pruning items and precomputing stat tables...", flush=True)
    pre_t0 = time.time()
    item_stats, gem_stats, ench_stats = precompute_lookup_tables(
        profile_data, simc_path, gems_settings, enchants_settings
    )
    print(
        f"  {sum(len(v) for v in item_stats.values())} items kept across "
        f"{len(item_stats)} slots, {len(gem_stats)} gems, "
        f"{len(ench_stats)} enchants ({time.time() - pre_t0:.1f}s)",
        flush=True,
    )

    # Compact records: (stats_dict, effect_id, changes_dict).
    # We deliberately do NOT keep slot_items around after computing changes.
    combo_list = []
    seen = set()

    compute_stats = compute_combo_stats_and_effect

    print("Generating gear/gem/enchant combinations...", flush=True)
    gen_t0 = time.time()
    considered = 0
    skipped = 0
    last_report = gen_t0
    report_every = 1000 if filter_dominated else 25000

    for gear_combo in generate_gear_combinations(profile_data):
        for option_combo in generate_option_combinations(profile_data):   # NEW
            for gem_placement in generate_gem_placements(gear_combo, gems_settings):
                for enchant_assignment in generate_enchant_combinations(gear_combo, enchants_settings):
                    considered += 1
                    slot_items = {}
                    for slot, item in gear_combo.items():
                        new_item = {
                            "base": item["base"],
                            "fields": item["fields"].copy()
                        }
                        num_sockets = get_socket_count(item)
                        slot_gems = [gem_placement.get((slot, idx))
                                     for idx in range(num_sockets)]
                        if any(g is not None for g in slot_gems):
                            set_gems(new_item, slot_gems)
                        enchant_spec = enchant_assignment.get(slot)
                        if enchant_spec is not None:
                            set_enchant(new_item, enchant_spec["id"])
                        slot_items[slot] = format_item_string(new_item)

                    # --- Compute which option values actually differ from base.
                    option_effects = []
                    option_changes = {}
                    for opt_key, opt_val in option_combo.items():
                        if opt_key not in active_pairs_orig or opt_val != active_pairs_orig[opt_key]:
                            option_effects.append(f"option:{opt_key}={opt_val}")
                            option_changes[opt_key] = opt_val

                    # Dedup key now covers gear AND options.
                    key = (
                        frozenset(slot_items.items()),
                        frozenset(option_combo.items()),
                    )
                    if key in seen:
                        skipped += 1
                        continue
                    seen.add(key)

                    if filter_dominated:
                        stats, effect_id = compute_stats(
                            slot_items, item_stats, gem_stats, ench_stats,
                            extra_effects=option_effects,
                        )
                    else:
                        stats, effect_id = {}, frozenset()

                    changes = {
                        slot: formatted
                        for slot, formatted in slot_items.items()
                        if slot not in active_pairs_orig
                           or formatted != active_pairs_orig[slot]
                    }
                    changes.update(option_changes)

                    combo_list.append((stats, effect_id, changes))

                    now = time.time()
                    unique = len(combo_list)
                    if unique % report_every == 0 or (now - last_report) >= 5.0:
                        print(
                            f"  Combinations: {unique} unique, {skipped} duplicates skipped, "
                            f"{considered} considered ({now - gen_t0:.1f}s)",
                            flush=True,
                        )
                        last_report = now

    print(
        f"Finished combination generation: {len(combo_list)} unique "
        f"({skipped} duplicates skipped, {considered} considered) "
        f"in {time.time() - gen_t0:.1f}s",
        flush=True,
    )

    # Free the dedup set before the (potentially heavier) filter step.
    del seen

    if filter_dominated:
        filtered_changes = filter_combinations(combo_list)
    else:
        filtered_changes = [changes for _, _, changes in combo_list]

    # combo_list is no longer needed.
    del combo_list

    descriptions = {0: "base gear (no changes)"}
    combo_counter = 1
    for new_pairs in filtered_changes:
        if not new_pairs:
            continue
        prefix = f'profileset."Combo {combo_counter}"+='
        lines = [f"{prefix}{k}={v}" for k, v in sorted(new_pairs.items())]
        desc = "; ".join(f"{k}={v}" for k, v in sorted(new_pairs.items()))
        out_path = os.path.join(output_dir, f"profile_{combo_counter}.simc")
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        descriptions[combo_counter] = desc
        combo_counter += 1

    print(f"Generated {combo_counter - 1} filtered profiles + base profile 0 in {output_dir}")
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
    lines.append(f'path=".\\{folder_name}"')

    for combo_id in sorted(iterations_dict.keys()):
        iters = abs(iterations_dict[combo_id])
        lines.append(f"active={player_name}")
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
    factor = 1.0 / (16.0 * (1-confidence) + 0.5) + 1.1
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

#import cProfile, pstats
#cProfile.run("generate_profiles('profile.simc','settings.json','profiles')", "gen.prof")
#pstats.Stats("gen.prof").sort_stats("cumulative").print_stats(30)

if __name__ == "__main__":
    import sys, subprocess, json, os, re, math

    profile_file = "profile.simc"
    settings_file = "settings.json"
    out_dir = "profiles"
    opts_file = "options.simc"

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
        CHUNK_SIZE = 100
        CHUNK_ITERATIONS = 50
        master_list = []

        for chunk_start in range(0, len(all_ids), CHUNK_SIZE):
            chunk_ids = all_ids[chunk_start:chunk_start + CHUNK_SIZE]
            print(f"\n--- Initial batch for chunk {chunk_start//CHUNK_SIZE + 1}: {len(chunk_ids)} combos ---")

            iter_dict = {cid: CHUNK_ITERATIONS for cid in chunk_ids}
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
                    iterations = CHUNK_ITERATIONS
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


    # Session timing: start now, and count batches run in this session
    session_start_time = time.time()
    session_batch_count = 0
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

    print("\n=== Final best profiles ===")
    final_results.sort(key=lambda x: x['id'])
    for r in final_results:
        print(f"Combo {r['id']}: mean={r['mean']:.2f}, std={r['stddev']:.2f}, "
              f"iter={r['iterations']}, UCB={r['ucb']:.2f}, LCB={r['lcb']:.2f}")
        print(f"  Changes: {r['changes']}\n")

        # ---- Save best profiles (full base + changes) ----
    best_dir = "best_profiles"
    if os.path.exists(best_dir):
        shutil.rmtree(best_dir)
        print(f"Removed previous best profiles directory: {best_dir}")
    os.makedirs(best_dir, exist_ok=True)

    # Read the base profile once
    with open(profile_file, 'r', encoding='utf-8') as f:
        base_content = f.read()

    for r in final_results:
        src = os.path.join(out_dir, f"profile_{r['id']}.simc")
        if os.path.exists(src):
            dst = os.path.join(best_dir, f"profile_{r['id']}.simc")
            # Read the changed lines (they may have the prefix)
            with open(src, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            prefix = f'profileset."Combo {r["id"]}"+='
            changed_lines = []
            for line in lines:
                if line.startswith(prefix):
                    changed_lines.append(line[len(prefix):])
                else:
                    # In case there are lines without prefix (shouldn't happen)
                    changed_lines.append(line)
            # Write the base content, then the changed gear lines
            with open(dst, 'w', encoding='utf-8') as f:
                f.write(base_content)
                # Ensure there's a newline between base and changes
                if base_content and not base_content.endswith('\n'):
                    f.write('\n')
                f.write("\n#Changes\n\n")
                f.writelines(changed_lines)
            print(f"Saved profile {r['id']} to {dst}")

    # Clean up original profiles, batch files, and checkpoint
    delete_checkpoint()
    cleanup_previous_runs(out_dir)