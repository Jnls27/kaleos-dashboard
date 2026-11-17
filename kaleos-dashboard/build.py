#!/usr/bin/env python3
"""Construye el dashboard de Kaleos (Meta + Google Ads) a partir de la API de Windsor.ai.

Uso:  WINDSOR_API_KEY=xxx python build.py            -> genera dist/index.html
Datos: desde el 1 de enero del año en curso hasta ayer (hora de Madrid).
"""
import base64, calendar, concurrent.futures as cf, io, json, os, re, sys, time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
API = "https://connectors.windsor.ai/{}"
KEY = os.environ.get("WINDSOR_API_KEY", "")

YESTERDAY = datetime.now(ZoneInfo("Europe/Madrid")).date() - timedelta(days=1)
START = date(YESTERDAY.year, 1, 1)
UPD = YESTERDAY
iso = lambda d: d.isoformat()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Windsor API
def fetch(connector, fields, d1, d2, account):
    """Una consulta a la API de Windsor con reintentos. Devuelve la lista de filas."""
    params = {"api_key": KEY, "fields": ",".join(fields), "date_from": iso(d1), "date_to": iso(d2),
              "select_accounts": account}
    last = None
    for attempt in range(5):
        try:
            r = requests.get(API.format(connector), params=params, timeout=600)
            if r.status_code == 200:
                j = r.json()
                rows = j.get("data", j) if isinstance(j, dict) else j
                if isinstance(rows, list):
                    return rows
                last = f"respuesta inesperada: {str(j)[:300]}"
            else:
                last = f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as e:  # timeout, conexión...
            last = repr(e)
        wait = 20 * (attempt + 1)
        log(f"  reintento {attempt + 1} ({connector} {fields[:3]}…): {last}; espero {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"Windsor falló tras 5 intentos: {connector} {fields} {d1}-{d2}: {last}")


def months(d1, d2):
    d = date(d1.year, d1.month, 1)
    while d <= d2:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        yield max(d, d1), min(last, d2)
        d = last + timedelta(days=1)


def fetch_chunked(connector, fields, account, d1=START, d2=UPD):
    """Consultas largas troceadas por mes para evitar timeouts."""
    out = []
    for a, b in months(d1, d2):
        out += fetch(connector, fields, a, b, account)
    return out


n = lambda v: v or 0
LEAD = "actions_lead"  # leads de formularios de Meta + leads web del píxel (= "Resultados" en Ads Manager)
MK = {"Spain": "ES", "France": "FR", "Germany": "DE", "United States": "US",
      "Australia": "ANZ", "New Zealand": "ANZ"}
mk = lambda c: MK.get(c, "OT")


def allocate(groups, ig_by_key, fields, out, idx):
    """Suma métricas por (fecha, entidad, mercado) y reparte las visitas al perfil de IG
    (que Meta no desglosa por país) según la inversión de cada país ese día."""
    for (d, ent), rs in groups.items():
        ts = sum(n(x["spend"]) for x in rs)
        ti = sum(n(x["impressions"]) for x in rs)
        v = ig_by_key.get((d, ent), 0)
        for x in rs:
            w = n(x["spend"]) / ts if ts > 0 else (n(x["impressions"]) / ti if ti > 0 else 1 / len(rs))
            a = out[(d, idx[ent], mk(x["country"]))]
            for i, f in enumerate(fields):
                a[i] += n(x.get(f))
            a[len(fields)] += v * w
            a[len(fields) + 1] += n(x.get(LEAD))


# ---------------------------------------------------------------- Meta
def build_meta():
    A = CFG["meta_account"]
    OBJ = {"OUTCOME_SALES": "C", "OUTCOME_TRAFFIC": "T", "OUTCOME_ENGAGEMENT": "T", "OUTCOME_LEADS": "L",
           "OUTCOME_AWARENESS": "A"}
    BASE = ["spend", "impressions", "reach", "outbound_clicks_outbound_click", "actions_landing_page_view",
            "actions_purchase", "action_values_purchase", "instagram_profile_follow"]

    log("Meta: campañas diarias por país")
    raw = fetch_chunked("facebook", ["date", "campaign", "campaign_objective", "country"] + BASE + [LEAD], A)
    log("Meta: visitas al perfil de IG por anuncio")
    ig = fetch_chunked("facebook", ["date", "ad_id", "campaign", "instagram_profile_visits"], A)
    log("Meta: estado de campañas")
    st = fetch("facebook", ["campaign", "campaign_effective_status", "spend"], START, UPD, A)

    camps = {}
    for r in raw:
        o = OBJ.get(r.get("campaign_objective"))  # hay filas sin objetivo: se usa el que venga informado
        if o or r["campaign"] not in camps:
            camps[r["campaign"]] = o or camps.get(r["campaign"], "T")
    cl = sorted(camps)
    ci = {c: i for i, c in enumerate(cl)}

    igc = defaultdict(float)
    for r in ig:
        igc[(r["date"], r["campaign"])] += n(r["instagram_profile_visits"])
    grp = defaultdict(list)
    for r in raw:
        grp[(r["date"], r["campaign"])].append(r)
    agg = defaultdict(lambda: [0.0] * 10)
    allocate(grp, igc, BASE, agg, ci)
    rows = [[d, c, m] + [round(x, 2) for x in v] for (d, c, m), v in sorted(agg.items())]

    # Alcance deduplicado (Meta solo lo da sin la dimensión fecha)
    def reach(d1, d2, extra=()):
        return fetch("facebook", list(extra) + ["campaign", "country", "reach"], d1, d2, A)

    log("Meta: alcance por mes, año, semana y periodos")
    mreach = []
    for a, b in months(START, UPD):
        for r in reach(a, b):
            if r["campaign"] in ci and n(r["reach"]) > 0:
                mreach.append([a.strftime("%Y-%m"), ci[r["campaign"]], mk(r["country"]), int(r["reach"])])
    yreach = [[ci[r["campaign"]], mk(r["country"]), int(r["reach"])]
              for r in reach(START, UPD) if r["campaign"] in ci and n(r["reach"]) > 0]
    wreach = []
    for r in reach(START, UPD, ["year_week_iso"]):
        if r["campaign"] in ci and n(r["reach"]) > 0 and r.get("year_week_iso"):
            y, w = str(r["year_week_iso"]).split("|")
            wreach.append([f"{y}-W{int(w):02d}", ci[r["campaign"]], mk(r["country"]), int(r["reach"])])
    preach = {}
    for p in (7, 14, 30, 90):
        preach[str(p)] = [[ci[r["campaign"]], mk(r["country"]), int(r["reach"])]
                          for r in reach(max(START, UPD - timedelta(days=p - 1)), UPD)
                          if r["campaign"] in ci and n(r["reach"]) > 0]

    active = {r["campaign"] for r in st if r.get("campaign_effective_status") == "ACTIVE"}
    status = [1 if c in active else 0 for c in cl]

    # ---- anuncios
    log("Meta: anuncios diarios por país")
    AF = BASE + ["actions_video_view", "video_thruplay_watched_actions_video_view", LEAD]
    daily = fetch_chunked("facebook", ["date", "ad_id", "country"] + AF, A)
    log("Meta: datos de los anuncios (nombre, miniatura, destino)")
    meta = fetch("facebook", ["ad_id", "ad_name", "campaign", "adset_name", "effective_status", "thumbnail_url",
                              "website_destination_url", "link", "spend"], START, UPD, A)
    img = fetch("facebook", ["ad_id", "image_asset_url", "placement_ad_thumbnail_url", "carousel_card_media_urls",
                             "spend"], START, UPD, A)

    spend = defaultdict(float)
    vv = defaultdict(lambda: [0, 0])
    for r in daily:
        spend[r["ad_id"]] += n(r["spend"])
        vv[r["ad_id"]][0] += n(r["actions_video_view"])
        vv[r["ad_id"]][1] += n(r["impressions"])
    seen, ads_meta = set(), []
    for m in meta:  # una fila por anuncio
        if m["ad_id"] in spend and m["ad_id"] not in seen and m.get("campaign") in ci:
            seen.add(m["ad_id"]); ads_meta.append(m)
    ai = {m["ad_id"]: i for i, m in enumerate(ads_meta)}
    imgs = {}
    for r in img:
        imgs.setdefault(r["ad_id"], r)

    bad = lambda u: (not u) or ("Favicon" in u) or ("t39.30808-1" in u)  # logo de página / favicon

    def pick(m):
        if not bad(m.get("thumbnail_url")):
            return m["thumbnail_url"]
        r = imgs.get(m["ad_id"])
        if r:
            c = r.get("carousel_card_media_urls")
            c = re.split(r"[;,]\s*(?=https?://)", c)[0].strip() if c else None
            u = c or r.get("placement_ad_thumbnail_url")
            if u:
                return u
        return m.get("thumbnail_url") or ""

    key = lambda u: u.split("?")[0] if "fbcdn.net" in u else u.split("&")[0]
    want = {}
    for m in ads_meta:
        u = pick(m)
        if u:
            want.setdefault(key(u), u)
    log(f"Meta: descargando {len(want)} miniaturas")
    thumbs = {}

    def get(item):
        k, u = item
        try:
            b = requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).content
            im = Image.open(io.BytesIO(b)).convert("RGB")
            im.thumbnail((96, 96))
            o = io.BytesIO(); im.save(o, "JPEG", quality=72)
            return k, "data:image/jpeg;base64," + base64.b64encode(o.getvalue()).decode()
        except Exception:
            return k, None

    with cf.ThreadPoolExecutor(16) as ex:
        for k, v in ex.map(get, want.items()):
            if v:
                thumbs[k] = v
    tl = sorted(thumbs)
    ti = {k: i for i, k in enumerate(tl)}
    imgre = re.compile(r"(IMG|imagen|\.jpe?g|\.png|CARRUSEL)", re.I)
    ads = []
    for m in ads_meta:
        a = vv[m["ad_id"]]
        rate = a[0] / a[1] if a[1] else 0
        is_video = 1 if rate >= 0.02 and not imgre.search(m.get("ad_name") or "") else 0
        u = pick(m)
        ads.append([m.get("ad_name") or m["ad_id"], ci[m["campaign"]], 1 if m.get("effective_status") == "ACTIVE" else 0,
                    ti.get(key(u), -1) if u else -1, m.get("website_destination_url") or m.get("link") or "",
                    is_video, m.get("adset_name") or ""])

    igad = defaultdict(float)
    for r in ig:
        igad[(r["date"], r["ad_id"])] += n(r["instagram_profile_visits"])
    g2 = defaultdict(list)
    for r in daily:
        if r["ad_id"] in ai:
            g2[(r["date"], r["ad_id"])].append(r)
    ag = defaultdict(lambda: [0.0] * 12)
    # mismo reparto, con las métricas de vídeo al final
    for (d, a), rs in g2.items():
        ts = sum(n(x["spend"]) for x in rs); tim = sum(n(x["impressions"]) for x in rs)
        v = igad.get((d, a), 0)
        for x in rs:
            w = n(x["spend"]) / ts if ts > 0 else (n(x["impressions"]) / tim if tim > 0 else 1 / len(rs))
            g = ag[(d, ai[a], mk(x["country"]))]
            for i, f in enumerate(BASE):
                g[i] += n(x[f])
            g[8] += v * w
            g[9] += n(x["actions_video_view"])
            g[10] += n(x["video_thruplay_watched_actions_video_view"])
            g[11] += n(x.get(LEAD))
    adrows = [[d, a, m] + [round(x, 2) if i in (0, 6, 8) else int(round(x)) for i, x in enumerate(v)]
              for (d, a, m), v in sorted(ag.items()) if any(v)]

    return {"camps": [[c, camps[c]] for c in cl], "rows": rows, "mreach": mreach, "yreach": yreach,
            "wreach": wreach, "preach": preach, "status": status, "ads": ads, "adrows": adrows,
            "thumbs": [thumbs[k] for k in tl]}


