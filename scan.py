#!/usr/bin/env python3
"""
SteamT-Data — облачный сборщик цен значков Steam.

Сканирует торговую площадку Steam (все обычные коллекционные карточки, по 100 за запрос),
бережно соблюдая лимит Steam (~100 запросов, потом пауза). Цены копятся в state.json.gz,
каждые ~20 минут публикуется catalog.tsv.gz — его скачивает приложение SteamT.
Скан непрерывный: каждый запуск продолжает с места, где остановился прошлый.
"""
import gzip, json, os, subprocess, sys, time, urllib.parse, urllib.request, urllib.error

PAGE = 100          # карточек за запрос (максимум Steam)
STEP = 85           # шаг страниц меньше размера -> страницы перекрываются на 15 карточек,
                    # поэтому сдвиг списка во время многочасового скана не «проглатывает» карточки
KEEP_MS = 3 * 24 * 3600 * 1000   # карточку удаляем, только если её не видно 3 суток
MIN_SET = 5         # в наборе Steam минимум 5 карточек — меньше значит набор собран не полностью
VERIFY_AGE = 24 * 3600 * 1000    # каждую игру перепроверяем отдельным запросом раз в сутки
VERIFY_RATIO = 2                 # на 1 страницу общего скана — 2 перепроверки игр
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


def fetch_page(start, sort=4, app=None):
    filters = {"item_class": ["item_class_2"], "cardborder": ["cardborder_0"]}
    if app:
        filters["Game"] = ["app_%d" % app]
    q = [{
        "appid": 753, "strQuery": "",
        "filters": filters,
        "price": {"eCurrency": 1}, "accessoryFilters": {},
        "sort": sort, "direction": 1, "start": start, "count": PAGE,
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


def store(cards, results):
    now = int(time.time() * 1000)
    n = 0
    for r in results:
        h = r["strHash"]
        try:
            appid = int(h.split("-", 1)[0])
        except ValueError:
            continue
        ad = r.get("asset_description", {})
        cards[h] = [appid, game_name(ad.get("type", "")), ad.get("name", ""),
                    price(r.get("strMinSellSubtotal", "")), int(r.get("cSellOrders", 0)), now]
        n += 1
    return n


def verify_game(cards, state, appid, results):
    """Точный состав набора игры: обновляем найденные, пропавшие с маркета помечаем 0 в продаже."""
    now = int(time.time() * 1000)
    seen = set()
    store(cards, results)
    for r in results:
        seen.add(r["strHash"])
    for h, v in cards.items():
        if v[0] == appid and h not in seen:
            v[4] = 0
            v[5] = now        # не удалять: карточка есть в наборе, просто сейчас никто не продаёт
    state.setdefault("verified", {})[str(appid)] = now


def build_verify_queue(cards, state, limit=300):
    """Сначала самые дешёвые значки — неполные наборы выглядят дешёвыми и всплывают наверх."""
    now = time.time() * 1000
    ver = state.get("verified", {})
    totals = {}
    for h, v in cards.items():
        totals[v[0]] = totals.get(v[0], 0.0) + v[3]
    cand = [a for a in totals if now - ver.get(str(a), 0) > VERIFY_AGE]
    cand.sort(key=lambda a: totals[a])
    return cand[:limit]


def clean(s):
    return s.replace("\t", " ").replace("\n", " ")


def build_catalog(state):
    games = {}
    for h, (appid, game, name, usd, listings, seen) in state["cards"].items():
        g = games.setdefault(appid, [game, []])
        g[1].append((name, h, usd, listings))
    ver = state.get("verified", {})
    lines = ["T\t%d" % int(time.time() * 1000)]
    for appid, (game, cards) in games.items():
        lines.append("G\t%d\t%s\t%d" % (appid, clean(game), int(ver.get(str(appid), 0))))
        for name, h, usd, listings in cards:
            lines.append("C\t%s\t%s\t%s\t%d" % (clean(name), clean(h), repr(usd), listings))
    with gzip.open("catalog.tsv.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\n".join(lines) + "\n")
    now = time.time() * 1000
    meta = {"updated": int(now), "games": len(games), "cards": len(state["cards"]),
            "verified_24h": sum(1 for a in games if now - ver.get(str(a), 0) < VERIFY_AGE),
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
    if state.get("cycle") and "sort" not in state["cycle"]:
        state["cycle"] = None   # цикл старого формата (без перекрытия страниц) — начинаем заново
    last_pub = time.time()
    wait = 180
    requests = 0
    while time.time() < DEADLINE:
        cyc = state.get("cycle")
        # 1) перепроверка игр по одной (точный состав набора), чередуется с общим сканом
        if state.get("vstep", 0) < VERIFY_RATIO:
            q = state.get("vqueue") or []
            if not q:
                q = state["vqueue"] = build_verify_queue(cards, state)
                if q:
                    log("очередь перепроверки:", len(q), "игр (от самых дешёвых)")
            if q:
                app = q[0]
                try:
                    d = fetch_page(0, sort=3, app=app)
                    requests += 1
                    wait = 180
                except Limited:
                    log("лимит Steam (перепроверка), жду", wait, "с")
                    if time.time() - last_pub > PUBLISH_EVERY:
                        publish(state); last_pub = time.time()
                    time.sleep(wait); wait = min(int(wait * 1.5), 600)
                    continue
                verify_game(cards, state, app, d.get("results", []))
                q.pop(0)
                state["vstep"] = state.get("vstep", 0) + 1
                if MAX_PAGES and requests >= MAX_PAGES:
                    break
                if time.time() - last_pub > PUBLISH_EVERY:
                    publish(state); last_pub = time.time()
                time.sleep(0.8)
                continue
        state["vstep"] = 0

        if not cyc:
            sort = 3 if state.get("cycles", 0) % 2 else 4   # чередуем порядок обхода между циклами
            try:
                d = fetch_page(0, sort=sort)
            except Limited:
                log("лимит Steam, жду", wait, "с"); time.sleep(wait); wait = min(int(wait * 1.5), 600); continue
            total = (d["total_count"] + STEP - 1) // STEP
            cyc = state["cycle"] = {"total": total, "done": [], "started": int(time.time() * 1000), "sort": sort}
            log("новый цикл:", total, "страниц, сортировка", sort)
        todo = [p for p in range(cyc["total"]) if p not in set(cyc["done"])]
        if not todo:
            # цикл завершён: убираем карточки, которых больше нет на маркете
            before = len(cards)
            limit = int(time.time() * 1000) - KEEP_MS
            for h in [h for h, v in cards.items() if v[5] < limit]:
                del cards[h]
            state["last_complete"] = int(time.time() * 1000)
            state["cycles"] = state.get("cycles", 0) + 1
            state["cycle"] = None
            state["vqueue"] = []      # после цикла — заново выбрать игры для перепроверки
            log("цикл завершён, удалено устаревших:", before - len(cards))
            publish(state); last_pub = time.time()
            continue
        p = todo[0]
        try:
            d = fetch_page(p * STEP, sort=cyc.get("sort", 4))
            requests += 1
            wait = 180
        except Limited:
            log("лимит Steam после", requests, "запросов, жду", wait, "с")
            if time.time() - last_pub > PUBLISH_EVERY:
                publish(state); last_pub = time.time()
            time.sleep(wait); wait = min(int(wait * 1.5), 600)
            continue
        store(cards, d.get("results", []))
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
