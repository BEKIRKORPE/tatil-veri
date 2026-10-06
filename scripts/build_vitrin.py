#!/usr/bin/env python3
"""
Tatilini Planla — VİTRİN betiği (ana ekranda dönen fotoğraflar).

Her il için:
  1. data/v1/<il>/*.json içindeki Vikipedi kaydı olan yerleri toplar
     (Yeme İçme ve Konaklama hariç).
  2. Popülerlik = son 12 ayda Vikipedi sayfasının okunma sayısı (Türkçe + İngilizce).
  3. En popüler yerlerden, Wikimedia Commons'ta en az 3 uygun fotoğrafı olan
     ilk 10 yeri seçer; her birine 6-8 yatay, büyük fotoğraf koyar.
  4. data/v1/<il>/vitrin.json ve Türkiye geneli için data/v1/vitrin.json yazar.

Fotoğrafların hepsi serbest lisanslı (CC0 / Kamu malı / CC BY / CC BY-SA).
CC BY lisansı gereği uygulama her fotoğrafta "Foto: <yazar> / <lisans>" göstermeli.

Çalıştırma:  python scripts/build_vitrin.py
             VITRIN_FORCE=1 python scripts/build_vitrin.py   (hepsini yeniden üret)
             ONLY="Muğla" python scripts/build_vitrin.py
"""
import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_data as bd   # il listesi, slug, fold, write_json ortak

OUT = bd.OUT
USER_AGENT = "TatiliniPlanla-vitrin/1.0 (KorpeSoft; korpe.bekir71@gmail.com)"
VERSION = 1
FRESH_DAYS = 30            # ayda bir yenilenir → "o yılın popüler yerleri" kendiliğinden güncellenir
TOP_PLACES = 10            # il başına yer
MAX_PHOTOS = 8
MIN_PHOTOS = 3             # bundan az fotoğrafı olan yer vitrine girmez
GOOD_PHOTOS = 6            # önce 6-8 fotoğraflı yerler seçilir; yetmezse 3-5'lik yerlerle tamamlanır
MAX_PER_CATEGORY = 4       # vitrin tek türe yığılmasın (ör. hep cami)
CANDIDATES = 40            # okunma sayısına göre ilk 40 aday için fotoğraf aranır
COUNTRY_PLACES = 24        # Türkiye geneli ana ekran
COUNTRY_PER_PROVINCE = 2
TIME_BUDGET_S = 70 * 60
WORKERS = 4
THUMB_WIDTH = 1280
SKIP_CATEGORIES = {"yeme-icme", "konaklama"}
OK_LICENSE = re.compile(r"^(cc0|cc[- ]by(-sa)?([- ]\d(\.\d)?)?.*|public domain|pd.*|attribution.*)$", re.I)
NONCOMMERCIAL = re.compile(r"(\bn[cd]\b|non[- ]?commercial|no[- ]?deriv)", re.I)   # reklamlı uygulama: NC/ND yasak
BAD_TITLE = re.compile(r"(map|harita|plan|logo|flag|bayrak|coat|arma|diagram|chart|locator|svg|\.tif|"
                       r"inscription|yazıt|coin|sikke|stamp|pul|drawing|çizim)", re.I)

API_WD = "https://www.wikidata.org/w/api.php"
API_COMMONS = "https://commons.wikimedia.org/w/api.php"
API_PV = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/%s/all-access/user/%s/monthly/%s/%s"

PRINT_LOCK = threading.Lock()


