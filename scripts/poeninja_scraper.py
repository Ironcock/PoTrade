"""
PoE Trade Extension - Poe Ninja Scraper v2
==========================================
Uses per-item exchange API with ETag caching for exchange items.
Uses category overview + ETag for stash items (unique weapons, etc).

Per-item data stored as:
  [unix_timestamp_ms, real_market_rate, real_trade_volume]

Exits with code 0 if any prices changed (caller should git push).
Exits with code 1 if nothing changed (caller should skip push).
"""

import json, time, os, re, sys, unicodedata
import urllib.request, urllib.error
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
DEFAULT_LEAGUE = "Runes of Aldur"
DATA_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
METADATA_FILE  = os.path.join(DATA_DIR, 'poeninja_metadata.json')

EXCHANGE_TYPES = [
    'Currency', 'Fragments', 'Abyss', 'UncutGems', 'LineageSupportGems',
    'Essences', 'SoulCores', 'Idols', 'Runes', 'Ritual', 'Expedition',
    'Delirium', 'Breach', 'Verisium'
]

STASH_TYPES = [
    'UniqueWeapons', 'UniqueArmours', 'UniqueAccessories', 'UniqueFlasks',
    'UniqueCharms', 'UniqueJewels', 'UniqueSanctumRelics', 'UniqueTablets',
    'PrecursorTablets'
]

PAIR_CURRENCIES = {'chaos', 'divine', 'exalted'}
MAX_HISTORY_MS  = 7 * 24 * 60 * 60 * 1000   # keep 7 days of price history
UA              = {'User-Agent': 'Mozilla/5.0 (PoETradeDashboard/2.0)'}

# ── HTTP ──────────────────────────────────────────────────────────────────────
def http_get(url, etag=None):
    headers = dict(UA)
    if etag:
        headers['If-None-Match'] = etag
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(2):   # retry once on 429
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                new_etag = r.info().get('ETag') or r.info().get('Etag')
                return r.status, new_etag, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, etag, None
            if e.code == 429 and attempt == 0:
                print(f"  [RATE LIMIT] 429 received — waiting 20s before retry...")
                time.sleep(20)
                continue
            print(f"  [HTTP {e.code}] {url}")
            return e.code, None, None
        except Exception as e:
            print(f"  [NET] {url}: {e}")
            return None, None, None
    return None, None, None

def fetch_exchange_overview(league, item_type, etag=None):
    url = f"https://poe.ninja/poe2/api/economy/exchange/current/overview?league={league.replace(' ','+')}&type={item_type}"
    return http_get(url, etag)

def fetch_stash_overview(league, item_type, etag=None):
    url = f"https://poe.ninja/poe2/api/economy/stash/current/item/overview?league={league.replace(' ','+')}&type={item_type}"
    return http_get(url, etag)

def fetch_item_details(league, item_type, details_id, etag=None):
    url = f"https://poe.ninja/poe2/api/economy/exchange/current/details?league={league.replace(' ','+')}&type={item_type}&id={details_id}"
    return http_get(url, etag)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def league_slug(league):
    return re.sub(r'[^a-zA-Z0-9]+', '_', league).strip('_').lower()

def get_details_id(name):
    """Convert display name to poe.ninja details ID. e.g. "Chaos Orb" -> "chaos-orb"."""
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(c for c in name if not unicodedata.combining(c))
    name = name.lower().replace("'", '')
    name = re.sub(r'[^a-z0-9]+', '-', name)
    return re.sub(r'-+', '-', name).strip('-')

def record_price(history, ts_ms, price, volume):
    """
    Append [ts_ms, price, volume] to history only if the price actually changed.
    If price is unchanged, just update the volume on the last point in place.
    Returns True if a new data point was added (price changed).
    """
    if not price:
        return False
    if history:
        last_price = history[-1][1]
        # Use relative tolerance: 0.01% difference counts as unchanged
        if abs(last_price - price) <= 0.0001 * max(abs(last_price), 1e-9):
            history[-1][2] = volume   # update volume in-place
            return False
    history.append([ts_ms, round(price, 6), round(volume, 4)])
    return True

