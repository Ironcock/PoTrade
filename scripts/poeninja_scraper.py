"""
PoE Trade Extension - Poe Ninja Scraper
=======================================
Fetches pricing data from Poe.Ninja for PoE 2 and builds our own independent 168-hour database.

This script runs hourly via GitHub Actions.
"""

import json
import time
import os
import urllib.request
import urllib.parse
import urllib.error
import re
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_LEAGUE = "Runes of Aldur"
DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
METADATA_FILE = os.path.join(DATA_DIR, 'poeninja_metadata.json')

ENDPOINTS = [
    # (type_name, api_type)
    ('Currency', 'exchange'),
    ('Fragments', 'exchange'),
    ('Abyss', 'exchange'),
    ('UncutGems', 'exchange'),
    ('LineageSupportGems', 'exchange'),
    ('Essences', 'exchange'),
    ('SoulCores', 'exchange'),
    ('Idols', 'exchange'),
    ('Runes', 'exchange'),
    ('Ritual', 'exchange'),
    ('Expedition', 'exchange'),
    ('Delirium', 'exchange'),
    ('Breach', 'exchange'),
    ('Verisium', 'exchange'),
    ('UniqueWeapons', 'stash'),
    ('UniqueArmours', 'stash'),
    ('UniqueAccessories', 'stash'),
    ('UniqueFlasks', 'stash'),
    ('UniqueCharms', 'stash'),
    ('UniqueJewels', 'stash'),
    ('UniqueSanctumRelics', 'stash'),
    ('UniqueTablets', 'stash'),
    ('PrecursorTablets', 'stash')
]

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def fetch_poe_ninja_data(league, item_type, api_type, etag=None):
    # api_type is 'exchange' or 'stash'
    if api_type == 'exchange':
        url = f"https://poe.ninja/poe2/api/economy/exchange/current/overview?league={league.replace(' ', '+')}&type={item_type}"
    else:
        url = f"https://poe.ninja/poe2/api/economy/stash/current/item/overview?league={league.replace(' ', '+')}&type={item_type}"
        
    headers = {'User-Agent': 'Mozilla/5.0'}
    if etag:
        headers['If-None-Match'] = etag
        
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            new_etag = r.info().get('Etag')
            return json.loads(r.read().decode('utf-8')), new_etag
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "304", etag
        print(f"[ERROR] Could not fetch {url}: {e}")
        return None, None
    except Exception as e:
        print(f"[ERROR] Could not fetch {url}: {e}")
        return None, None

