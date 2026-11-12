#!/usr/bin/env python3
"""Ищет сервер WARDOGS по карте, онлайну и минуте матча и заходит на него через Steam.

Данные: публичный список https://wardogserverlist.com/api/servers (сайт читает браузер
серверов игры раз в ~5 минут, CDN держит ответ ещё 90 с). С 7 ноября 2026 он отвечает только
с API-ключом: ключ делается бесплатно на https://wardogserverlist.com/account#api-keys (вход
через Discord, тип «server key») и кладётся в wardogs_api_key.txt рядом с программой или в
переменную окружения WARDOGS_API_KEY. Для официальных серверов есть
источник свежее — wardogsnow.com (раз в ~минуту), его подмешивает окно wardogs_gui.pyw.
Вход: публичная страница сайта /join?key=... отдаёт ссылку steam://joinlobby/1867240/<лобби>;
Steam по ней запускает игру или говорит уже запущенной игре зайти на сервер.

Примеры:
  python3 wardogs_finder.py --once --map ozeti --region eu-east,eu-central
  python3 wardogs_finder.py --map bakurani --region eu --min-players 60 --minutes 0-10 --join
"""
import argparse
import gzip
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://wardogserverlist.com/api/servers"
JOIN_PAGE_URL = "https://wardogserverlist.com/join"
API_KEYS_PAGE_URL = "https://wardogserverlist.com/account#api-keys"
API_KEY_ENV = "WARDOGS_API_KEY"
API_KEY_PATH = Path(__file__).resolve().with_name("wardogs_api_key.txt")
USER_AGENT = "wardogs-server-finder/1.0 (+https://github.com/drapik/wardogs-server-finder)"
STEAM_LOBBY_LINK = re.compile(r"steam://joinlobby/1867240/\d+")
# Раунды идут до ~4 часов; старше суток — протухшая отметка, а не текущий матч.
MAX_ROUND_MINUTES = 24 * 60

# В списке карта лежит в поле world; названия из браузера игры — справа.
MAPS = {
    "Kavkazi": "Bakurani",
    "Europe": "Ozeti",
    "NorthAmerica": "Zestafona",
    "Russia": "Sabatulo",
}
MAP_ALIASES = {
    "bakurani": "Kavkazi", "kavkazi": "Kavkazi", "бакурани": "Kavkazi",
    "ozeti": "Europe", "europe": "Europe", "madrid": "Europe", "озети": "Europe",
    "zestafona": "NorthAmerica", "northamerica": "NorthAmerica", "detroit": "NorthAmerica",
    "зестафона": "NorthAmerica",
    # wardogsnow подписывает Sabatulo внутренним именем Moscow.
    "sabatulo": "Russia", "russia": "Russia", "moscow": "Russia", "сабатуло": "Russia",
}
REGIONS = [
    "eu-central", "eu-east", "eu-west", "eu-south",
    "na-central", "na-north", "na-east", "na-west",
    "south-america", "asia-east", "asia-west", "asia-se", "oceania", "south-africa",
]


def parse_maps(value):
    worlds = set()
    for name in value.split(","):
        key = name.strip().lower().replace(" ", "")
        if key not in MAP_ALIASES:
            raise argparse.ArgumentTypeError(f"неизвестная карта {name!r}; есть: {', '.join(MAPS.values())}")
        worlds.add(MAP_ALIASES[key])
    return worlds


def parse_regions(value):
    regions = set()
    for name in value.split(","):
        key = name.strip().lower()
        # "eu" или "na" — все регионы с этим префиксом.
        found = [r for r in REGIONS if r == key or r.startswith(key + "-")]
        if not found:
            raise argparse.ArgumentTypeError(f"неизвестный регион {name!r}; есть: eu, na, {', '.join(REGIONS)}")
        regions.update(found)
    return regions


def parse_minutes(value):
    m = re.fullmatch(r"\s*(\d+)?\s*-\s*(\d+)?\s*", value)
    if not m or (m.group(1) is None and m.group(2) is None):
        raise argparse.ArgumentTypeError("формат минут: 5-15, 0-10, 30- или -20")
    lo = int(m.group(1)) if m.group(1) else 0
    hi = int(m.group(2)) if m.group(2) else MAX_ROUND_MINUTES
    if lo > hi:
        raise argparse.ArgumentTypeError("начало диапазона минут больше конца")
    return lo, hi


