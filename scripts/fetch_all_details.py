import json
import os
import urllib.request
import time
import re
import unicodedata
import sys

# Paths
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, 'data')
METADATA_FILE = os.path.join(DATA_DIR, 'poeninja_metadata.json')

# Active leagues configuration
DEFAULT_LEAGUES = ["Runes of Aldur", "HC Runes of Aldur", "Standard", "Hardcore"]

# Global list to track failed details fetches for manual checking
FAILED_FETCHES = []

def load_metadata():
    """Load metadata from the JSON file."""
    if not os.path.exists(METADATA_FILE):
        print(f"[Error] Metadata file not found at {METADATA_FILE}")
        return []
    with open(METADATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_details_id(name):
    """Convert item name to poe.ninja details ID (e.g. 'Chaos Orb' -> 'chaos-orb')"""
    name = unicodedata.normalize('NFKD', name)
    name = "".join([c for c in name if not unicodedata.combining(c)])
    
    name = name.lower()
    name = name.replace("'", "") # Remove apostrophes e.g. Glassblower's
    
    name = re.sub(r'[^a-z0-9:]+', '-', name)
    name = re.sub(r'-+', '-', name)
    return name.strip('-')

def is_exchange_category(category_name):
    """
    Determine if a category uses Poe.ninja's exchange details API.
    """
    if (category_name.startswith('Unique') or 
        category_name.startswith('Precursor') or 
        category_name.startswith('Map')):
        return False
    return True

def fetch_details(league, category, name, details_id):
    """Fetch detail data from Poe.ninja API for exchange categories."""
    league_param = league.replace(' ', '+')
    url = f'https://poe.ninja/poe2/api/economy/exchange/current/details?league={league_param}&type={category}&id={details_id}'
    
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (PoETradeDashboard)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status == 200:
                return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"  [!] Failed to fetch {details_id}: {e}")
        FAILED_FETCHES.append({
            'category': category,
            'name': name,
            'details_id': details_id,
            'url': url,
            'error': str(e)
        })
    return None

def save_category_details(category, data, league_slug):
    """Save details dictionary to a JSON file named after the category and league."""
    category_slug = re.sub(r'(?<!^)(?=[A-Z])', '_', category).lower()
    file_name = f"{category_slug}_details_{league_slug}.json"
    out_file = os.path.join(DATA_DIR, file_name)
    
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    print(f"  [+] Saved {len(data)} items to {file_name}")

def process_category(league, category, items, league_slug):
    """Process all items for a single category."""
    if not is_exchange_category(category):
        print(f"\n[*] Category '{category}' is a Stash category. Skipping details fetch (not supported by Poe.ninja details API).")
        return
        
    print(f"\n[*] Processing Category: {category} ({len(items)} items) in {league}...")
    category_details = {}
    
    for i, item in enumerate(items):
        item_id = item['id']
        name = item['name']
        details_id = get_details_id(name)
        
        print(f"  [{i+1}/{len(items)}] Fetching details for {name} ({details_id})...")
        data = fetch_details(league, category, name, details_id)
        
        if data:
            category_details[item_id] = data
            
        time.sleep(0.2)
        
    if category_details:
        save_category_details(category, category_details, league_slug)
    else:
        print(f"  [-] No details fetched for category {category}")

def run_for_league(league):
    print(f"\n==============================================")
    print(f"Running details fetcher for league: {league}")
    print(f"==============================================")
    
    metadata = load_metadata()
    if not metadata:
        return

    category_groups = {}
    for item in metadata:
        cat = item.get('category', 'Other')
        if cat not in category_groups:
            category_groups[cat] = []
        category_groups[cat].append(item)

    print(f"Discovered {len(category_groups)} categories in metadata.")
    league_slug = re.sub(r'[^a-zA-Z0-9]+', '_', league).strip('_').lower()

    for category, items in category_groups.items():
        process_category(league, category, items, league_slug)

def main():
    if len(sys.argv) > 1:
        leagues = [sys.argv[1]]
    else:
        leagues = DEFAULT_LEAGUES

    for league in leagues:
        run_for_league(league)

    # Save failed fetches list if there are any
    failed_log_file = os.path.join(DATA_DIR, 'failed_fetches.json')
    if FAILED_FETCHES:
        with open(failed_log_file, 'w', encoding='utf-8') as f:
            json.dump(FAILED_FETCHES, f, indent=2)
        print(f"\n[!] Wrote {len(FAILED_FETCHES)} failed item fetches to {failed_log_file} for manual verification.")
    else:
        if os.path.exists(failed_log_file):
            try:
                os.remove(failed_log_file)
            except _: pass
        print(f"\n[+] Success! No details fetches failed.")

if __name__ == "__main__":
    main()