def fetch_item_details(league, item_type, details_id):
    url = f"https://poe.ninja/poe2/api/economy/exchange/current/details?league={league.replace(' ', '+')}&type={item_type}&id={details_id}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"[ERROR] Could not fetch item details for {details_id}: {e}")
        return None

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def update_own_db(league=DEFAULT_LEAGUE):
    print(f"\n[PoeNinja Scraper] Starting run at {datetime.now(timezone.utc).isoformat()}")
    print(f"[PoeNinja Scraper] League: {league}")
    
    league_slug = re.sub(r'[^a-zA-Z0-9]+', '_', league).strip('_').lower()
    own_db_file = os.path.join(DATA_DIR, f'priceoverview_own_{league_slug}.json')
    
    # 1. Fetch all data, loading and merging with existing metadata to prevent overwriting between leagues
    all_items = {}
    metadata_map = {}
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'r', encoding='utf-8') as f:
                for item in json.load(f):
                    metadata_map[item['id']] = item
        except Exception as e:
            print(f"[PoeNinja Scraper] Failed to load existing metadata for merging: {e}")
            
    # Load ETag Cache
    etag_cache_file = os.path.join(DATA_DIR, 'etag_cache.json')
    etag_cache = {}
    if os.path.exists(etag_cache_file):
        try:
            with open(etag_cache_file, 'r', encoding='utf-8') as f:
                etag_cache = json.load(f)
        except:
            pass
    if league_slug not in etag_cache:
        etag_cache[league_slug] = {}
    
    for item_type, api_type in ENDPOINTS:
        print(f"[PoeNinja Scraper] Fetching {item_type} ({api_type})...")
        etag = etag_cache[league_slug].get(item_type)
        data, new_etag = fetch_poe_ninja_data(league, item_type, api_type, etag)
        
        if data == "304":
            print(f"[PoeNinja Scraper] {item_type} has not changed (304).")
            continue
            
        if new_etag:
            etag_cache[league_slug][item_type] = new_etag
            
        if data and 'lines' in data:
            for line in data['lines']:
                if api_type == 'exchange':
                    item_id = line.get('id')
                else:
                    item_id = line.get('detailsId') or str(line.get('id'))
                
                if item_id:
                    name = line.get('name') or line.get('currencyTypeName') or item_id
                    icon = line.get('icon') or ''
                    base_type = line.get('baseType') or ''
                    
                    all_items[item_id] = line
                    
                    def clean_mod_text(text):
                        if not text: return ""
                        text = re.sub(r'\[[^\]|]+\|([^\]]+)\]', r'\1', text)
                        text = re.sub(r'\[([^\]|]+)\]', r'\1', text)
                        return text
                    
                    explicit_mods = [clean_mod_text(m['text']) for m in line.get('explicitModifiers', [])] if line.get('explicitModifiers') else []
                    implicit_mods = [clean_mod_text(m['text']) for m in line.get('implicitModifiers', [])] if line.get('implicitModifiers') else []
                    flavour_text = line.get('flavourText') or ''
                    
                    level_req = line.get('levelRequired') or 0
                    if level_req >= 90 or level_req == 0:
                        parsed_level = 0
                        if line.get('requirementModifiers'):
                            for req in line.get('requirementModifiers', []):
                                req_text = req.get('text', '')
                                level_match = re.search(r'Level:\s*\(?(\d+)', req_text)
                                if level_match:
                                    parsed_level = int(level_match.group(1))
                                    break
                        if parsed_level > 0:
                            level_req = parsed_level
                                
                    metadata_map[item_id] = {
                        'id': item_id,
                        'name': name,
                        'baseType': base_type,
                        'icon': icon,
                        'category': item_type,
                        'flavourText': flavour_text,
                        'explicitModifiers': explicit_mods,
                        'implicitModifiers': implicit_mods,
                        'levelRequired': level_req
                    }
                    
            if api_type == 'exchange' and 'items' in data:
                for item_detail in data['items']:
                    det_id = item_detail.get('id')
                    if not det_id:
                        continue
                    
                    if det_id in metadata_map:
                        meta = metadata_map[det_id]
                        if item_detail.get('name'): meta['name'] = item_detail.get('name')
                        if item_detail.get('image'): meta['icon'] = item_detail.get('image')
                        if item_detail.get('icon'): meta['icon'] = item_detail.get('icon')
                    else:
                        name = item_detail.get('name') or det_id
                        icon = item_detail.get('image') or item_detail.get('icon') or ''
                        
                        metadata_map[det_id] = {
                            'id': det_id,
                            'name': name,
                            'baseType': name,
                            'icon': icon,
                            'category': item_type,
                            'flavourText': '',
                            'explicitModifiers': [],
                            'implicitModifiers': [],
                            'levelRequired': 0
                        }

                    # Always add to all_items as a placeholder if not present in lines
                    if det_id not in all_items:
                        name = item_detail.get('name') or det_id
                        icon = item_detail.get('image') or item_detail.get('icon') or ''
                        all_items[det_id] = {
                            'id': det_id,
                            'detailsId': det_id,
                            'name': name,
                            'icon': icon,
                            'primaryValue': None,
                            'volumePrimaryValue': 0
                        }

    metadata_list = list(metadata_map.values())
    print(f"[PoeNinja Scraper] Fetched {len(all_items)} total items for this league. Merged metadata has {len(metadata_list)} items.")

    if not all_items:
        print(f"[PoeNinja Scraper] No updates detected for {league}. Database is up to date.")
        with open(etag_cache_file, 'w', encoding='utf-8') as f:
            json.dump(etag_cache, f, indent=2)
        return False

    # Fetch official PoE Trade API static data to fix broken internal names
    print("[PoeNinja Scraper] Fetching official static data to fix broken names...")
    static_mapping = {}
    try:
        req = urllib.request.Request('https://www.pathofexile.com/api/trade2/data/static', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=10) as r:
            static_data = json.loads(r.read())
            for cat in static_data.get('result', []):
                for entry in cat.get('entries', []):
                    entry_id = entry.get('id')
                    if entry_id:
                        static_mapping[entry_id] = {
                            'name': entry.get('text'),
                            'icon': 'https://web.poecdn.com' + entry.get('image', '') if entry.get('image') else None
                        }
        
        fixed_count = 0
        for meta in metadata_list:
            if meta['id'] in static_mapping:
                if meta['name'] == meta['id'] or meta['name'].islower():
                    meta['name'] = static_mapping[meta['id']]['name']
                    fixed_count += 1
                if static_mapping[meta['id']]['icon'] and (not meta['icon'] or 'gen/image' not in meta['icon']):
                    meta['icon'] = static_mapping[meta['id']]['icon']
        print(f"[PoeNinja Scraper] Fixed {fixed_count} broken names using official static data.")
    except Exception as e:
        print(f"[PoeNinja Scraper] Failed to fetch official static data: {e}")

    # Wiki tooltip HTML is bundled directly with the extension (wiki_tooltips.json).
    # The extension fetches new tooltips on-demand and caches them locally.
    # No wiki API calls needed in the scraper.

    # Write metadata to file (shared)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(metadata_list, f, indent=2)
    print(f"[PoeNinja Scraper] Saved metadata for {len(metadata_list)} items to {METADATA_FILE}")

    # 3. Load or init our own DB
    if os.path.exists(own_db_file):
        with open(own_db_file, 'r', encoding='utf-8') as f:
            own = json.load(f)
    else:
        own = {"timestamps": [], "prices": {}}

    # 2. Find base currency values (chaos, divine, exalted)
    divine_primary = 1.0
    chaos_primary = 0.1013
    exalted_primary = 0.0025
    annul_primary = 0.075

    if 'divine' in all_items:
        divine_primary = all_items['divine'].get('primaryValue', 1.0)

    if 'chaos' in all_items:
        chaos_primary = all_items['chaos'].get('primaryValue', 0.1013)
    elif own and 'prices' in own and 'chaos' in own['prices'] and 'divine' in own['prices']['chaos']:
        history = own['prices']['chaos']['divine']
        if history:
            chaos_primary = history[-1][1]

    if 'exalted' in all_items:
        exalted_primary = all_items['exalted'].get('primaryValue', 0.0025)
    elif own and 'prices' in own and 'exalted' in own['prices'] and 'divine' in own['prices']['exalted']:
        history = own['prices']['exalted']['divine']
        if history:
            exalted_primary = history[-1][1]

    if 'annul' in all_items:
        annul_primary = all_items['annul'].get('primaryValue', 0.075)
    elif own and 'prices' in own and 'annul' in own['prices'] and 'divine' in own['prices']['annul']:
        history = own['prices']['annul']['divine']
        if history:
            annul_primary = history[-1][1]
            
    print(f"[PoeNinja Scraper] Base Rates:")
    if chaos_primary > 0:
        print(f"  - 1 Divine = {1/chaos_primary:.2f} Chaos")
        print(f"  - 1 Exalted = {exalted_primary/chaos_primary:.2f} Chaos")

    now_ms = int(time.time() * 1000)
    own["timestamps"].append(now_ms)
    time_index = len(own["timestamps"]) - 1

    # 4. Process each item and add to own DB
    for item_id, item_data in all_items.items():
        if item_id not in own["prices"]:
            own["prices"][item_id] = {}

        item_primary = item_data.get('primaryValue')
        if item_primary is None:
            # For placeholder items with no primary value, we fetch their specific details endpoint
            meta = metadata_map.get(item_id)
            if meta:
                category = meta.get('category')
                print(f"[PoeNinja Scraper] Fetching missing details for {item_id} ({category})...")
                details_data = fetch_item_details(league, category, item_id)
                if details_data and 'pairs' in details_data:
                    prices_in_currencies = {}
                    qty = 0
                    for pair in details_data['pairs']:
                        pair_id = pair.get('id')
                        if pair_id in ['chaos', 'divine', 'exalted']:
                            prices_in_currencies[pair_id] = pair.get('rate', 0)
                            if pair_id == 'chaos' or (pair_id == 'divine' and 'chaos' not in prices_in_currencies):
                                qty = pair.get('volumePrimaryValue', 0)
                    
                    for currency, price in prices_in_currencies.items():
                        if item_id == currency:
                            continue
                        if currency not in own["prices"][item_id]:
                            own["prices"][item_id][currency] = []
                        own["prices"][item_id][currency].append([time_index, price, qty])
                time.sleep(0.1) # Sleep to be polite
            continue
            
        qty = item_data.get('volumePrimaryValue') or item_data.get('listingCount') or 0

        prices_in_currencies = {
            'chaos': item_primary / chaos_primary if chaos_primary > 0 else 0,
            'divine': item_primary / divine_primary if divine_primary > 0 else 0,
            'exalted': item_primary / exalted_primary if exalted_primary > 0 else 0
        }

        if annul_primary > 0:
            prices_in_currencies['annul'] = item_primary / annul_primary

        for currency, price in prices_in_currencies.items():
            if item_id == currency:
                continue
                
            if currency not in own["prices"][item_id]:
                own["prices"][item_id][currency] = []
            
            own["prices"][item_id][currency].append([time_index, price, qty])

    # 5. Trim to last 168 hours
    if len(own["timestamps"]) > 168:
        trim = len(own["timestamps"]) - 168
        own["timestamps"] = own["timestamps"][trim:]
        for item_id in own["prices"]:
            for cur in own["prices"][item_id]:
                hist = own["prices"][item_id][cur]
                hist = [[pt[0] - trim, pt[1], pt[2]] for pt in hist if pt[0] >= trim]
                own["prices"][item_id][cur] = hist

    # 6. Save
    with open(own_db_file, 'w', encoding='utf-8') as f:
        json.dump(own, f, separators=(',', ':'))

    own_pts = len(own["timestamps"])
    print(f"[PoeNinja Scraper] Snapshot #{own_pts}/168 saved to {os.path.basename(own_db_file)}. {168 - own_pts} more runs until fully independent.")

    # Save ETag Cache
    with open(etag_cache_file, 'w', encoding='utf-8') as f:
        json.dump(etag_cache, f, indent=2)

    return True