def http_get(url, accept_gzip=False, extra_headers=None):
    headers = {"User-Agent": USER_AGENT, **(extra_headers or {})}
    if accept_gzip:
        headers["Accept-Encoding"] = "gzip"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return body


class ApiKeyError(Exception):
    """Список не отдают из-за ключа: повтор с тем же ключом не поможет, нужен другой."""


def load_api_key():
    """Ключ из WARDOGS_API_KEY или wardogs_api_key.txt; пустая строка — ключа нет."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    try:
        # utf-8-sig: Блокнот Windows может дописать BOM в начало файла.
        return API_KEY_PATH.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def save_api_key(key):
    API_KEY_PATH.write_text(key.strip() + "\n", encoding="utf-8")


def api_error_text(exc):
    """Текст для человека по ответу 401/403 сайта (тело — JSON с error и message)."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:
        body = {}
    code, message = body.get("error", ""), body.get("message", "")
    if exc.code == 401 and code == "api_key_required":
        text = "сайт требует API-ключ, а он не задан"
    elif exc.code == 401:
        text = "сайт не принял API-ключ: он неверный или отозван"
    else:
        text = "ключ не подходит для программы: нужен тип «server key», а не «browser key»"
    return f"{text} (HTTP {exc.code}{': ' + message if message else ''})"


def fetch_servers(api_key=None):
    key = api_key or load_api_key()
    if not key:
        raise ApiKeyError(f"нет API-ключа wardogserverlist: сделай его на {API_KEYS_PAGE_URL} "
                          f"и положи в {API_KEY_PATH.name}")
    try:
        return json.loads(http_get(API_URL, accept_gzip=True, extra_headers={"X-API-Key": key}))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ApiKeyError(api_error_text(exc)) from None
        raise


WARDOGSNOW_OFFICIAL_URL = "https://wardogsnow.com/servers/official"
WARDOGSNOW_ROW = re.compile(r'<li class="item brw-grid">(.*?)</li>', re.S)


def parse_match_text(text):
    """'1h 39m' / '24m' -> минуты; всё остальное -> None."""
    m = re.fullmatch(r"\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*", text)
    if not m or not any(m.groups()):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def fetch_official_live(region, has_space, min_players, max_pages=12):
    """Официальные сервера региона с wardogsnow.com: он обновляется раз в ~минуту, а не раз в 5.

    Страницы отсортированы по онлайну, поэтому листаем, пока игроков не станет меньше min_players.
    """
    rows = []
    for page in range(1, max_pages + 1):
        query = {"zone": region, "sort": "players", "page": page}
        if has_space:
            query["space"] = 1
        text = http_get(f"{WARDOGSNOW_OFFICIAL_URL}?{urllib.parse.urlencode(query)}").decode("utf-8", "replace")
        for item in WARDOGSNOW_ROW.findall(text):
            cells = [html.unescape(c).strip() for c in re.findall(r">([^<>]+)<", item) if c.strip()]
            number = re.search(r"#(\d+)", cells[0]) if cells else None
            players = next((re.match(r"(\d+)\s*/\s*(\d+)$", c) for c in cells if re.match(r"\d+\s*/\s*\d+$", c)), None)
            if not number or not players:
                continue
            rows.append({
                "number": int(number.group(1)),
                "world": next((MAP_ALIASES[c.lower()] for c in cells if c.lower() in MAP_ALIASES), None),
                "players": int(players.group(1)),
                "maxPlayers": int(players.group(2)),
                "minutes": next((parse_match_text(c) for c in cells if parse_match_text(c) is not None), None),
            })
        if 'rel="next"' not in text or (rows and rows[-1]["players"] < min_players):
            break
    return rows