def log(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


def http_json(url, params=None, tries=3):
    """GET → dict. 404 → {} ; başarısız → None."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code not in (429, 500, 502, 503, 504):
                return None
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


# ---------------------------------------------------------------- adaylar
def candidates_for(province):
    """İldeki Vikipedi kayıtlı yerler (aynı ad bir kez)."""
    folder = os.path.join(OUT, bd.slug(province))
    seen, out = set(), []
    if not os.path.isdir(folder):
        return out
    for fn in sorted(os.listdir(folder)):
        cat = fn[:-5]
        if not fn.endswith(".json") or cat in SKIP_CATEGORIES or cat == "vitrin":
            continue
        try:
            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for el in data.get("elements", []):
            t = el.get("tags") or {}
            if not (t.get("wikidata") or t.get("wikipedia")):
                continue
            name = (t.get("name:tr") or t.get("name") or "").strip()
            key = bd.fold(name)
            if not name or key in seen:
                continue
            seen.add(key)
            c = el.get("center") or {}
            out.append({
                "name": name,
                "category": cat,
                "heading": data.get("heading") or data.get("category") or cat,
                "osm": "%s/%s" % (el.get("type", "node"), el.get("id")),
                "lat": el.get("lat", c.get("lat")),
                "lon": el.get("lon", c.get("lon")),
                "wikidata": (t.get("wikidata") or "").split(";")[0].strip() or None,
                "wikipedia": t.get("wikipedia"),
            })
    return out


def wikidata_info(qids, fetch=http_json):
    """{Q: {'trwiki':..,'enwiki':..,'image':..,'commonscat':..}} — 50'şerli toplu sorgu."""
    info = {}
    qids = [q for q in dict.fromkeys(qids) if q and re.match(r"^Q\d+$", q)]
    for i in range(0, len(qids), 50):
        part = qids[i:i + 50]
        d = fetch(API_WD, {"action": "wbgetentities", "ids": "|".join(part), "props": "claims|sitelinks",
                           "sitefilter": "trwiki|enwiki", "format": "json"})
        for q, ent in ((d or {}).get("entities") or {}).items():
            if "missing" in ent:
                continue
            claims = ent.get("claims") or {}

            def first(p):
                try:
                    return claims[p][0]["mainsnak"]["datavalue"]["value"]
                except (KeyError, IndexError, TypeError):
                    return None
            links = ent.get("sitelinks") or {}
            info[q] = {
                "trwiki": (links.get("trwiki") or {}).get("title"),
                "enwiki": (links.get("enwiki") or {}).get("title"),
                "image": first("P18"),
                "commonscat": first("P373"),
            }
    return info


def pageviews(project, title, start, end, fetch=http_json):
    if not title:
        return 0
    art = urllib.parse.quote(title.replace(" ", "_"), safe="")
    d = fetch(API_PV % (project, art, start, end))
    return sum(int(it.get("views", 0)) for it in (d or {}).get("items", []))


def period():
    """Son tamamlanmış 12 ay: (başlangıç, bitiş, etiket)."""
    now = datetime.now(timezone.utc)
    end = now.replace(day=1) - timedelta(days=1)                 # geçen ayın son günü
    start = (end.replace(day=1) - timedelta(days=330)).replace(day=1)
    return start.strftime("%Y%m%d00"), end.strftime("%Y%m%d00"), "%s – %s" % (start.strftime("%m.%Y"), end.strftime("%m.%Y"))


# ---------------------------------------------------------------- fotoğraflar
def strip_html(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80]


def photo_ok(page):
    title = page.get("title", "")
    ii = (page.get("imageinfo") or [{}])[0]
    if ii.get("mime") != "image/jpeg" or BAD_TITLE.search(title):
        return None
    w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
    if w < 1200 or h < 600 or w < h * 1.2:        # yatay ve büyük (banner için)
        return None
    meta = ii.get("extmetadata") or {}
    lic = strip_html((meta.get("LicenseShortName") or {}).get("value"))
    if not lic or not OK_LICENSE.match(lic) or NONCOMMERCIAL.search(lic):
        return None
    author = strip_html((meta.get("Artist") or {}).get("value")) or "Wikimedia Commons"
    url = ii.get("thumburl") or ii.get("url")
    if not url:
        return None
    tw = int(ii.get("thumbwidth") or w)
    th = int(ii.get("thumbheight") or h)
    return {"url": url, "w": tw, "h": th, "author": author, "license": lic,
            "page": ii.get("descriptionurl") or ""}


II_PROPS = {"prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": THUMB_WIDTH,
            "iiextmetadatafilter": "Artist|LicenseShortName", "format": "json"}


def photos_for(place, wd, fetch=http_json):
    picks, seen = [], set()

    def add_pages(d):
        pages = list(((d or {}).get("query") or {}).get("pages", {}).values())
        pages.sort(key=lambda p: p.get("index", 0))
        for p in pages:
            if p.get("title") in seen or len(picks) >= MAX_PHOTOS:
                continue
            ph = photo_ok(p)
            if ph:
                seen.add(p["title"])
                picks.append(ph)

    if wd.get("image"):
        add_pages(fetch(API_COMMONS, dict(II_PROPS, action="query", titles="File:" + wd["image"])))
    if wd.get("commonscat") and len(picks) < MAX_PHOTOS:
        add_pages(fetch(API_COMMONS, dict(II_PROPS, action="query", generator="categorymembers",
                                         gcmtitle="Category:" + wd["commonscat"], gcmtype="file",
                                         gcmlimit=40)))
    return picks


# ---------------------------------------------------------------- il
def wiki_title(place, wd, lang):
    w = place.get("wikipedia") or ""
    if w.startswith(lang + ":"):
        return w.split(":", 1)[1]
    return wd.get(lang + "wiki")


def build_province(province, fetch=http_json):
    cands = candidates_for(province)
    start, end, label = period()
    wdinfo = wikidata_info([c["wikidata"] for c in cands], fetch)
    for c in cands:
        wd = wdinfo.get(c["wikidata"] or "", {})
        c["_wd"] = wd
        c["views"] = (pageviews("tr.wikipedia", wiki_title(c, wd, "tr"), start, end, fetch)
                      + pageviews("en.wikipedia", wiki_title(c, wd, "en"), start, end, fetch))
    cands = [c for c in cands if c["_wd"].get("image") or c["_wd"].get("commonscat")]
    cands.sort(key=lambda c: -c["views"])
    good, ok = [], []
    for c in cands[:CANDIDATES]:
        if len(good) >= TOP_PLACES * 2:
            break
        ph = photos_for(c, c["_wd"], fetch)
        if len(ph) >= MIN_PHOTOS:
            row = {k: c[k] for k in ("name", "category", "heading", "osm", "lat", "lon", "views")} | {"photos": ph}
            (good if len(ph) >= GOOD_PHOTOS else ok).append(row)
    places, per_cat = [], {}

    def take(pool, limit_cat):
        for row in pool:
            if len(places) >= TOP_PLACES:
                return
            if row in places or (limit_cat and per_cat.get(row["category"], 0) >= MAX_PER_CATEGORY):
                continue
            places.append(row)
            per_cat[row["category"]] = per_cat.get(row["category"], 0) + 1

    take(good, True)                                         # 1) 6-8 fotoğraflı, tür sınırlı
    take(ok, True)                                           # 2) 3-5 fotoğraflı, tür sınırlı
    take(sorted(good + ok, key=lambda r: -r["views"]), False)  # 3) hâlâ boşluk varsa sınırsız doldur
    places.sort(key=lambda r: -r["views"])
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "v": VERSION,
        "province": province,
        "period": label,
        "source": "Fotoğraflar: Wikimedia Commons (serbest lisans). Popülerlik: Vikipedi okunma sayısı.",
        "places": places,
    }