def trim_history(prices_dict, cutoff_ms):
    """Remove price history older than cutoff_ms."""
    for item_id in list(prices_dict):
        for cur in list(prices_dict[item_id]):
            prices_dict[item_id][cur] = [
                pt for pt in prices_dict[item_id][cur] if pt[0] >= cutoff_ms
            ]
        # Drop empty currency entries
        prices_dict[item_id] = {k: v for k, v in prices_dict[item_id].items() if v}

DAY_MS = 24 * 60 * 60 * 1000

def seed_sparkline_history(prices_dict, iid, cur_id, current_price, sparkline, volume, now_ms):
    """
    Bootstrap 7-day daily history from poe.ninja's built-in sparkLine data.
    sparkLine.data is a list of % changes from the reference (day 0) price:
      data[0] = 0.0  (baseline, earliest day)
      data[N] = totalChange  (latest day, approx. current price level)
    current_price = the real price right now (used to anchor the scale).

    Only seeds when the item has no existing v2-format history, so real
    accumulated data is never overwritten.
    """
    if not sparkline or not current_price:
        return

    data         = sparkline.get('data') or []
    total_change = sparkline.get('totalChange')

    if not data or total_change is None or len(data) < 2:
        return

    # Don't seed if the item already has real v2 data (unix ms timestamps > 1e12)
    existing = prices_dict.get(iid, {}).get(cur_id, [])
    if existing and existing[0][0] > 1_000_000_000_000:
        return

    # Reconstruct absolute prices:
    #   current_price = ref_price * (1 + total_change / 100)
    #   price_at_day_i = ref_price * (1 + data[i] / 100)
    if abs(total_change + 100) < 0.001:
        return   # avoid division by zero (-100% change)
    ref_price = current_price / (1 + total_change / 100)
    if ref_price <= 0:
        return

    prices_dict.setdefault(iid, {})
    prices_dict[iid].setdefault(cur_id, [])

    n = len(data)
    seeded = []
    for i, pct in enumerate(data):
        if pct is None:
            continue
        try:
            price = ref_price * (1 + pct / 100)
        except (TypeError, ZeroDivisionError):
            continue
        if price <= 0:
            continue
        # data[0] = n days ago, data[n-1] = 1 day ago (yesterday)
        days_ago = n - i
        ts = now_ms - days_ago * DAY_MS
        seeded.append([ts, round(price, 6), round(volume, 4)])

    if seeded:
        # Prepend seeded points before any existing (legacy-format) data
        prices_dict[iid][cur_id] = seeded + prices_dict[iid][cur_id]


def seed_pair_history(prices_dict, iid, cur_id, pair_history, now_ms):
    """
    Seed a currency pair's history from the daily `history` array returned by
    the per-item details endpoint.  Each entry is:
        {timestamp: "2026-07-04T00:00:00Z", rate: 7.43, volumePrimaryValue: 12345}
    Only seeds when the item has no existing v2-format history.
    """
    if not pair_history:
        return

    # Skip if already seeded or has real v2 data
    existing = prices_dict.get(iid, {}).get(cur_id, [])
    if existing and existing[0][0] > 1_000_000_000_000:
        return

    prices_dict.setdefault(iid, {})
    prices_dict[iid].setdefault(cur_id, [])

    seeded = []
    for entry in pair_history:
        ts_str = entry.get('timestamp', '')
        rate   = entry.get('rate') or 0
        vol    = entry.get('volumePrimaryValue') or 0
        if not ts_str or not rate:
            continue
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            ts_ms = int(dt.timestamp() * 1000)
        except Exception:
            continue
        seeded.append([ts_ms, round(rate, 6), round(vol, 4)])

    if seeded:
        seeded.sort(key=lambda x: x[0])   # ensure chronological order
        prices_dict[iid][cur_id] = seeded + prices_dict[iid][cur_id]