def merge_official_live(data, region, rows, fetched_at):
    """Кладёт свежие онлайн, карту и начало раунда официальных серверов поверх 5-минутного списка."""
    index = {s.get("serverNumber"): s for s in data.get("servers", [])
             if s.get("official") and s.get("region") == region}
    for row in rows:
        server = index.get(row["number"])
        if server is None:
            continue
        server["players"], server["maxPlayers"] = row["players"], row["maxPlayers"]
        if row["minutes"] is not None:
            old = match_minutes(server, fetched_at * 1000)
            changed_map = row["world"] is not None and row["world"] != server.get("world")
            # wardogsnow округляет до минуты вниз; расхождение больше 2 минут — это уже новый раунд.
            if changed_map or old is None or old - row["minutes"] > 2:
                server["matchStart"] = int((fetched_at - row["minutes"] * 60 - 30) * 1000)
        if row["world"] is not None:
            server["world"] = row["world"]
        server["liveAt"] = fetched_at


def match_minutes(server, now_ms):
    start = server.get("matchStart")
    if not start:
        return None
    minutes = (now_ms - start) / 60000
    return minutes if 0 <= minutes <= MAX_ROUND_MINUTES else None


def is_infantry(server):
    return any(str(t).split(".")[-1] == "InfantryOnly" for t in server.get("tags") or [])


def passes_static(server, args):
    """Всё, кроме минуты матча: она проверяется отдельно, чтобы показывать «скоро подойдёт»."""
    if args.maps and server.get("world") not in args.maps:
        return False
    if args.regions and server.get("region") not in args.regions:
        return False
    if args.kind == "official" and not server.get("official"):
        return False
    if args.kind == "community" and server.get("official"):
        return False
    if server.get("passworded") and not args.passworded:
        return False
    if args.infantry == "only" and not is_infantry(server):
        return False
    if args.infantry == "no" and is_infantry(server):
        return False
    if args.name and args.name.lower() not in (server.get("name") or "").lower():
        return False
    players, max_players = server.get("players", 0), server.get("maxPlayers", 0)
    if players < args.min_players:
        return False
    if args.max_players is not None and players > args.max_players:
        return False
    if not args.allow_full and players >= max_players:
        return False
    return True


def find(data, args):
    """Возвращает (подходящие сейчас, войдут в окно минут позже), с минутой матча."""
    now_ms = time.time() * 1000
    lo, hi = args.minutes
    ready, upcoming = [], []
    for server in data.get("servers", []):
        if not passes_static(server, args):
            continue
        minutes = match_minutes(server, now_ms)
        if args.minutes_set:
            if minutes is None:
                continue
            if lo <= minutes <= hi:
                ready.append((server, minutes))
            elif minutes < lo:
                upcoming.append((server, minutes))
        else:
            ready.append((server, minutes))
    ready.sort(key=lambda sm: (-sm[0].get("players", 0), sm[1] if sm[1] is not None else 1e9))
    upcoming.sort(key=lambda sm: lo - sm[1])
    return ready, upcoming


def fmt_minutes(minutes):
    if minutes is None:
        return "    ?"
    h, m = divmod(int(minutes), 60)
    return f"{h}:{m:02d}" if h else f"{m:>4}м"


def fmt_row(server, minutes):
    world = server.get("world")
    tag = " [пехота]" if is_infantry(server) else ""
    return (f"{server.get('players', 0):>3}/{server.get('maxPlayers', 0):<3} "
            f"{fmt_minutes(minutes):>6}  {MAPS.get(world, world):<10} {server.get('region', ''):<13} "
            f"{server.get('name', '')}{tag}")


def print_table(ready, upcoming, args, limit):
    stamp = time.strftime("%H:%M:%S")
    if not ready:
        print(f"[{stamp}] подходящих серверов нет")
    else:
        print(f"[{stamp}] подходит: {len(ready)}")
        print(" игроки  матч   карта      регион        сервер")
        for server, minutes in ready[:limit]:
            print(" " + fmt_row(server, minutes))
    if args.minutes_set and upcoming:
        lo = args.minutes[0]
        print("  скоро войдут в окно минут:")
        for server, minutes in upcoming[:3]:
            print(f"   через {max(1, math.ceil(lo - minutes)):>3} мин: " + fmt_row(server, minutes))
    sys.stdout.flush()


