#!/usr/bin/env python3
"""
Tatilini Planla — veri hazırlama betiği.

81 il × 9 kategori için OpenStreetMap (Overpass) verisini bir kez çeker,
temizler ve data/v1/<il>/<kategori>.json dosyalarına yazar.
Dosyalar GitHub Pages üzerinden uygulamaya sunulur.

Çalıştırma:  python scripts/build_data.py            (taze dosyaları atlar)
             FORCE=1 python scripts/build_data.py    (hepsini yeniden üretir)
             ONLY="Muğla,Ankara" python scripts/build_data.py
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "v1")
USER_AGENT = "TatiliniPlanla-veri/1.0 (KorpeSoft; korpe.bekir71@gmail.com)"
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
FRESH_DAYS = 25            # bu kadar yeni dosyalar yeniden sorgulanmaz (yarıda kalan çalışma devam eder)
TIME_BUDGET_S = 5 * 3600   # GitHub Actions 6 saat sınırının altında kal
PAUSE_S = 3                # sunucuya nazik davran
MAX_ELEMENTS = 60          # uygulama zaten en fazla 30 gösterir; sıralama için pay bırakılır

PROVINCES = [
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Aksaray", "Amasya", "Ankara", "Antalya", "Ardahan",
    "Artvin", "Aydın", "Balıkesir", "Bartın", "Batman", "Bayburt", "Bilecik", "Bingöl", "Bitlis", "Bolu",
    "Burdur", "Bursa", "Çanakkale", "Çankırı", "Çorum", "Denizli", "Diyarbakır", "Düzce", "Edirne",
    "Elazığ", "Erzincan", "Erzurum", "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane", "Hakkâri", "Hatay",
    "Iğdır", "Isparta", "İstanbul", "İzmir", "Kahramanmaraş", "Karabük", "Karaman", "Kars", "Kastamonu",
    "Kayseri", "Kilis", "Kırıkkale", "Kırklareli", "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya",
    "Manisa", "Mardin", "Mersin", "Muğla", "Muş", "Nevşehir", "Niğde", "Ordu", "Osmaniye", "Rize",
    "Sakarya", "Samsun", "Siirt", "Sinop", "Sivas", "Şanlıurfa", "Şırnak", "Tekirdağ", "Tokat", "Trabzon",
    "Tunceli", "Uşak", "Van", "Yalova", "Yozgat", "Zonguldak",
]

# Uygulamadaki MainActivity.overpassFilters ile birebir aynı (değiştirirsen ikisini birlikte değiştir).
FILTERS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "filters.json"), encoding="utf-8"))
CATEGORIES = ["Deniz ve Plaj", "Doğa Tatili", "Kamp Alanları", "Eğlence ve Gece Hayatı", "Aktivite ve Spor",
              "Tarih ve Kültür", "Aile Tatili", "Ekonomik Tatil", "Yeme İçme"]
WATER_ALT = "__SU_ALTERNATIF__"
WATER_ALT_HEADING = "Göl, Şelale ve Su Kenarı"

# Uygulamanın ihtiyaç duyduğu etiketler (dosya küçük kalsın)
KEEP_TAGS = ["name", "name:tr", "wikipedia", "wikidata", "phone", "contact:phone", "contact:mobile", "mobile",
             "cuisine", "sport", "amenity", "leisure", "tourism", "natural", "waterway", "historic", "man_made"]


def turkish_lower(s):
    return s.replace("İ", "i").replace("I", "ı").lower()


def fold(s):
    s = turkish_lower((s or "").strip())
    for a, b in (("ç", "c"), ("ğ", "g"), ("ı", "i"), ("ö", "o"), ("ş", "s"), ("ü", "u"),
                 ("â", "a"), ("î", "i"), ("û", "u")):
        s = s.replace(a, b)
    return s


def slug(s):
    """Uygulamadaki PlaceLogic.slug ile aynı: 'Eğlence ve Gece Hayatı' -> 'eglence-ve-gece-hayati'."""
    return re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")


def notable_filters(filters):
    return filters.replace("(area.searchArea);", '[~"^(wikipedia|wikidata)$"~"."](area.searchArea);')


def build_query(province, filters):
    area = ('area["name"="%s"]["boundary"="administrative"]["admin_level"="4"]->.searchArea;'
            % province.replace('"', ""))
    return ("[out:json][timeout:90];" + area
            + "(" + notable_filters(filters) + ");out tags center 60;"
            + "(" + filters + ");out tags center 200;")


def fetch(query):
    """Overpass'a sorar. Başarılıysa dict, olmazsa None. 429/504'te bekleyip tekrar dener."""
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for attempt in range(3):
        for endpoint in ENDPOINTS:
            req = urllib.request.Request(endpoint, data=data, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.loads(r.read().decode("utf-8"))
                remark = body.get("remark", "") or ""
                if "runtime error" in remark or "timed out" in remark:
                    print("   ! sunucu notu:", remark[:120], flush=True)
                    continue
                return body
            except urllib.error.HTTPError as e:
                print("   ! %s HTTP %s" % (endpoint.split("/")[2], e.code), flush=True)
                if e.code in (429, 504):
                    time.sleep(20 * (attempt + 1))
            except Exception as e:  # zaman aşımı, bağlantı hatası
                print("   ! %s %s" % (endpoint.split("/")[2], type(e).__name__), flush=True)
        time.sleep(10 * (attempt + 1))
    return None


# İsim yerine tür yazılmış kayıtlar ("Restaurant", "Kafe", "Park" ...)
GENERIC_NAMES = {fold(x) for x in [
    "restaurant", "restoran", "lokanta", "cafe", "kafe", "kahve", "bar", "pub", "büfe", "kantin",
    "park", "çocuk parkı", "oyun parkı", "playground", "plaj", "beach", "halk plajı", "kamp", "camping",
    "cami", "camii", "mescit", "mosque", "kilise", "church", "müze", "museum", "otopark", "parking",
    "piknik alanı", "picnic site", "seyir terası", "viewpoint", "spor salonu", "gym", "sahil", "koy",
]}
# Tatil rehberinde öne çıkmaması gereken doğa kayıtları (zirve, burun, sırt...)
LOW_NATURAL = {"peak", "cape", "ridge", "hill", "saddle", "rock", "stone", "tree", "wood", "scrub",
               "grassland", "heath", "bare_rock", "scree", "shrubbery", "wetland", "arete", "volcano"}
GOOD_NATURAL = {"beach", "bay", "waterfall", "cave_entrance", "hot_spring", "spring", "gorge", "valley",
                "water", "lagoon", "canyon", "sinkhole"}


def score(tags):
    """Yerin rehberdeki önemi. Yüksek puan listede üstte."""
    s = 0
    if "wikipedia" in tags or "wikidata" in tags:
        s += 5
    if "image" in tags or "wikimedia_commons" in tags:
        s += 2
    if tags.get("tourism") in ("attraction", "viewpoint", "museum", "theme_park", "zoo", "aquarium"):
        s += 3
    for k in ("website", "contact:website", "opening_hours", "phone", "contact:phone", "cuisine", "description"):
        if k in tags:
            s += 1
    nat = tags.get("natural")
    if nat in GOOD_NATURAL:
        s += 2
    if nat in LOW_NATURAL and "tourism" not in tags:
        s -= 7
    return s


def clean_elements(body):
    """Adsız, tür-adlı ve zincir kayıtları atar; aynı adı bir kez tutar; önemine göre sıralar."""
    seen, rows = set(), []
    for order, el in enumerate((body or {}).get("elements", [])):
        tags = el.get("tags") or {}
        name = (tags.get("name:tr") or tags.get("name") or "").strip()
        if len(name) < 2:
            continue
        key = fold(name)
        if key in seen or key in GENERIC_NAMES:
            continue
        if "brand" in tags or "brand:wikidata" in tags:   # McDonald's, Starbucks gibi zincirler
            continue
        seen.add(key)
        sc = score(tags)
        small = {k: tags[k] for k in KEEP_TAGS if k in tags}
        if "wikidata" in small:
            small["wikidata"] = str(small["wikidata"])[:20]
        if sc < 0:
            # Uygulama Vikipedi kaydı olanları öne alır; zirve/burun gibi yerler öne çıkmasın
            small.pop("wikipedia", None)
            small.pop("wikidata", None)
        out = {"type": el.get("type", "node"), "id": el.get("id"), "tags": small}
        if "lat" in el and "lon" in el:
            out["lat"], out["lon"] = round(el["lat"], 6), round(el["lon"], 6)
        elif el.get("center"):
            out["center"] = {"lat": round(el["center"]["lat"], 6), "lon": round(el["center"]["lon"], 6)}
        rows.append((-sc, order, out))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows][:MAX_ELEMENTS]


def is_fresh(path):
    if os.environ.get("FORCE") == "1" or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            gen = json.load(f).get("generated", "")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(gen.replace("Z", "+00:00"))
        return age.days < FRESH_DAYS
    except Exception:
        return False


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def build_one(province, category, fetcher=fetch):
    """Tek il/kategori dosyası üretir. Başarısızsa None döner (dosya yazılmaz)."""
    heading = category
    body = fetcher(build_query(province, FILTERS[category]))
    if body is None:
        return None
    elements = clean_elements(body)
    if category == "Deniz ve Plaj" and not elements:
        alt = fetcher(build_query(province, FILTERS[WATER_ALT]))
        if alt is None:
            return None
        elements = clean_elements(alt)
        if elements:
            heading = WATER_ALT_HEADING
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "province": province,
        "category": category,
        "heading": heading,
        "source": "© OpenStreetMap katkıda bulunanları (ODbL)",
        "elements": elements,
    }


def write_index():
    index = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "provinces": {}}
    for p in PROVINCES:
        row = {}
        for c in CATEGORIES:
            path = os.path.join(OUT, slug(p), slug(c) + ".json")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    row[slug(c)] = len(json.load(f).get("elements", []))
        index["provinces"][slug(p)] = {"name": p, "counts": row}
    write_json(os.path.join(OUT, "index.json"), index)


def main(fetcher=fetch, pause=PAUSE_S):
    only = [s.strip() for s in os.environ.get("ONLY", "").split(",") if s.strip()]
    only_keys = {fold(o) for o in only}
    provinces = [p for p in PROVINCES if not only or fold(p) in only_keys]
    started = time.time()
    done = skipped = failed = 0
    for p in provinces:
        for c in CATEGORIES:
            path = os.path.join(OUT, slug(p), slug(c) + ".json")
            if is_fresh(path):
                skipped += 1
                continue
            if time.time() - started > TIME_BUDGET_S:
                print("Süre bütçesi doldu; kalanlar bir sonraki çalışmada tamamlanacak.")
                write_index()
                print("ÖZET: üretilen=%d atlanan=%d hatalı=%d" % (done, skipped, failed))
                return 0
            print("%s / %s" % (p, c), flush=True)
            result = build_one(p, c, fetcher)
            if result is None:
                failed += 1
                print("   x alınamadı, sonra tekrar denenecek", flush=True)
            else:
                write_json(path, result)
                done += 1
                print("   ✓ %d yer" % len(result["elements"]), flush=True)
            time.sleep(pause)
    write_index()
    print("ÖZET: üretilen=%d atlanan=%d hatalı=%d" % (done, skipped, failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
