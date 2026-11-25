# SteamT-Data

Облачный сборщик цен значков Steam для приложения **SteamT** (by _SNOYP_).

GitHub Actions круглосуточно сканирует торговую площадку Steam (все обычные коллекционные карточки),
соблюдая лимиты Steam, и каждые ~20 минут публикует свежие цены в релиз `data`:

- `catalog.tsv.gz` — все игры, карточки и минимальные цены (USD)
- `meta.json` — время обновления и прогресс текущего цикла

---

Cloud collector of Steam badge prices for the **SteamT** app. GitHub Actions continuously scans the
Steam Community Market (all regular trading cards), respecting Steam rate limits, and publishes
fresh prices to the `data` release every ~20 minutes.
