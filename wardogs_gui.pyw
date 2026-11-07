#!/usr/bin/env python3
"""Окно поиска серверов WARDOGS: фильтры, живая таблица и автозаход через Steam.

Список серверов запрашивается каждые 5 секунд (источник и фильтры — из wardogs_finder.py),
а таймер матча тикает каждую секунду локально от времени начала раунда.
Запуск на Windows: двойной клик по файлу (.pyw открывается без консоли).
"""
import datetime
import json
import sys
import threading
import time
import tkinter as tk
import types
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wardogs_finder as w  # noqa: E402

POLL_SECONDS = 5
# Ключ не приняли — с тем же ключом не долбим сайт; новый ключ из окна будит опрос сразу.
KEY_RETRY_SECONDS = 60
# wardogsnow сам обновляется раз в ~минуту: спрашивать чаще бессмысленно и грузит фан-сайт.
LIVE_SECONDS = 60
LIVE_MAX_AGE = 180
MAX_ROWS = 200
SETTINGS_PATH = Path(__file__).with_name("wardogs_gui.json")

REGION_NAMES = {
    "eu-east": "EU Восток", "eu-central": "EU Центр", "eu-west": "EU Запад", "eu-south": "EU Юг",
    "na-east": "NA Восток", "na-central": "NA Центр", "na-west": "NA Запад", "na-north": "NA Север",
    "south-america": "Юж. Америка", "asia-east": "Азия Восток", "asia-west": "Азия Запад",
    "asia-se": "Азия ЮВ", "oceania": "Океания", "south-africa": "Юж. Африка",
}
KINDS = {"все": "any", "официальные": "official", "комьюнити": "community"}
INFANTRY = {"любой": "any", "только пехота": "only", "без пехотных": "no"}

C = {
    "bg": "#15171c", "panel": "#1d2027", "field": "#262a33", "line": "#30353f",
    "fg": "#e8eaee", "dim": "#8b919c", "accent": "#ff5e1f", "accent_dim": "#7a3a20",
    "ok_bg": "#163824", "ok_fg": "#86ffb0", "soon_bg": "#3a3116", "soon_fg": "#ffd27a",
    "past_fg": "#6f7580", "sel": "#2c4a7c", "err": "#ff6b6b",
}

DEFAULTS = {
    "maps": [], "regions": ["eu-east", "eu-central"],
    "min_players": 50, "max_players": 100,
    "use_minutes": True, "min_from": 0, "min_to": 10,
    "kind": "все", "infantry": "любой", "name": "",
    "allow_full": False, "passworded": False, "sound": True, "topmost": False,
}


def load_settings():
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return settings