if __name__ == "__main__":
    import sys
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop', action='store_true', help='(Legacy) Run scraper in a loop for 25 minutes. The workflow now handles looping via bash.')
    parser.add_argument('league', nargs='?', help='Specify a single league to scrape')
    args = parser.parse_args()
    
    if args.league:
        leagues = [args.league]
    else:
        leagues = ["Runes of Aldur", "HC Runes of Aldur", "Standard", "Hardcore"]

    # Default: run a single tick across all leagues and exit.
    # The workflow's bash loop calls this script every 5 minutes and handles git pushes.
    if args.loop:
        print("[PoeNinja Scraper] (Legacy loop mode) Starting looping worker mode (runs for 25 minutes)...")
        start_time = time.time()
        run_duration = 1500  # 25 minutes
        tick_interval = 300  # check every 5 minutes
        
        while time.time() - start_time < run_duration:
            print(f"\n--- Scraper Tick: {datetime.now(timezone.utc).isoformat()} ---")
            for l in leagues:
                update_own_db(l)
            
            if time.time() - start_time + tick_interval < run_duration:
                print(f"[PoeNinja Scraper] Sleeping {tick_interval}s until next check...")
                time.sleep(tick_interval)
            else:
                break
        print("[PoeNinja Scraper] Looping worker finished lifecycle gracefully.")
    else:
        # Single tick mode — called by the bash loop in scraper.yml
        print(f"[PoeNinja Scraper] Starting single tick at {datetime.now(timezone.utc).isoformat()}")
        for l in leagues:
            update_own_db(l)
        print(f"[PoeNinja Scraper] Single tick complete.")
