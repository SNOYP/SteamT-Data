#!/usr/bin/env python3
"""
SteamT-Data — облачный сборщик цен значков Steam.

Сканирует торговую площадку Steam (все обычные коллекционные карточки, по 100 за запрос),
бережно соблюдая лимит Steam (~100 запросов, потом пауза). Цены копятся в state.json.gz,
каждые ~20 минут публикуется catalog.tsv.gz — его скачивает приложение SteamT.
Скан непрерывный: каждый запуск продолжает с места, где остановился прошлый.
"""
import gzip, json, os, subprocess, sys, time, urllib.parse, urllib.request, urllib.error

PAGE = 100
DEADLINE = time.time() + float(os.environ.get("RUN_SECONDS", 5 * 3600 + 20 * 60))
PUBLISH_EVERY = float(os.environ.get("PUBLISH_EVERY", 20 * 60))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "0"))      # для теста
NO_UPLOAD = os.environ.get("NO_UPLOAD") == "1"
TAG = "data"
UA = "SteamT-Data/1.0 (+https://github.com/SNOYP/SteamT-Data)"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def download_state():
    if NO_UPLOAD:
        return
    r = gh("release", "download", TAG, "-p", "state.json.gz", "-O", "state.json.gz", "--clobber", check=False)
    log("state download:", "ok" if r.returncode == 0 else "нет (первый запуск)")


def load_state():
    if os.path.exists("state.json.gz"):
        with gzip.open("state.json.gz", "rt", encoding="utf-8") as f:
            return json.load(f)
    return {"cards": {}, "cycle": None, "last_complete": 0}


class Limited(Exception):
    pass


def fetch_page(start):
    q = [{
        "appid": 753, "strQuery": "",
        "filters": {"item_class": ["item_class_2"], "cardborder": ["cardborder_0"]},
        "price": {"eCurrency": 1}, "accessoryFilters": {},
        "sort": 4, "direction": 1, "start": start, "count": PAGE,
    }]
    url = "https://steamcommunity.com/market/actions?q=Search&qp=" + urllib.parse.quote(json.dumps(q, separators=(",", ":")))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))["data"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise Limited()
            log("HTTP", e.code, "повтор")
        except Exception as e:  # сеть
            log("ошибка сети:", e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("маркет не отвечает")


def price(s):
    c = "".join(ch for ch in s if ch.isdigit() or ch in ",.")
    if "," in c and "." in c:
        c = c.replace(".", "").replace(",", ".") if c.rfind(",") > c.rfind(".") else c.replace(",", "")
    else:
        c = c.replace(",", ".")
    try:
        return float(c)
    except ValueError:
        return 0.0


def game_name(t):
    t = t.strip()
    if t.endswith(" Trading Card"):
        return t[:-len(" Trading Card")].strip()
    if t.startswith("Коллекционная карточка из "):
        return t[len("Коллекционная карточка из "):].strip()
    i = t.find(" — ")
    if i >= 0:
        return t[i + 3:].strip()
    return t.replace("Trading Card", "").replace("Коллекционная карточка", "").strip(" -—")


def clean(s):
    return s.replace("\t", " ").replace("\n", " ")


def build_catalog(state):
    games = {}
    for h, (appid, game, name, usd, listings, seen) in state["cards"].items():
        g = games.setdefault(appid, [game, []])
        g[1].append((name, h, usd, listings))
    lines = ["T\t%d" % int(time.time() * 1000)]
    for appid, (game, cards) in games.items():
        lines.append("G\t%d\t%s" % (appid, clean(game)))
        for name, h, usd, listings in cards:
            lines.append("C\t%s\t%s\t%s\t%d" % (clean(name), clean(h), repr(usd), listings))
    with gzip.open("catalog.tsv.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\n".join(lines) + "\n")
    meta = {"updated": int(time.time() * 1000), "games": len(games), "cards": len(state["cards"]),
            "last_complete": state.get("last_complete", 0),
            "cycle_done": len(state["cycle"]["done"]) if state.get("cycle") else 0,
            "cycle_total": state["cycle"]["total"] if state.get("cycle") else 0}
    with open("meta.json", "w") as f:
        json.dump(meta, f)
    return meta


def publish(state):
    with gzip.open("state.json.gz", "wt", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    meta = build_catalog(state)
    log("публикую:", meta)
    if not NO_UPLOAD:
        gh("release", "upload", TAG, "catalog.tsv.gz", "meta.json", "state.json.gz", "--clobber")


def main():
    download_state()
    state = load_state()
    cards = state["cards"]
    last_pub = time.time()
    wait = 180
    requests = 0
    while time.time() < DEADLINE:
        cyc = state.get("cycle")
        if not cyc:
            try:
                d = fetch_page(0)
            except Limited:
                log("лимит Steam, жду", wait, "с"); time.sleep(wait); wait = min(int(wait * 1.5), 600); continue
            total = (d["total_count"] + PAGE - 1) // PAGE
            cyc = state["cycle"] = {"total": total, "done": [], "started": int(time.time() * 1000)}
            log("новый цикл:", total, "страниц")
        todo = [p for p in range(cyc["total"]) if p not in set(cyc["done"])]
        if not todo:
            # цикл завершён: убираем карточки, которых больше нет на маркете
            before = len(cards)
            for h in [h for h, v in cards.items() if v[5] < cyc["started"]]:
                del cards[h]
            state["last_complete"] = int(time.time() * 1000)
            state["cycle"] = None
            log("цикл завершён, удалено устаревших:", before - len(cards))
            publish(state); last_pub = time.time()
            continue
        p = todo[0]
        try:
            d = fetch_page(p * PAGE)
            requests += 1
            wait = 180
        except Limited:
            log("лимит Steam после", requests, "запросов, жду", wait, "с")
            if time.time() - last_pub > PUBLISH_EVERY:
                publish(state); last_pub = time.time()
            time.sleep(wait); wait = min(int(wait * 1.5), 600)
            continue
        now = int(time.time() * 1000)
        for r in d.get("results", []):
            h = r["strHash"]
            try:
                appid = int(h.split("-", 1)[0])
            except ValueError:
                continue
            ad = r.get("asset_description", {})
            cards[h] = [appid, game_name(ad.get("type", "")), ad.get("name", ""),
                        price(r.get("strMinSellSubtotal", "")), int(r.get("cSellOrders", 0)), now]
        cyc["done"].append(p)
        if len(cyc["done"]) % 25 == 0:
            log("цикл:", len(cyc["done"]), "/", cyc["total"], "карточек:", len(cards))
        if MAX_PAGES and requests >= MAX_PAGES:
            break
        if time.time() - last_pub > PUBLISH_EVERY:
            publish(state); last_pub = time.time()
        time.sleep(0.8)
    publish(state)
    log("готово, запросов:", requests)


if __name__ == "__main__":
    main()