def is_fresh(path):
    if os.environ.get("VITRIN_FORCE") == "1" or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("v", 0) < VERSION:
            return False
        age = datetime.now(timezone.utc) - datetime.fromisoformat(obj["generated"].replace("Z", "+00:00"))
        return age.days < FRESH_DAYS
    except Exception:
        return False


def write_country():
    """Türkiye geneli ana ekran: en çok okunan yerler, il başına en fazla 2."""
    rows = []
    for p in bd.PROVINCES:
        path = os.path.join(OUT, bd.slug(p), "vitrin.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        for i, pl in enumerate(obj.get("places", [])[:COUNTRY_PER_PROVINCE]):
            rows.append(dict(pl, province=p, provinceSlug=bd.slug(p), photos=pl["photos"][:4]))
    rows.sort(key=lambda r: -r.get("views", 0))
    bd.write_json(os.path.join(OUT, "vitrin.json"), {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "v": VERSION,
        "period": period()[2],
        "source": "Fotoğraflar: Wikimedia Commons (serbest lisans). Popülerlik: Vikipedi okunma sayısı.",
        "places": rows[:COUNTRY_PLACES],
    })
    return len(rows[:COUNTRY_PLACES])


def main(fetch=http_json, workers=WORKERS):
    only = {bd.fold(s) for s in os.environ.get("ONLY", "").split(",") if s.strip()}
    provinces = [p for p in bd.ordered_provinces() if not only or bd.fold(p) in only]
    started = time.time()
    counts = {"done": 0, "skipped": 0, "failed": 0, "late": 0}
    todo = []
    for p in provinces:
        if is_fresh(os.path.join(OUT, bd.slug(p), "vitrin.json")):
            counts["skipped"] += 1
        else:
            todo.append(p)
    log("Vitrin: %d il yapılacak (%d taze atlandı)" % (len(todo), counts["skipped"]))

    def work(p):
        if time.time() - started > TIME_BUDGET_S:
            with PRINT_LOCK:
                counts["late"] += 1
            return
        try:
            res = build_province(p, fetch)
        except Exception as e:
            with PRINT_LOCK:
                counts["failed"] += 1
            log("%s: x vitrin hatası %s" % (p, type(e).__name__))
            return
        path = os.path.join(OUT, bd.slug(p), "vitrin.json")
        old_n = 0
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old_n = len(json.load(f).get("places", []))
            except Exception:
                pass
        if not res["places"] and old_n:
            with PRINT_LOCK:
                counts["failed"] += 1      # ağ sorunu olabilir: dolu eski dosyayı boşla ezme
            log("%s: x yer bulunamadı, eski vitrin korundu" % p)
            return
        bd.write_json(path, res)
        with PRINT_LOCK:
            counts["done"] += 1
        log("%s: ✓ %d yer, %d foto" % (p, len(res["places"]), sum(len(x["photos"]) for x in res["places"])))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, todo))
    n = write_country()
    log("VİTRİN ÖZET: üretilen=%d atlanan=%d hatalı=%d kalan=%d | Türkiye vitrini %d yer"
        % (counts["done"], counts["skipped"], counts["failed"], counts["late"], n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