# ── MAIN ──────────────────────────────────────────────────────────────────────
def update_own_db(league=DEFAULT_LEAGUE):
    ts_start = datetime.now(timezone.utc)
    print(f"\n[Scraper v2] {ts_start.strftime('%Y-%m-%d %H:%M:%S UTC')}  League: {league}")

    slug          = league_slug(league)
    own_db_file   = os.path.join(DATA_DIR, f'priceoverview_own_{slug}.json')
    etag_file     = os.path.join(DATA_DIR, 'etag_cache.json')

    # ── Load existing DB ──────────────────────────────────────────────────────
    own = {"prices": {}}
    if os.path.exists(own_db_file):
        try:
            with open(own_db_file, 'r', encoding='utf-8') as f:
                own = json.load(f)
        except Exception as e:
            print(f"  [WARN] Could not load DB: {e}. Starting fresh.")
    own.setdefault("prices", {})

    # ── Load ETag cache ───────────────────────────────────────────────────────
    etag_cache = {}
    if os.path.exists(etag_file):
        try:
            with open(etag_file, 'r', encoding='utf-8') as f:
                etag_cache = json.load(f)
        except: pass
    etag_cache.setdefault(slug, {})

    # ── Load metadata ─────────────────────────────────────────────────────────
    metadata_map = {}
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'r', encoding='utf-8') as f:
                for item in json.load(f):
                    metadata_map[item['id']] = item
        except: pass

    now_ms      = int(time.time() * 1000)
    cutoff_ms   = now_ms - MAX_HISTORY_MS
    any_changed = False

    # =========================================================================
    # PASS 1 — EXCHANGE ITEMS
    # Step 1: Discovery via overview API → get all item IDs + metadata
    # =========================================================================
    exchange_items = {}  # internal_id -> {type, details_id, name}

    for item_type in EXCHANGE_TYPES:
        ov_key = f'__ov_ex_{item_type}'
        status, new_etag, data = fetch_exchange_overview(league, item_type, etag_cache[slug].get(ov_key))

        if status == 304:
            # Category unchanged — restore known items from persisted metadata
            for iid, meta in metadata_map.items():
                if meta.get('category') == item_type:
                    exchange_items[iid] = {
                        'type': item_type,
                        'details_id': meta.get('detailsId') or get_details_id(meta.get('name', iid)),
                        'name': meta.get('name', iid)
                    }
            continue

        if status != 200 or not data:
            continue

        if new_etag:
            etag_cache[slug][ov_key] = new_etag

        # From 'lines': primary item list — also writes to metadata for 304 fallback
        for line in data.get('lines', []):
            iid  = line.get('id')
            if not iid:
                continue
            name       = line.get('name') or line.get('currencyTypeName') or iid
            details_id = line.get('detailsId') or get_details_id(name)
            icon       = line.get('icon') or ''
            exchange_items[iid] = {'type': item_type, 'details_id': details_id, 'name': name}
            # Write to metadata so the 304 fallback can recover item list on future runs
            metadata_map.setdefault(iid, {})
            metadata_map[iid].update({
                'id': iid, 'name': name, 'detailsId': details_id,
                'icon': icon, 'category': item_type, 'baseType': name
            })
            # Note: history seeding for exchange items happens in the per-item details loop
            # (pair.history contains exact daily rates — see seed_pair_history call below)


        # From 'items': richer info (icons, corrected names)
        for det in data.get('items', []):
            iid = det.get('id')
            if not iid:
                continue
            name       = det.get('name') or iid
            details_id = det.get('detailsId') or get_details_id(name)
            icon       = det.get('image') or det.get('icon') or ''

            exchange_items.setdefault(iid, {'type': item_type, 'details_id': details_id, 'name': name})
            exchange_items[iid].update({'name': name, 'details_id': details_id})

            metadata_map[iid] = {
                'id': iid, 'name': name, 'detailsId': details_id,
                'icon': icon, 'category': item_type, 'baseType': name,
                'flavourText': '', 'explicitModifiers': [],
                'implicitModifiers': [], 'levelRequired': 0
            }

    print(f"  Discovered {len(exchange_items)} exchange items.")

    # Step 2: Per-item price fetch (ETag per item)
    n_changed = 0
    n_304     = 0
    n_miss    = 0
    item_delay = 0.2   # base inter-item delay; increases after 429 hits

    for idx, (iid, info) in enumerate(exchange_items.items()):
        if idx > 0 and idx % 100 == 0:
            print(f"    Checking exchange items: {idx}/{len(exchange_items)}...")
        item_key    = f'i_{iid}'
        blacklist_k = f'__404_{iid}'

        # Skip items that previously consistently returned 404
        if blacklist_k in etag_cache[slug]:
            n_miss += 1
            continue

        status, new_etag, data = fetch_item_details(
            league, info['type'], info['details_id'],
            etag_cache[slug].get(item_key)
        )

        if status == 304:
            n_304 += 1
            time.sleep(0.05)   # 304 is instant, tiny sleep
            continue

        if status in (404, None) or not data:
            n_miss += 1
            if status == 404:
                etag_cache[slug][blacklist_k] = True
            if status == 429:
                item_delay = max(item_delay, 0.5)   # slow down after rate limit hit
            time.sleep(0.05)
            continue

        if new_etag:
            etag_cache[slug][item_key] = new_etag

        # Refresh metadata from details response
        if 'item' in data and iid in metadata_map:
            d = data['item']
            if d.get('name'):  metadata_map[iid]['name'] = d['name']
            if d.get('image'): metadata_map[iid]['icon'] = d['image']

        own["prices"].setdefault(iid, {})
        item_changed = False

        for pair in data.get('pairs', []):
            cur_id = pair.get('id')
            if cur_id not in PAIR_CURRENCIES or cur_id == iid:
                continue
            rate   = pair.get('rate') or 0
            volume = pair.get('volumePrimaryValue') or 0
            own["prices"][iid].setdefault(cur_id, [])
            # Seed full daily history from pair.history (exact rates, already in the response)
            seed_pair_history(own["prices"], iid, cur_id, pair.get('history', []), now_ms)
            if record_price(own["prices"][iid][cur_id], now_ms, rate, volume):
                item_changed = True

        if item_changed:
            n_changed += 1
            any_changed = True

        time.sleep(item_delay)   # polite delay; auto-increases after any 429

    print(f"  Exchange: {n_changed} prices changed | {n_304} unchanged (304) | {n_miss} not found")

    # =========================================================================
    # PASS 2 — STASH ITEMS (Unique items, Tablets, etc.)
    # Category overview with per-category ETag — one call per category.
    # Prices stored as: chaos (from API), divine (from API or derived),
    # exalted (derived from chaos using live exchange rate).
    # =========================================================================

    # Get live exchange rates for derivation (chaos <-> divine <-> exalted)
    chaos_per_divine   = 1.0    # how many chaos per 1 divine
    exalted_per_chaos  = 1.0    # how many exalted per 1 chaos

    divine_chaos_hist = own["prices"].get("divine", {}).get("chaos", [])
    if divine_chaos_hist:
        chaos_per_divine = divine_chaos_hist[-1][1]

    # exalted rate: stored as "chaos" pair on divine (rate = exalted per divine)
    # So exalted per chaos = (exalted per divine) / (chaos per divine)
    divine_exalted_hist = own["prices"].get("divine", {}).get("exalted", [])
    if divine_exalted_hist and chaos_per_divine > 0:
        exalted_per_divine = divine_exalted_hist[-1][1]
        exalted_per_chaos  = exalted_per_divine / chaos_per_divine

    n_stash = 0

    for item_type in STASH_TYPES:
        ov_key = f'__ov_st_{item_type}'
        status, new_etag, data = fetch_stash_overview(league, item_type, etag_cache[slug].get(ov_key))

        if status == 304:
            continue

        if status != 200 or not data:
            continue

        if new_etag:
            etag_cache[slug][ov_key] = new_etag

        for line in data.get('lines', []):
            iid = line.get('detailsId') or str(line.get('id', ''))
            if not iid:
                continue

            name     = line.get('name') or iid
            icon     = line.get('icon') or ''
            volume   = line.get('listingCount') or line.get('count') or 0

            # poe.ninja PoE2 stash overview: prices are in exalted orbs (primaryValue)
            # Derive chaos and divine using live exchange rates from exchange items
            exalt_p  = line.get('primaryValue') or 0
            chaos_p  = (exalt_p / exalted_per_chaos) if exalt_p and exalted_per_chaos > 0 else 0
            divine_p = (chaos_p / chaos_per_divine)  if chaos_p and chaos_per_divine  > 0 else 0

            # Update or create metadata
            if iid not in metadata_map:
                metadata_map[iid] = {
                    'id': iid, 'name': name, 'detailsId': iid,
                    'icon': icon, 'category': item_type,
                    'baseType': line.get('baseType', name),
                    'flavourText': line.get('flavourText', ''),
                    'explicitModifiers': [m.get('text','') for m in line.get('explicitModifiers',[])],
                    'implicitModifiers': [m.get('text','') for m in line.get('implicitModifiers',[])],
                    'levelRequired': line.get('levelRequired', 0)
                }
            else:
                metadata_map[iid]['name'] = name
                metadata_map[iid]['icon'] = icon

            own["prices"].setdefault(iid, {})
            item_changed = False

            # Seed 7-day history from poe.ninja's built-in sparkLine (runs once per item)
            # primaryValue is in exalted orbs — seed all three derived currencies
            sparkline = line.get('sparkLine')
            if sparkline and exalt_p:
                seed_sparkline_history(own["prices"], iid, 'exalted', exalt_p, sparkline, volume, now_ms)
                if chaos_p:
                    seed_sparkline_history(own["prices"], iid, 'chaos',  chaos_p,  sparkline, volume, now_ms)
                if divine_p:
                    seed_sparkline_history(own["prices"], iid, 'divine', divine_p, sparkline, volume, now_ms)

            for cur_id, price in [('chaos', chaos_p), ('divine', divine_p), ('exalted', exalt_p)]:
                if price and price > 0:
                    own["prices"][iid].setdefault(cur_id, [])
                    if record_price(own["prices"][iid][cur_id], now_ms, price, volume):
                        item_changed = True

            if item_changed:
                n_stash += 1
                any_changed = True

    print(f"  Stash:    {n_stash} prices changed")

    # =========================================================================
    # SAVE
    # =========================================================================
    trim_history(own["prices"], cutoff_ms)
    own["updatedAt"] = datetime.now(timezone.utc).isoformat()

    os.makedirs(DATA_DIR, exist_ok=True)

    with open(own_db_file, 'w', encoding='utf-8') as f:
        json.dump(own, f, separators=(',', ':'))
    print(f"  Saved {os.path.basename(own_db_file)} ({os.path.getsize(own_db_file)//1024} KB)")

    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(metadata_map.values()), f, indent=2)

    with open(etag_file, 'w', encoding='utf-8') as f:
        json.dump(etag_cache, f, indent=2)

    elapsed = (datetime.now(timezone.utc) - ts_start).total_seconds()
    print(f"  Done in {elapsed:.1f}s  |  any_changed={any_changed}")
    return any_changed


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='PoE Trade Scraper v2')
    parser.add_argument('--league',      default=DEFAULT_LEAGUE, help='League name')
    parser.add_argument('--all-leagues', action='store_true',    help='Run for all active leagues')
    args = parser.parse_args()

    leagues = (
        ["Runes of Aldur", "HC Runes of Aldur", "Standard", "Hardcore"]
        if args.all_leagues else [args.league]
    )

    any_league_changed = False
    has_failed = False
    for lg in leagues:
        try:
            changed = update_own_db(lg)
            any_league_changed = any_league_changed or changed
        except Exception as e:
            print(f"[FATAL] League '{lg}' failed: {e}")
            import traceback; traceback.print_exc()
            has_failed = True

    # Exit 1 only on actual fatal error/exception (otherwise exit 0 even if no prices changed)
    sys.exit(1 if has_failed else 0)