def steam_join_link(server):
    query = urllib.parse.urlencode({"key": server["serverKey"]})
    try:
        page = http_get(f"{JOIN_PAGE_URL}?{query}").decode("utf-8", "replace")
    except Exception:  # 404 — сервер уже пропал из списка
        return None
    found = STEAM_LOBBY_LINK.search(page)
    return found.group(0) if found else None


def open_url(url):
    if sys.platform.startswith("win"):
        os.startfile(url)
    elif sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        subprocess.run(["xdg-open", url], check=False)


def alert(text):
    print("\a", end="")
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "WARDOGS"'], check=False)
        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
    elif sys.platform.startswith("win"):
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def join(candidates):
    """Заходит на первый сервер, для которого сайт отдал лобби Steam; False — ни на один."""
    for server, _ in candidates[:5]:
        name = server.get("name", "")
        link = steam_join_link(server)
        if not link:
            print(f"    нет ссылки входа для {name}, пробую следующий")
            continue
        print(f"==> захожу: {name}\n    {link}")
        open_url(link)
        alert(f"Захожу: {name}")
        return True
    return False


def main(argv=None):
    # В названиях серверов бывают ★ и прочее, чего нет в cp1251 консоли Windows.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description="Поиск и автоподключение к серверам WARDOGS")
    p.add_argument("--map", dest="maps", type=parse_maps, default=set(),
                   help="карта(ы) через запятую: " + ", ".join(t.lower() for t in MAPS.values()))
    p.add_argument("--region", dest="regions", type=parse_regions, default=set(),
                   help="регион(ы): eu, eu-east, eu-central, na, ...")
    p.add_argument("--min-players", type=int, default=1, help="минимум игроков (по умолчанию 1)")
    p.add_argument("--max-players", type=int, default=None, help="максимум игроков")
    p.add_argument("--minutes", type=parse_minutes, default=None,
                   help="минута матча, диапазон: 0-10, 30-45, 60- (от 60), -5 (до 5)")
    p.add_argument("--kind", choices=["any", "official", "community"], default="any")
    p.add_argument("--infantry", choices=["any", "only", "no"], default="any",
                   help="режим «только пехота»")
    p.add_argument("--name", help="подстрока в названии сервера")
    p.add_argument("--allow-full", action="store_true", help="не пропускать полные сервера (встанешь в очередь)")
    p.add_argument("--passworded", action="store_true", help="не пропускать сервера с паролем")
    p.add_argument("--join", action="store_true", help="зайти на первый подходящий сервер и выйти")
    p.add_argument("--once", action="store_true", help="один проход: показать список и выйти")
    p.add_argument("--interval", type=int, default=60, help="пауза между проверками, с (не меньше 30)")
    p.add_argument("--limit", type=int, default=15, help="сколько строк показывать")
    p.add_argument("--api-key", default=None,
                   help=f"ключ wardogserverlist (иначе берётся из {API_KEY_ENV} или {API_KEY_PATH.name})")
    args = p.parse_args(argv)
    args.minutes_set = args.minutes is not None
    args.minutes = args.minutes or (0, MAX_ROUND_MINUTES)
    interval = max(30, args.interval)
    had_ready = False

    while True:
        try:
            ready, upcoming = find(fetch_servers(args.api_key), args)
        except ApiKeyError as exc:  # с этим ключом ждать бесполезно
            print(f"[{time.strftime('%H:%M:%S')}] {exc}")
            return 3
        except Exception as exc:  # сеть/сайт лежит — просто ждём следующего круга
            print(f"[{time.strftime('%H:%M:%S')}] не удалось получить список: {exc}")
            ready, upcoming = None, []
        if ready is not None:
            print_table(ready, upcoming, args, args.limit)
            if ready and args.join and join(ready):
                return 0
            if args.once:
                return 0 if ready else 1
            # Без --join сигналим, только когда подходящие появились, а не каждый круг.
            if ready and not had_ready:
                alert(f"Нашлось серверов: {len(ready)}")
            had_ready = bool(ready)
        elif args.once:
            return 2
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nостановлено")
