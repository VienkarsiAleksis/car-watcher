"""Car watcher: polls ss.com for new listings matching your searches and alerts you.

Usage:
    python car_watcher.py            # run forever, checking every N minutes
    python car_watcher.py --once     # single check (for Task Scheduler)
    python car_watcher.py --test     # print current matches, no alerts, nothing saved

Only uses the Python standard library.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
SEEN_FILE = HERE / "seen.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) car-watcher/1.0"

# ss.com has no drive-type field, so rear-wheel drive is judged by make + the
# model slug in the ad URL (e.g. .../cars/bmw/320/...) plus keywords in the ad text.
RWD_MODELS = {
    "bmw": r"\d{3}|[1-8]-series|m\d|m-series|z\d|z-series",
    "mercedes": r"(c|e|s|cl|clk|cls|sl|slk)-?\d.*|190|200|230|250|300|mercedes-benz",
    "volvo": r"240|740|760|940|960|s90|v90",
    "lexus": r"gs|is|ls|lls|rc|sc",
    "opel": r"omega|senator",
    "ford": r"mustang|scorpio|sierra|granada",
    "toyota": r"supra|crown",
    "mazda": r"mx-5|rx-8",
    "nissan": r"200sx|350z|370z|skyline",
    "chrysler": r"300c|crossfire",
    "jaguar": r"s-type|xj|xjs|xk|xkr|xf|xe",
    "porsche": r".*",
}
RWD_YES_WORDS = ["aizmugurējā piedziņa", "aizmugures piedziņa", "aizmugurējo piedziņu",
                 "rwd", "rear wheel drive", "задний привод"]
RWD_NO_WORDS = ["xdrive", "x-drive", "4matic", "quattro", "4x4", "4wd", "awd",
                "pilnpiedziņ", "полный привод", "priekšējā piedziņa"]
BMW_AWD = re.compile(r"\b\d{3}\s?x[id]?\b", re.I)  # 325xi, 530xd, 330 xd


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def to_int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def to_float(text):
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(m.group().replace(",", ".")) if m else None


def strip_tags(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def parse_ss_rss(xml_text):
    """Return a list of listing dicts from an ss.com category RSS feed."""
    listings = []
    if not xml_text.lstrip().startswith("<?xml"):
        raise ValueError("not an RSS feed - check that the URL is a valid ss.com category page")
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        desc_html = item.findtext("description") or ""
        # Fields look like "Gads: <b>2019</b><br/>"; split on <br/>, strip tags
        fields = {}
        for segment in re.split(r"<br\s*/?>", desc_html):
            plain = strip_tags(segment)
            if ":" in plain:
                key, value = plain.split(":", 1)
                fields[key.strip()] = value.strip()
        mileage = fields.get("Nobraukums", "")
        mileage_km = to_int(mileage)
        if mileage_km is not None and "tūkst" in mileage:
            mileage_km *= 1000
        url = item.findtext("link", "").strip()
        path = re.search(r"/transport/cars/([^/]+)/([^/]+)/", url)
        listings.append({
            "id": url,
            "url": url,
            "make": path.group(1) if path else "",
            "model": path.group(2) if path else "",
            "title": " ".join((item.findtext("title") or "").split()),
            "year": to_int(fields.get("Gads")),
            "engine": fields.get("Tilpums"),
            "engine_l": to_float(fields.get("Tilpums")),
            "mileage_km": mileage_km,
            "price": to_int(fields.get("Cena", "").split("€")[0]),
            "source": "ss.com",
        })
    return listings


def fetch_ad_details(listing):
    """Open the ad page and add gearbox, fuel, inspection date and full text."""
    page = fetch(listing["url"])
    opts = {}
    for k, v in re.findall(r'class="ads_opt_name[^"]*"[^>]*>(.*?)</td>\s*<td[^>]*class="ads_opt"[^>]*>(.*?)</td>',
                           page, re.S):
        opts[strip_tags(k).rstrip(":")] = strip_tags(v)
    desc = re.search(r'id="msg_div_msg"[^>]*>(.*?)<table', page, re.S)
    listing["brand_model"] = opts.get("Marka", "")
    listing["motor"] = opts.get("Motors", "")          # e.g. "2.0 dīzelis"
    listing["gearbox"] = opts.get("Ātrumkārba", "")     # "Manuāla" / "Automāts"
    listing["inspection"] = opts.get("Tehniskā apskate", "")  # "03.2027"
    listing["text"] = strip_tags(desc.group(1)) if desc else listing["title"]
    if listing["engine_l"] is None:
        listing["engine_l"] = to_float(listing["motor"])
    return listing


def ss_rss_url(page_url):
    """Turn a normal ss.com category URL into its RSS feed URL."""
    return page_url.rstrip("/") + "/rss/"


def month_value(mm_yyyy):
    m = re.search(r"(\d{1,2})\.(\d{4})", mm_yyyy or "")
    return int(m.group(2)) * 12 + int(m.group(1)) if m else None


def is_rear_wheel_drive(listing):
    text = f"{listing['title']} {listing.get('brand_model', '')} {listing.get('text', '')}".lower()
    if any(w in text for w in RWD_NO_WORDS) or (listing["make"] == "bmw" and BMW_AWD.search(text)):
        return False
    if any(w in text for w in RWD_YES_WORDS):
        return True
    return rwd_model(listing)


def rwd_model(listing):
    pattern = RWD_MODELS.get(listing["make"])
    return bool(pattern and re.fullmatch(pattern, listing["model"]))


def quick_match(listing, s):
    """Filters that only need the RSS feed data (cheap, no extra request)."""
    p, y, km, eng = listing["price"], listing["year"], listing["mileage_km"], listing["engine_l"]
    if s.get("max_price") and (p is None or p > s["max_price"]):
        return False
    if s.get("min_price") and (p is None or p < s["min_price"]):
        return False
    if s.get("min_year") and (y is None or y < s["min_year"]):
        return False
    if s.get("max_year") and (y is None or y > s["max_year"]):
        return False
    if s.get("max_mileage_km") and km is not None and km > s["max_mileage_km"]:
        return False
    if s.get("min_engine_l") and eng is not None and eng < s["min_engine_l"]:
        return False
    if s.get("rear_wheel_drive") and not rwd_model(listing):
        return False
    return True


def needs_details(s):
    return any(s.get(k) for k in ("gearbox", "fuels", "inspection_valid_until", "rear_wheel_drive",
                                  "must_contain_any", "must_not_contain", "min_engine_l"))


def full_match(listing, s):
    """Filters that need the ad page (gearbox, fuel, inspection, full text)."""
    text = f"{listing['title']} {listing.get('text', '')}".lower()
    if s.get("min_engine_l") and (listing["engine_l"] is None or listing["engine_l"] < s["min_engine_l"]):
        return False
    if s.get("gearbox") and s["gearbox"].lower() not in listing.get("gearbox", "").lower():
        return False
    if s.get("fuels") and not any(f.lower() in listing.get("motor", "").lower() for f in s["fuels"]):
        return False
    if s.get("inspection_valid_until"):
        have, need = month_value(listing.get("inspection")), month_value(s["inspection_valid_until"])
        if have is None or have < need:
            return False
    if s.get("rear_wheel_drive") and not is_rear_wheel_drive(listing):
        return False
    if s.get("must_contain_any") and not any(w.lower() in text for w in s["must_contain_any"]):
        return False
    if any(w.lower() in text for w in s.get("must_not_contain", [])):
        return False
    return True


def describe(listing):
    parts = [
        f"{listing['price']:,} €".replace(",", " ") if listing["price"] else "price ?",
        str(listing["year"] or "year ?"),
        listing.get("motor") or (f"{listing['engine']} L" if listing["engine"] else None),
        listing.get("gearbox"),
        f"{listing['mileage_km'] // 1000} tkm" if listing["mileage_km"] else None,
        f"TA {listing['inspection']}" if listing.get("inspection") else None,
    ]
    return " · ".join(p for p in parts if p)


# ---------- alerts ----------

def windows_notify(title, body, url):
    def esc(s):
        return html.escape(s, quote=True).replace("'", "&apos;")
    xml = (
        f"<toast activationType='protocol' launch='{esc(url)}'><visual><binding template='ToastGeneric'>"
        f"<text>{esc(title)}</text><text>{esc(body)}</text></binding></visual></toast>"
    )
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:TOAST_XML);"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    env = dict(os.environ, TOAST_XML=xml)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def telegram_api(token, method, params=None):
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30) as r:
        return json.loads(r.read())


def telegram_chat_id(cfg):
    """Return the chat id; if not set yet, pick it up from the first message sent to the bot."""
    a = cfg.get("alerts", {})
    if a.get("telegram_chat_id") or not a.get("telegram_bot_token"):
        return a.get("telegram_chat_id")
    updates = telegram_api(a["telegram_bot_token"], "getUpdates").get("result", [])
    for u in reversed(updates):
        chat = (u.get("message") or {}).get("chat")
        if chat:
            a["telegram_chat_id"] = str(chat["id"])
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            raw.setdefault("alerts", {})["telegram_chat_id"] = a["telegram_chat_id"]
            CONFIG_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            telegram_api(a["telegram_bot_token"], "sendMessage",
                         {"chat_id": a["telegram_chat_id"], "text": "✅ Car watcher connected. Alerts will arrive here."})
            print(f"Telegram connected (chat id {a['telegram_chat_id']}).")
            return a["telegram_chat_id"]
    print("Telegram: no chat yet - open your bot in Telegram and press Start / send it any message.")
    return None


def alert(cfg, search_name, listing):
    title = f"🚗 {search_name}: {describe(listing)}"
    body = listing.get("brand_model") or listing["title"][:120]
    print(f"  NEW  {title}\n       {listing['url']}")
    a = cfg.get("alerts", {})
    if a.get("windows_notification") and sys.platform == "win32":
        try:
            windows_notify(title, body, listing["url"])
        except Exception as e:
            print(f"  (windows notification failed: {e})")
    chat_id = telegram_chat_id(cfg)
    if a.get("telegram_bot_token") and chat_id:
        try:
            telegram_api(a["telegram_bot_token"], "sendMessage",
                         {"chat_id": chat_id, "text": f"{title}\n{body}\n{listing['url']}"})
        except Exception as e:
            print(f"  (telegram failed: {e})")


# ---------- main loop ----------

def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return set()


def check(cfg, test=False):
    seen = set() if test else load_seen()
    for s in cfg["searches"]:
        urls = s.get("ss_urls") or [s["ss_url"]]
        listings, by_id = [], {}
        for url in urls:
            try:
                for l in parse_ss_rss(fetch(ss_rss_url(url))):
                    by_id.setdefault(l["id"], l)
            except Exception as e:
                print(f"[{s['name']}] {url} fetch failed: {e}")
        listings = list(by_id.values())
        hits = 0
        for l in listings:
            if l["id"] in seen or not quick_match(l, s):
                continue
            if needs_details(s):
                try:
                    fetch_ad_details(l)
                except Exception as e:
                    print(f"  could not open {l['url']}: {e}")
                    continue
                time.sleep(1)  # be gentle with ss.com
            seen.add(l["id"])  # checked once; don't re-open it next time
            if not full_match(l, s):
                continue
            hits += 1
            if test:
                print(f"  {describe(l)}  {l['url']}\n    {l.get('brand_model') or l['title'][:100]}")
            else:
                alert(cfg, s["name"], l)
        print(f"[{s['name']}] {len(listings)} listings in feeds, {hits} new match(es)")
    if not test:
        SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def main():
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # Secrets can come from environment variables (GitHub Actions) instead of config.json
    alerts = cfg.setdefault("alerts", {})
    for key, env in (("telegram_bot_token", "TELEGRAM_BOT_TOKEN"), ("telegram_chat_id", "TELEGRAM_CHAT_ID")):
        if os.environ.get(env):
            alerts[key] = os.environ[env]
    if "--test" in sys.argv:
        check(cfg, test=True)
    elif "--once" in sys.argv:
        check(cfg)
    else:
        minutes = cfg.get("check_every_minutes", 5)
        print(f"Watching {len(cfg['searches'])} search(es), every {minutes} min. Ctrl+C to stop.")
        while True:
            print(time.strftime("%H:%M:%S"), "checking...")
            try:
                telegram_chat_id(cfg)
                check(cfg)
            except Exception as e:
                print(f"check failed: {e}")
            time.sleep(minutes * 60)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