def fmt_clock(minutes):
    if minutes is None:
        return "—"
    total = max(0, int(minutes * 60))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_iso(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def beep():
    if sys.platform.startswith("win"):
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    else:
        print("\a", end="", flush=True)


class Poller(threading.Thread):
    """Тянет данные в фоне; окно забирает последний результат на своём тике.

    Полный список (wardogserverlist) — каждые POLL_SECONDS. Официальные сервера выбранных
    регионов (wardogsnow, свежее) — каждые LIVE_SECONDS, поверх полного списка.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.base, self.error, self.key_problem = None, None, False
        self.live = {}  # регион -> (когда получено, строки)
        self.live_error = None
        self.version, self.merged_version, self.merged = 0, -1, None
        # Пишет окно на каждом тике: (регионы, только со свободным местом, минимум игроков).
        self.wanted = (frozenset(), True, 0)
        self.wake = threading.Event()  # окно сохранило новый ключ — спросить сразу

    def run(self):
        threading.Thread(target=self.run_live, daemon=True).start()
        while True:
            started = time.time()
            key_problem = False
            try:
                data, error = w.fetch_servers(), None
            except w.ApiKeyError as exc:
                data, error, key_problem = None, str(exc), True
            except Exception as exc:  # сеть или сайт — покажем в статусе и повторим
                data, error = None, str(exc)
            with self.lock:
                if data is not None and (self.base is None or data.get("updatedAt") != self.base.get("updatedAt")):
                    self.base = data
                    self.version += 1
                self.error, self.key_problem = error, key_problem
            pause = KEY_RETRY_SECONDS if key_problem else POLL_SECONDS
            self.wake.wait(max(0.5, pause - (time.time() - started)))
            self.wake.clear()

    def run_live(self):
        tried = {}  # регион -> когда спрашивали в последний раз (и при ошибке тоже)
        while True:
            regions, has_space, min_players = self.wanted
            # Только что отмеченный регион грузится сразу, остальные — раз в LIVE_SECONDS.
            for region in sorted(r for r in regions if time.time() - tried.get(r, 0) >= LIVE_SECONDS):
                tried[region] = time.time()
                try:
                    rows, error = w.fetch_official_live(region, has_space, min_players), None
                except Exception as exc:
                    rows, error = None, str(exc)
                with self.lock:
                    if rows is not None:
                        self.live[region] = (time.time(), rows)
                        self.version += 1
                    self.live_error = error
            time.sleep(1)

    def snapshot(self):
        """(данные с подмешанными свежими, ошибка, ошибка из-за ключа?, возраст свежих данных в с)."""
        with self.lock:
            now = time.time()
            if self.base is not None and self.version != self.merged_version:
                merged = dict(self.base)
                merged["servers"] = [dict(s) for s in self.base.get("servers", [])]
                for region, (at, rows) in self.live.items():
                    if now - at < LIVE_MAX_AGE:
                        w.merge_official_live(merged, region, rows, at)
                self.merged, self.merged_version = merged, self.version
            ages = [now - self.live[r][0] for r in self.wanted[0] if r in self.live]
            return (self.merged, self.error or self.live_error, self.key_problem,
                    (max(ages) if ages else None))


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.poller = Poller()
        self.poller.start()
        self.armed = False
        self.join_thread = None
        self.join_result = None
        self.had_ready = False
        self.servers_by_iid = {}
        self.row_cache = {}  # iid -> (значения, тег), что сейчас нарисовано в таблице

        self.setup_style()
        self.build_vars()
        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.key_dialog = None
        self.last_key_error = None
        self.tick()
        if not w.load_api_key():
            root.after(400, self.open_key_dialog)

    # ---------- оформление ----------

    def setup_style(self):
        root = self.root
        root.title("WARDOGS — поиск сервера")
        root.configure(bg=C["bg"])
        root.geometry("1180x760")
        root.minsize(900, 560)
        base = ("Segoe UI", 10) if sys.platform.startswith("win") else ("Helvetica", 12)
        self.font = base
        self.font_bold = (base[0], base[1], "bold")
        self.font_big = (base[0], base[1] + 3, "bold")
        root.option_add("*Font", base)
        root.option_add("*TCombobox*Listbox.background", C["field"])
        root.option_add("*TCombobox*Listbox.foreground", C["fg"])
        root.option_add("*TCombobox*Listbox.selectBackground", C["sel"])

        st = ttk.Style(root)
        st.theme_use("clam")
        st.configure(".", background=C["panel"], foreground=C["fg"], fieldbackground=C["field"],
                     bordercolor=C["line"], lightcolor=C["panel"], darkcolor=C["panel"], font=base)
        st.configure("Bg.TFrame", background=C["bg"])
        st.configure("Panel.TFrame", background=C["panel"])
        st.configure("TLabel", background=C["panel"], foreground=C["fg"])
        st.configure("Dim.TLabel", background=C["panel"], foreground=C["dim"])
        st.configure("Head.TLabel", background=C["bg"], foreground=C["fg"], font=self.font_big)
        st.configure("Status.TLabel", background=C["bg"], foreground=C["dim"])
        st.configure("Err.TLabel", background=C["bg"], foreground=C["err"])
        st.configure("Count.TLabel", background=C["bg"], foreground=C["fg"], font=self.font_bold)
        st.configure("TCheckbutton", background=C["panel"], foreground=C["fg"],
                     indicatorbackground=C["field"], indicatorforeground=C["fg"])
        st.map("TCheckbutton", background=[("active", C["panel"])],
               indicatorbackground=[("selected", C["accent"]), ("active", C["line"])])
        st.configure("TCombobox", fieldbackground=C["field"], background=C["field"],
                     foreground=C["fg"], arrowcolor=C["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", C["field"])],
               foreground=[("readonly", C["fg"])], selectbackground=[("readonly", C["field"])])
        st.configure("TEntry", fieldbackground=C["field"], foreground=C["fg"], insertcolor=C["fg"])
        st.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                     foreground=C["fg"], rowheight=int(base[1] * 2.6), borderwidth=0)
        st.map("Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["fg"])])
        st.configure("Treeview.Heading", background=C["field"], foreground=C["dim"],
                     relief="flat", font=self.font_bold)
        st.map("Treeview.Heading", background=[("active", C["line"])])
        st.configure("Vertical.TScrollbar", background=C["field"], troughcolor=C["panel"],
                     arrowcolor=C["dim"], bordercolor=C["panel"])
        st.configure("Ghost.TButton", background=C["field"], foreground=C["fg"], padding=(14, 8))
        st.map("Ghost.TButton", background=[("active", C["line"])])

    def build_vars(self):
        s = self.settings
        self.map_vars = {world: tk.BooleanVar(value=world in s["maps"]) for world in w.MAPS}
        self.region_vars = {r: tk.BooleanVar(value=r in s["regions"]) for r in REGION_NAMES}
        self.v = {
            "min_players": tk.StringVar(value=str(s["min_players"])),
            "max_players": tk.StringVar(value=str(s["max_players"])),
            "use_minutes": tk.BooleanVar(value=s["use_minutes"]),
            "min_from": tk.StringVar(value=str(s["min_from"])),
            "min_to": tk.StringVar(value=str(s["min_to"])),
            "kind": tk.StringVar(value=s["kind"] if s["kind"] in KINDS else "все"),
            "infantry": tk.StringVar(value=s["infantry"] if s["infantry"] in INFANTRY else "любой"),
            "name": tk.StringVar(value=s["name"]),
            "allow_full": tk.BooleanVar(value=s["allow_full"]),
            "passworded": tk.BooleanVar(value=s["passworded"]),
            "sound": tk.BooleanVar(value=s["sound"]),
            "topmost": tk.BooleanVar(value=s["topmost"]),
        }
        self.v["topmost"].trace_add("write", lambda *_: self.root.attributes("-topmost", self.v["topmost"].get()))
        self.root.attributes("-topmost", self.v["topmost"].get())

    def spin(self, parent, var, lo, hi, width=5):
        return tk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=width, justify="center",
                          bg=C["field"], fg=C["fg"], buttonbackground=C["field"], insertbackground=C["fg"],
                          relief="flat", highlightthickness=1, highlightbackground=C["line"],
                          highlightcolor=C["accent"], disabledbackground=C["panel"])

    def build_ui(self):
        root = self.root
        outer = ttk.Frame(root, style="Bg.TFrame", padding=(16, 12, 16, 10))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer, style="Bg.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="WARDOGS · поиск сервера", style="Head.TLabel").pack(side="left")
        # Условие лицензии CC BY 4.0 у wardogserverlist — видимая ссылка на источник.
        ttk.Label(head, text="данные: wardogserverlist.com (CC BY 4.0), wardogsnow.com",
                  style="Status.TLabel").pack(side="left", padx=(14, 0))
        self.data_label = ttk.Label(head, text="загружаю список…", style="Status.TLabel")
        self.data_label.pack(side="right")

        # --- фильтры ---
        f = ttk.Frame(outer, style="Panel.TFrame", padding=(14, 12))
        f.pack(fill="x", pady=(10, 10))
        f.columnconfigure(1, weight=1)
        pad = {"padx": (0, 14), "pady": 4}

        ttk.Label(f, text="Карта", style="Dim.TLabel").grid(row=0, column=0, sticky="w", **pad)
        maps_row = ttk.Frame(f, style="Panel.TFrame")
        maps_row.grid(row=0, column=1, sticky="w", pady=4)
        for world, title in w.MAPS.items():
            ttk.Checkbutton(maps_row, text=title, variable=self.map_vars[world]).pack(side="left", padx=(0, 16))
        ttk.Label(maps_row, text="(ничего не отмечено — любая)", style="Dim.TLabel").pack(side="left")

        ttk.Label(f, text="Регион", style="Dim.TLabel").grid(row=1, column=0, sticky="nw", **pad)
        reg = ttk.Frame(f, style="Panel.TFrame")
        reg.grid(row=1, column=1, sticky="w", pady=4)
        for i, (code, title) in enumerate(REGION_NAMES.items()):
            ttk.Checkbutton(reg, text=title, variable=self.region_vars[code]).grid(
                row=i // 7, column=i % 7, sticky="w", padx=(0, 14), pady=1)

        ttk.Label(f, text="Условия", style="Dim.TLabel").grid(row=2, column=0, sticky="w", **pad)
        cond = ttk.Frame(f, style="Panel.TFrame")
        cond.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(cond, text="Игроков от").pack(side="left")
        self.spin(cond, self.v["min_players"], 0, 100).pack(side="left", padx=6)
        ttk.Label(cond, text="до").pack(side="left")
        self.spin(cond, self.v["max_players"], 0, 100).pack(side="left", padx=(6, 24))
        ttk.Checkbutton(cond, text="Минута матча от", variable=self.v["use_minutes"]).pack(side="left")
        self.min_from_spin = self.spin(cond, self.v["min_from"], 0, w.MAX_ROUND_MINUTES)
        self.min_from_spin.pack(side="left", padx=6)
        ttk.Label(cond, text="до").pack(side="left")
        self.min_to_spin = self.spin(cond, self.v["min_to"], 0, w.MAX_ROUND_MINUTES)
        self.min_to_spin.pack(side="left", padx=6)
        ttk.Label(cond, text="мин", style="Dim.TLabel").pack(side="left")

        ttk.Label(f, text="Ещё", style="Dim.TLabel").grid(row=3, column=0, sticky="w", **pad)
        more = ttk.Frame(f, style="Panel.TFrame")
        more.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(more, text="Тип").pack(side="left")
        ttk.Combobox(more, textvariable=self.v["kind"], values=list(KINDS), state="readonly",
                     width=12).pack(side="left", padx=(6, 16))
        ttk.Label(more, text="Режим").pack(side="left")
        ttk.Combobox(more, textvariable=self.v["infantry"], values=list(INFANTRY), state="readonly",
                     width=14).pack(side="left", padx=(6, 16))
        ttk.Label(more, text="Название").pack(side="left")
        ttk.Entry(more, textvariable=self.v["name"], width=18).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(more, text="Полные тоже (очередь)", variable=self.v["allow_full"]).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(more, text="С паролем", variable=self.v["passworded"]).pack(side="left")

        # --- кнопки ---
        bar = ttk.Frame(outer, style="Bg.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        # Label вместо Button: на macOS tk.Button не красится, а так кнопка одинаковая везде.
        self.arm_btn = tk.Label(bar, font=self.font_big, padx=20, pady=8, cursor="hand2")
        self.arm_btn.pack(side="left")
        self.arm_btn.bind("<Button-1>", lambda e: self.toggle_arm())
        self.arm_btn.bind("<Enter>", lambda e: self.paint_arm(hover=True))
        self.arm_btn.bind("<Leave>", lambda e: self.paint_arm())
        ttk.Button(bar, text="Зайти на выбранный", style="Ghost.TButton",
                   command=self.join_selected).pack(side="left", padx=10)
        ttk.Checkbutton(bar, text="Звук", variable=self.v["sound"], style="Bar.TCheckbutton").pack(side="left", padx=(6, 10))
        ttk.Checkbutton(bar, text="Поверх окон", variable=self.v["topmost"], style="Bar.TCheckbutton").pack(side="left")
        ttk.Style(self.root).configure("Bar.TCheckbutton", background=C["bg"])
        ttk.Style(self.root).map("Bar.TCheckbutton", background=[("active", C["bg"])])
        ttk.Button(bar, text="API-ключ…", style="Ghost.TButton",
                   command=self.open_key_dialog).pack(side="right")
        self.paint_arm()

        # --- таблица ---
        table = ttk.Frame(outer, style="Panel.TFrame")
        table.pack(fill="both", expand=True)
        cols = [("status", "Статус", 120, "w"), ("players", "Игроки", 80, "center"),
                ("match", "Матч идёт", 90, "center"), ("map", "Карта", 100, "w"),
                ("region", "Регион", 110, "w"), ("kind", "Тип", 160, "w"), ("name", "Сервер", 400, "w")]
        self.tree = ttk.Treeview(table, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, title, width, anchor in cols:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        self.tree.tag_configure("ok", background=C["ok_bg"], foreground=C["ok_fg"])
        self.tree.tag_configure("soon", background=C["soon_bg"], foreground=C["soon_fg"])
        self.tree.tag_configure("past", foreground=C["past_fg"])
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.join_selected())

        foot = ttk.Frame(outer, style="Bg.TFrame")
        foot.pack(fill="x", pady=(8, 0))
        self.count_label = ttk.Label(foot, text="", style="Count.TLabel")
        self.count_label.pack(side="right")
        self.status = ttk.Label(foot, text="Двойной клик по строке — зайти. Автозаход заходит на лучший "
                                           "подходящий сервер, как только он появится.", style="Status.TLabel")
        self.status.pack(side="left", fill="x", expand=True)

    # ---------- фильтры ----------

    def num(self, key, default):
        try:
            return int(self.v[key].get())
        except (ValueError, tk.TclError):
            return default

    def filters(self):
        lo, hi = self.num("min_from", 0), self.num("min_to", w.MAX_ROUND_MINUTES)
        return types.SimpleNamespace(
            maps={k for k, var in self.map_vars.items() if var.get()},
            regions={k for k, var in self.region_vars.items() if var.get()},
            kind=KINDS.get(self.v["kind"].get(), "any"),
            infantry=INFANTRY.get(self.v["infantry"].get(), "any"),
            name=self.v["name"].get().strip() or None,
            min_players=self.num("min_players", 0),
            max_players=self.num("max_players", 100),
            allow_full=self.v["allow_full"].get(),
            passworded=self.v["passworded"].get(),
            use_minutes=self.v["use_minutes"].get(),
            minutes=(min(lo, hi), max(lo, hi)),
        )

    def compute_rows(self, data):
        """[(статус, сервер, минута)]: 0 — подходит сейчас, 1 — войдёт в окно минут, 2 — уже прошло."""
        f = self.filters()
        now_ms = time.time() * 1000
        lo, hi = f.minutes
        rows = []
        for server in data.get("servers", []):
            if not w.passes_static(server, f):
                continue
            minutes = w.match_minutes(server, now_ms)
            if not f.use_minutes:
                state = 0
            elif minutes is None:
                continue
            elif lo <= minutes <= hi:
                state = 0
            elif minutes < lo:
                state = 1
            else:
                state = 2
            rows.append((state, server, minutes))
        rows.sort(key=lambda r: (r[0], lo - r[2] if r[0] == 1 else -r[1].get("players", 0),
                                 r[2] if r[2] is not None else 1e9))
        return rows, f

    # ---------- цикл окна ----------

    def tick(self):
        f = self.filters()
        live_regions = frozenset() if f.kind == "community" else frozenset(f.regions)
        self.poller.wanted = (live_regions, not f.allow_full, f.min_players)
        data, error, key_problem, live_age = self.poller.snapshot()
        self.update_header(data, error, key_problem, live_age, live_regions)
        if data:
            rows, f = self.compute_rows(data)
            self.sync_table(rows, f)
            ready = [r[1] for r in rows if r[0] == 0]
            soon = sum(1 for r in rows if r[0] == 1)
            self.count_label.configure(text=f"подходит сейчас: {len(ready)}   скоро: {soon}   по фильтру: {len(rows)}")
            if ready and not self.had_ready and not self.armed and self.v["sound"].get():
                beep()
            self.had_ready = bool(ready)
            if self.armed and ready and self.join_thread is None:
                self.start_join(ready[:5], auto=True)
        self.finish_join()
        self.root.after(1000, self.tick)

    def update_header(self, data, error, key_problem, live_age, live_regions):
        if key_problem:
            self.data_label.configure(text="нет доступа к списку серверов — нажми «API-ключ…»",
                                      style="Err.TLabel")
            if error != self.last_key_error:  # подробность — в строку статуса, один раз
                self.last_key_error = error
                self.set_status(error)
            return
        self.last_key_error = None
        if error:
            self.data_label.configure(text=f"ошибка сети: {error[:80]} — повторяю", style="Err.TLabel")
            return
        if not data:
            return
        parts = []
        if live_regions:
            parts.append("официальные: " + (f"{int(live_age)} с назад" if live_age is not None else "загружаю…"))
        else:
            parts.append("официальные: выбери регион для свежих данных")
        updated = parse_iso(data.get("updatedAt"))
        if updated:
            parts.append(f"остальное: {int(time.time() - updated) // 60} мин назад")
        parts.append(f"опрос {POLL_SECONDS} с")
        self.data_label.configure(text="   ·   ".join(parts), style="Status.TLabel")

    def sync_table(self, rows, f):
        # Таблица обновляется каждую секунду, поэтому трогаем только изменившееся:
        # вызовы Tk держат GIL и иначе тормозят фоновую загрузку.
        lo = f.minutes[0]
        tree = self.tree
        shown = rows[:MAX_ROWS]
        order = [s["serverKey"] for _, s, _ in shown]
        keep = set(order)
        for iid in tree.get_children():
            if iid not in keep:
                tree.delete(iid)
                self.row_cache.pop(iid, None)
        for state, s, minutes in shown:
            iid = s["serverKey"]
            if state == 0:
                status = "✓ подходит"
            elif state == 1:
                status = f"через {fmt_clock(lo - minutes)}"
            else:
                status = "окно прошло"
            kind = "официальный" if s.get("official") else "комьюнити"
            if w.is_infantry(s):
                kind += " · пехота"
            values = (status, f"{s.get('players', 0)}/{s.get('maxPlayers', 0)}", fmt_clock(minutes),
                      w.MAPS.get(s.get("world"), s.get("world")), REGION_NAMES.get(s.get("region"), s.get("region")),
                      kind, s.get("name", ""))
            tag = ("ok", "soon", "past")[state]
            cached = self.row_cache.get(iid)
            if cached is None:
                tree.insert("", "end", iid=iid, values=values, tags=(tag,))
            elif cached != (values, tag):
                tree.item(iid, values=values, tags=(tag,))
            self.row_cache[iid] = (values, tag)
        if list(tree.get_children()) != order:
            for index, iid in enumerate(order):
                tree.move(iid, "", index)
        self.servers_by_iid = {s["serverKey"]: s for _, s, _ in shown}

    # ---------- заход ----------

    def toggle_arm(self):
        self.armed = not self.armed
        self.paint_arm()
        self.save_settings()
        if self.armed:
            self.set_status("Автозаход включён: жду подходящий сервер…")

    def paint_arm(self, hover=False):
        if self.armed:
            self.arm_btn.configure(text="■  АВТОЗАХОД ВКЛЮЧЁН — жду сервер", fg="#ffffff",
                                   bg="#ff7a45" if hover else C["accent"])
        else:
            self.arm_btn.configure(text="▶  ВКЛЮЧИТЬ АВТОЗАХОД", fg=C["fg"],
                                   bg=C["line"] if hover else C["field"])

    def join_selected(self):
        sel = self.tree.selection()
        server = self.servers_by_iid.get(sel[0]) if sel else None
        if not server:
            self.set_status("Сначала выбери сервер в таблице.")
            return
        if self.join_thread is None:
            self.start_join([server], auto=False)

    def start_join(self, candidates, auto):
        self.set_status(f"Получаю ссылку входа: {candidates[0].get('name', '')}…")

        def work():
            for server in candidates:
                link = w.steam_join_link(server)
                if link:
                    w.open_url(link)
                    self.join_result = (True, server, auto)
                    return
            self.join_result = (False, candidates[0], auto)

        self.join_thread = threading.Thread(target=work, daemon=True)
        self.join_thread.start()

    def finish_join(self):
        if self.join_result is None:
            return
        ok, server, auto = self.join_result
        self.join_result, self.join_thread = None, None
        name = server.get("name", "")
        if ok:
            if auto:
                self.armed = False
                self.paint_arm()
            self.set_status(f"Захожу через Steam: {name}")
            if self.v["sound"].get():
                beep()
        else:
            # Автозаход остаётся включённым: на следующем тике попробует снова.
            self.set_status(f"Сайт не дал ссылку входа для {name} (сервер мог пропасть), пробую дальше…")

    def set_status(self, text):
        self.status.configure(text=f"[{time.strftime('%H:%M:%S')}] {text}")

    # ---------- API-ключ ----------

    def open_key_dialog(self):
        if self.key_dialog is not None and self.key_dialog.winfo_exists():
            self.key_dialog.lift()
            self.key_dialog.focus_force()
            return
        dlg = tk.Toplevel(self.root, bg=C["bg"])
        self.key_dialog = dlg
        dlg.title("API-ключ wardogserverlist")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        # Поверх окон — только вслед за главным окном: иначе диалог висит поверх игры.
        dlg.attributes("-topmost", self.v["topmost"].get())

        box = ttk.Frame(dlg, style="Panel.TFrame", padding=(18, 14))
        box.pack(fill="both", expand=True)
        text = ("С 7 ноября сайт wardogserverlist.com отдаёт список серверов только по API-ключу.\n"
                "Ключ бесплатный, делается за минуту:\n"
                "  1. «Открыть страницу ключей» → Sign in with Discord.\n"
                "  2. В разделе API keys создай ключ типа «server key» (не «browser key»).\n"
                "  3. Скопируй ключ, нажми «Вставить из буфера» и «Сохранить».\n"
                f"Ключ хранится только у тебя, в файле {w.API_KEY_PATH.name} рядом с программой.")
        ttk.Label(box, text=text, justify="left").pack(anchor="w")

        current = w.load_api_key()
        key_var = tk.StringVar(value=current)
        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", pady=(12, 4))
        ttk.Label(row, text="Ключ", style="Dim.TLabel").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(row, textvariable=key_var, width=56, show="•")
        entry.pack(side="left", fill="x", expand=True)
        shown = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="показать", variable=shown,
                        command=lambda: entry.configure(show="" if shown.get() else "•")).pack(side="left", padx=(8, 0))
        note = ttk.Label(box, text="ключ сохранён" if current else "ключа пока нет", style="Dim.TLabel")
        note.pack(anchor="w")

        def paste():
            # Ctrl+V в Tk на Windows не срабатывает на русской раскладке — отсюда отдельная кнопка.
            try:
                key_var.set(self.root.clipboard_get().strip())
                note.configure(text="вставлено из буфера — нажми «Сохранить»")
            except tk.TclError:
                note.configure(text="в буфере нет текста")

        def save():
            key = key_var.get().strip()
            if not key:
                note.configure(text="ключ пустой")
                return
            try:
                w.save_api_key(key)
            except OSError as exc:
                note.configure(text=f"не удалось сохранить: {exc}")
                return
            self.poller.wake.set()
            self.set_status("Ключ сохранён, запрашиваю список серверов…")
            dlg.destroy()

        buttons = ttk.Frame(box, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Открыть страницу ключей", style="Ghost.TButton",
                   command=lambda: w.open_url(w.API_KEYS_PAGE_URL)).pack(side="left")
        ttk.Button(buttons, text="Вставить из буфера", style="Ghost.TButton",
                   command=paste).pack(side="left", padx=8)
        ttk.Button(buttons, text="Отмена", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="right")
        ttk.Button(buttons, text="Сохранить", style="Ghost.TButton",
                   command=save).pack(side="right", padx=8)
        entry.bind("<Return>", lambda e: save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        entry.focus_set()

    # ---------- настройки ----------

    def save_settings(self):
        data = {k: (var.get() if isinstance(var, tk.BooleanVar) else var.get()) for k, var in self.v.items()}
        for key in ("min_players", "max_players", "min_from", "min_to"):
            data[key] = self.num(key, DEFAULTS[key])
        data["maps"] = [k for k, var in self.map_vars.items() if var.get()]
        data["regions"] = [k for k, var in self.region_vars.items() if var.get()]
        try:
            SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def on_close(self):
        self.save_settings()
        self.root.destroy()


def main():
    if sys.platform.startswith("win"):
        try:  # чёткий шрифт на мониторах с масштабированием
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