# ---------------------------------------------------------------- Google Ads
def build_google():
    A = CFG["google_account"]
    PURCHASE = CFG["google_purchase_action"]
    TYPE = {"SEARCH": "Search", "PERFORMANCE_MAX": "PMax", "SHOPPING": "Shopping", "DISPLAY": "Display",
            "VIDEO": "YouTube", "DEMAND_GEN": "Demand Gen"}
    PFX = {"ES": "ES", "FR": "FR", "DE": "DE", "US": "US", "AU": "ANZ", "NZ": "ANZ"}
    gmk = lambda name: PFX.get(name[:2].upper(), "OT")

    log("Google: campañas diarias")
    camp = fetch_chunked("google_ads", ["date", "campaign", "advertising_channel_type", "campaign_status",
                                        "spend", "impressions", "clicks"], A)
    conv = fetch_chunked("google_ads", ["date", "campaign", "conversion_action_name", "conversions",
                                        "conversions_value"], A)
    log("Google: anuncios")
    ad = fetch_chunked("google_ads", ["date", "campaign", "ad_id", "spend", "impressions", "clicks"], A)
    adc = fetch_chunked("google_ads", ["date", "campaign", "ad_id", "conversion_action_name", "conversions",
                                       "conversions_value"], A)
    admeta = fetch("google_ads", ["campaign", "ad_id", "ad_group_name", "ad_name", "ad_final_urls", "ad_group_ad_status",
                                  "spend"], START, UPD, A)
    if not camp:
        return {"acc": A, "camps": [], "rows": [], "ads": [], "adrows": []}

    info = {}
    for r in sorted(camp, key=lambda r: r["date"]):
        info[r["campaign"]] = (r.get("advertising_channel_type") or "", r.get("campaign_status") or "")
    cl = sorted(info)
    ci = {c: i for i, c in enumerate(cl)}
    gc = [[c, 1 if info[c][1] == "ENABLED" else 0, gmk(c), TYPE.get(info[c][0], info[c][0].title())] for c in cl]

    cv = defaultdict(lambda: [0.0, 0.0])
    for r in conv:
        if r.get("conversion_action_name") == PURCHASE:
            k = (r["date"], r["campaign"]); cv[k][0] += n(r["conversions"]); cv[k][1] += n(r["conversions_value"])
    cagg = defaultdict(lambda: [0.0, 0, 0])
    for r in camp:
        a = cagg[(r["date"], r["campaign"])]
        a[0] += n(r["spend"]); a[1] += n(r["impressions"]); a[2] += n(r["clicks"])
    rows = sorted([d, ci[c], round(v[0], 2), int(v[1]), int(v[2]), round(cv[(d, c)][0], 4), round(cv[(d, c)][1], 2)]
                  for (d, c), v in cagg.items())

    akey = lambda r: str(r["ad_id"]) if r.get("ad_id") else "PMAX:" + r["campaign"]
    mt = {}
    for r in admeta:
        if r.get("ad_id"):
            mt.setdefault(str(r["ad_id"]), r)
    keys = sorted({akey(r) for r in ad if r["campaign"] in ci})
    ai = {k: i for i, k in enumerate(keys)}
    acamp = {akey(r): r["campaign"] for r in ad}
    url_re = re.compile(r"https?://[^\s'\"\],]+")
    gads = []
    for k in keys:
        c = acamp[k]; typ = gc[ci[c]][3]
        if k.startswith("PMAX:"):
            gads.append(["Performance Max · " + c, ci[c], gc[ci[c]][1], "PMax", "",
                         "Grupos de recursos (sin desglose por anuncio)"])
            continue
        m = mt.get(k, {})
        urls = url_re.findall(str(m.get("ad_final_urls") or ""))
        grp = m.get("ad_group_name") or ""
        short = " | ".join(str(m.get("ad_name") or "").split(" | ")[:3]).strip()
        name = CFG["google_ad_names"].get(k) or short or (f"{typ} · {grp}" if grp else f"{typ} · {k}")
        active = 0 if m.get("ad_group_ad_status") in ("PAUSED", "REMOVED") else 1
        gads.append([name, ci[c], active, typ, urls[0] if urls else "",
                     "Grupo de anuncios" if typ == "Search" else ("Grupo: " + grp if grp else "")])

    ac = defaultdict(lambda: [0.0, 0.0])
    for r in adc:
        if r.get("conversion_action_name") == PURCHASE:
            kk = (r["date"], akey(r)); ac[kk][0] += n(r["conversions"]); ac[kk][1] += n(r["conversions_value"])
    aagg = defaultdict(lambda: [0.0, 0, 0])
    for r in ad:
        if r["campaign"] in ci:
            a = aagg[(r["date"], akey(r))]
            a[0] += n(r["spend"]); a[1] += n(r["impressions"]); a[2] += n(r["clicks"])
    arows = sorted([d, ai[k], gmk(acamp[k]), round(v[0], 2), int(v[1]), int(v[2]),
                    round(ac[(d, k)][0], 4), round(ac[(d, k)][1], 2)] for (d, k), v in aagg.items())
    return {"acc": A, "camps": gc, "rows": rows, "ads": gads, "adrows": arows}


# ---------------------------------------------------------------- main
def main():
    if not KEY:
        sys.exit("Falta la variable de entorno WINDSOR_API_KEY")
    log(f"Periodo: {START} → {UPD}")
    data = build_meta()
    data["g"] = build_google()
    data["updated"] = iso(UPD)
    data["start"] = iso(START)

    # Controles de calidad: si algo viene vacío, no se publica (queda la versión de ayer)
    ms = sum(r[3] for r in data["rows"]); gs = sum(r[2] for r in data["g"]["rows"])
    ads_s = sum(r[3] for r in data["adrows"])
    log(f"Meta {ms:.2f} € ({len(data['camps'])} campañas, {len(data['ads'])} anuncios) · Google {gs:.2f} €")
    if ms <= 0:
        sys.exit("ERROR: Meta devuelve 0 € de inversión; no se publica.")
    if abs(ads_s - ms) > max(5, ms * 0.01):
        log(f"AVISO: el total de anuncios ({ads_s:.2f}) no cuadra con el de campañas ({ms:.2f})")

    tpl = open(os.path.join(ROOT, "template.html"), encoding="utf-8").read()
    html = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", "index.html")
    open(out, "w", encoding="utf-8").write(html)
    log(f"OK → {out} ({len(html) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
