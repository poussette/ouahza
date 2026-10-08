"""Ouahza - Android app (Kivy).

Native port of the Ouahza CLI: same providers/pricing modules,
wrapped in a small touch UI instead of argparse. Because this runs as a
real compiled app (not a page inside a browser), its network calls behave
exactly like the original Python script's -- no browser CORS policy
applies, so Solana token/staking lookups that fail from a browser page
work fine here.

UI: "dashboard" layout -- EUR total on top, a stacked bar showing each
label's share, labels sorted by value, tap a label to see its wallets and a
wallet to see its positions. Refresh/Copy actions sit at the bottom.
"""

from __future__ import annotations

__version__ = "0.1.14"






import os
import re
import json
import time
import threading

# Android has no reliable default CA bundle path for `requests`/urllib3 to
# find on its own; point it at certifi's bundled one explicitly, before any
# HTTPS call is made.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

from kivy.animation import Animation
from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, Line, Ellipse, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import escape_markup, platform

import report
from providers.safe import clean_text, safe_error, validate_rpc_url

APP_VERSION = "0.1.14"


def _version_problems() -> list[str]:
    """Component files whose release differs from the app's (a patch copied over
    an older tree leaves such files behind). Never raises."""
    try:
        from providers import version as _v
        return _v.mismatches({"main": __version__}) if _v.VERSION == APP_VERSION else \
            [f"providers/version.py ({_v.VERSION})"] + _v.mismatches({"main": __version__})
    except Exception:  # noqa: BLE001
        return ["providers/version.py (?)"]


VERSION_PROBLEMS = _version_problems()
SETTINGS_FILENAME = "ouahza_settings.json"

# "universal" or "arm64": written by the build (build_variant.py) so the update
# check offers the APK of the same variant as the installed one.
try:
    from build_variant import VARIANT as BUILD_VARIANT
except Exception:  # noqa: BLE001 - running from source
    BUILD_VARIANT = "universal"


def detect_variant() -> str:
    """Variant of the installed APK. The truth is read from the APK itself (its
    native-library folders); the build-time stamp is only a fallback."""
    if platform == "android":
        try:
            import zipfile
            from jnius import autoclass
            from providers import updater
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            with zipfile.ZipFile(activity.getApplicationInfo().sourceDir) as z:
                found = updater.variant_from_apk_entries(z.namelist())
            if found:
                return found
        except Exception:  # noqa: BLE001
            pass
    return BUILD_VARIANT

# Monospace font shipped with Kivy (used for the config editor).
try:
    import kivy

    MONO_FONT = os.path.join(kivy.kivy_data_dir, "fonts", "RobotoMono-Regular.ttf")
    if not os.path.exists(MONO_FONT):
        MONO_FONT = "Roboto"
except Exception:
    MONO_FONT = "Roboto"

DEFAULT_CONFIG = """# Colle ici ta liste d'adresses, au meme format que addresses.txt :
#   <adresse>                  -> chaine auto-detectee
#   <chaine>,<adresse>         -> chaine forcee (bitcoin/ethereum/solana/multiversx)
#   [Un Label]                 -> regroupe les adresses qui suivent sous ce nom
#
# Exemple :
# [Perso]
# bitcoin,bc1q...
# ethereum,0x...
"""

DEFAULT_SETTINGS = {
    "config_text": DEFAULT_CONFIG,
    "etherscan_key": "",
    "beacon_key": "",
    "secure_screen": False,
    "lp_pricing": True,
    "lp_trust_all": False,
    "check_updates": True,
    "skip_version": "",
    "mvx_gateway": "",
}

#: limits on what a (possibly corrupted/tampered) settings file or a huge
#: paste may feed into the app.
MAX_CONFIG_CHARS = 200_000
MAX_KEY_CHARS = 300
MAX_ROWS_PER_WALLET = 200        # first page; "Afficher plus" adds ROWS_PAGE at a time
ROWS_PAGE = 150
MAX_ROWS_HARD = 3000             # absolute ceiling per wallet (a hostile airdrop must not freeze the UI)


def sanitize_settings(raw) -> dict:
    """Return a settings dict with the right types/limits, whatever `raw` is."""
    out = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return out
    cfg = raw.get("config_text")
    if isinstance(cfg, str):
        out["config_text"] = cfg[:MAX_CONFIG_CHARS]
    for k in ("etherscan_key", "beacon_key"):
        v = raw.get(k)
        if isinstance(v, str):
            # API keys are printable ASCII without spaces; anything else
            # (newline, control chars...) would be a header/URL injection.
            v = "".join(ch for ch in v.strip() if 33 <= ord(ch) < 127)
            out[k] = v[:MAX_KEY_CHARS]
    gw = raw.get("mvx_gateway")
    if isinstance(gw, str) and (not gw.strip() or validate_rpc_url(gw.strip())):
        out["mvx_gateway"] = gw.strip()[:MAX_KEY_CHARS]  # empty = public default
    for k in ("secure_screen", "lp_pricing", "lp_trust_all", "check_updates"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    sv = raw.get("skip_version")
    if isinstance(sv, str) and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", sv.strip()):
        out["skip_version"] = sv.strip()
    return out


# ---------------------------------------------------------------------- theme

def rgba(hex_str: str, alpha: float = 1.0):
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, alpha)


BG = rgba("0E1320")
SURFACE = rgba("161D2F")
SURFACE2 = rgba("1D2640")
SEP = rgba("2B3656")
TRACK = rgba("232D47")
TEXT = rgba("F2F4FA")
ACCENT = rgba("5B8CFF")
CLEAR = (0, 0, 0, 0)
MUTED_HEX = "9AA5BF"
MUTED = rgba(MUTED_HEX)
FAINT_HEX = "7C88A6"
RED_HEX = "FF6B6B"
ORANGE_HEX = "FFB347"

# One colour per label, in order of value; the rest share a neutral grey.
SERIES = [rgba(c) for c in ("5B8CFF", "2DD4BF", "F5B94A", "C084FC", "FF7A90", "7BD88F")]
OTHER_COLOR = rgba("6B7690")
CHAIN_COLORS = {
    "bitcoin": rgba("F7931A"),
    "ethereum": rgba("8C9EFF"),
    "solana": rgba("B07CFF"),
    "multiversx": rgba("4C7DFF"),
}


# ------------------------------------------------------------------ formatting

def fmt_usd(v):
    return "-" if v is None else f"${v:,.2f}"


def fmt_eur(v):
    return "-" if v is None else f"€{v:,.2f}"


def fmt_usd0(v):
    return "-" if v is None else f"${v:,.0f}"


def fmt_eur0(v):
    return "-" if v is None else f"€{v:,.0f}"


def fmt_amount(x):
    if x is None:
        return "-"
    s = f"{x:,.8f}" if abs(x) < 1 else f"{x:,.4f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def fmt_pct(share):
    if share <= 0:
        return "0 %"
    if share < 0.001:
        return "<0.1 %"
    return f"{share * 100:.1f} %"


def short_addr(a: str) -> str:
    return a if len(a) <= 16 else f"{a[:7]}...{a[-6:]}"


def esc(s) -> str:
    return escape_markup(str(s))


# --------------------------------------------------------------------- widgets

def _bind_round_bg(widget, color, radius):
    """Paint a rounded rectangle behind `widget` (kept in sync with its size)."""
    with widget.canvas.before:
        widget._bg_color = Color(*color)
        widget._bg_rect = RoundedRectangle(pos=widget.pos, size=widget.size, radius=[radius])

    def sync(*_):
        widget._bg_rect.pos = widget.pos
        widget._bg_rect.size = widget.size

    widget.bind(pos=sync, size=sync)


def mk_label(text, size=14, color=TEXT, bold=False, halign="left", markup=False, **kwargs):
    """Fixed-box label: text is clipped/aligned inside whatever size it gets."""
    lbl = Label(
        text=text, font_size=sp(size), color=color, bold=bold, halign=halign,
        valign="middle", markup=markup, **kwargs,
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lbl


class WrapLabel(Label):
    """Label that wraps its text to its own width and grows in height."""

    def __init__(self, min_height=0, **kwargs):
        kwargs.setdefault("halign", "left")
        kwargs.setdefault("valign", "middle")
        kwargs.setdefault("font_size", sp(14))
        kwargs.setdefault("color", TEXT)
        kwargs.setdefault("size_hint", (1, None))
        super().__init__(**kwargs)
        self._min_h = min_height
        self.bind(width=self._update, texture_size=self._update)

    def _update(self, *_):
        self.text_size = (self.width, None)
        self.height = max(self.texture_size[1] + dp(8), self._min_h)


class Panel(BoxLayout):
    """Vertical box that sizes itself to its content, with a rounded background."""

    def __init__(self, bg=SURFACE, radius=0, **kwargs):
        kwargs.setdefault("orientation", "vertical")
        kwargs.setdefault("size_hint", (1, None))
        super().__init__(**kwargs)
        _bind_round_bg(self, bg, radius)
        self.bind(minimum_height=self.setter("height"))


class RoundedButton(Button):
    def __init__(self, bg=SURFACE2, fg=TEXT, radius=None, **kwargs):
        kwargs.setdefault("background_normal", "")
        kwargs.setdefault("background_down", "")
        kwargs.setdefault("background_disabled_normal", "")
        kwargs.setdefault("background_disabled_down", "")
        kwargs["background_color"] = CLEAR
        kwargs.setdefault("color", fg)
        kwargs.setdefault("disabled_color", (fg[0], fg[1], fg[2], 0.4))
        kwargs.setdefault("font_size", sp(15))
        kwargs.setdefault("bold", True)
        super().__init__(**kwargs)
        self._bg = bg
        _bind_round_bg(self, bg, radius if radius is not None else dp(16))
        self.bind(
            on_press=lambda *_: self._tint(True),
            on_release=lambda *_: self._tint(False),
            disabled=lambda *_: self._tint(False),
        )
        self._tint(False)

    def _tint(self, pressed):
        r, g, b, a = self._bg
        if self.disabled:
            a *= 0.5
        f = 1.2 if pressed else 1.0
        self._bg_color.rgba = (min(r * f, 1.0), min(g * f, 1.0), min(b * f, 1.0), a)


class IconButton(RoundedButton):
    """Square button showing a small 'sliders' (settings) icon."""

    def __init__(self, **kwargs):
        kwargs.setdefault("text", "")
        super().__init__(**kwargs)
        with self.canvas.after:
            Color(*TEXT)
            self._l1 = Line(width=dp(1.6), cap="round")
            self._l2 = Line(width=dp(1.6), cap="round")
            self._c1 = Line(width=dp(1.6))
            self._c2 = Line(width=dp(1.6))
        self.bind(pos=self._draw, size=self._draw)
        self._draw()

    def _draw(self, *_):
        cx, cy = self.center
        s = dp(9)
        self._l1.points = [cx - s, cy + dp(5), cx + s, cy + dp(5)]
        self._l2.points = [cx - s, cy - dp(5), cx + s, cy - dp(5)]
        self._c1.circle = (cx + dp(3), cy + dp(5), dp(2.6))
        self._c2.circle = (cx - dp(3), cy - dp(5), dp(2.6))


class TapCard(ButtonBehavior, BoxLayout):
    """A tappable box (header of a collapsible section)."""

    def __init__(self, bg=CLEAR, radius=None, **kwargs):
        super().__init__(**kwargs)
        self._bg = bg
        _bind_round_bg(self, bg, radius if radius is not None else dp(16))
        self.bind(
            on_press=lambda *_: self._tint(True),
            on_release=lambda *_: self._tint(False),
        )

    def _tint(self, pressed):
        r, g, b, a = self._bg
        self._bg_color.rgba = (1, 1, 1, 0.06) if pressed else (r, g, b, a)


class Dot(Widget):
    def __init__(self, color, size=10, **kwargs):
        kwargs.setdefault("size_hint", (None, None))
        kwargs.setdefault("size", (dp(size), dp(size)))
        kwargs.setdefault("pos_hint", {"center_y": 0.5})
        super().__init__(**kwargs)
        self._col = color
        self.bind(pos=self._draw, size=self._draw)
        self._draw()

    def _draw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(*self._col)
            Ellipse(pos=self.pos, size=self.size)


class BusyBar(Widget):
    """Fine barre de chargement indéterminée : un segment lumineux glisse sur un rail,
    en douceur (accélère puis ralentit). Invisible quand l'app est au repos."""

    def __init__(self, **kwargs):
        kwargs.setdefault("size_hint", (1, None))
        kwargs.setdefault("height", dp(4))
        super().__init__(**kwargs)
        self.opacity = 0
        self._t = 0.0
        self._ev = None
        with self.canvas:
            Color(*SURFACE2)
            self._track = RoundedRectangle(radius=[dp(2)])
            Color(*ACCENT)
            self._seg = RoundedRectangle(radius=[dp(2)])
        self.bind(pos=self._draw, size=self._draw)

    @staticmethod
    def _ease(t):  # smoothstep
        return t * t * (3 - 2 * t)

    def _draw(self, *_):
        w = self.width
        seg = w * 0.34
        x = self.x - seg + self._ease(self._t) * (w + seg)
        left, right = max(x, self.x), min(x + seg, self.x + w)
        self._track.pos, self._track.size = self.pos, self.size
        self._seg.pos = (left, self.y)
        self._seg.size = (max(0.0, right - left), self.height)

    def _tick(self, dt):
        self._t = (self._t + dt / 1.5) % 1.0
        self._draw()

    def start(self):
        if self._ev is None:
            self._ev = Clock.schedule_interval(self._tick, 1 / 30)
        Animation.cancel_all(self, "opacity")
        Animation(opacity=1, d=0.25).start(self)

    def stop(self):
        Animation.cancel_all(self, "opacity")
        anim = Animation(opacity=0, d=0.4)

        def done(*_):
            if self._ev is not None and self.opacity == 0:
                self._ev.cancel()
                self._ev = None
        anim.bind(on_complete=done)
        anim.start(self)


class Chevron(Widget):
    open = BooleanProperty(False)

    def __init__(self, **kwargs):
        kwargs.setdefault("size_hint", (None, None))
        kwargs.setdefault("size", (dp(16), dp(16)))
        kwargs.setdefault("pos_hint", {"center_y": 0.5})
        super().__init__(**kwargs)
        self.bind(pos=self._draw, size=self._draw, open=self._draw)
        self._draw()

    def _draw(self, *_):
        self.canvas.clear()
        cx, cy = self.center
        with self.canvas:
            Color(*MUTED)
            if self.open:  # pointing down
                pts = [cx - dp(5), cy + dp(2.5), cx, cy - dp(2.5), cx + dp(5), cy + dp(2.5)]
            else:  # pointing right
                pts = [cx - dp(2.5), cy + dp(5), cx + dp(2.5), cy, cx - dp(2.5), cy - dp(5)]
            Line(points=pts, width=dp(1.6), cap="round", joint="round")


class Bar(Widget):
    """Thin progress bar: `frac` (0..1) of the width filled with `color`."""

    def __init__(self, frac, color, **kwargs):
        kwargs.setdefault("size_hint", (1, None))
        kwargs.setdefault("height", dp(4))
        super().__init__(**kwargs)
        self._frac = max(0.0, min(1.0, frac))
        self._col = color
        self.bind(pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(*TRACK)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(2)])
            if self._frac > 0:
                w = max(self.width * self._frac, min(dp(3), self.width))
                Color(*self._col)
                RoundedRectangle(pos=self.pos, size=(w, self.height), radius=[dp(2)])


class StackBar(Widget):
    """Horizontal bar split into one coloured segment per label."""

    def __init__(self, segments, **kwargs):
        kwargs.setdefault("size_hint", (1, None))
        kwargs.setdefault("height", dp(12))
        super().__init__(**kwargs)
        self._segments = segments  # list of (share, color)
        self.bind(pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        segs = self._segments
        if not segs or self.width <= 0:
            return
        gap = dp(2)
        avail = self.width - gap * (len(segs) - 1)
        widths = [max(s * avail, dp(3)) for s, _c in segs]
        total = sum(widths)
        if total > avail:
            widths = [w * avail / total for w in widths]
        big, small = dp(6), dp(2)
        x = self.x
        with self.canvas:
            for i, ((_s, color), w) in enumerate(zip(segs, widths)):
                left = big if i == 0 else small
                right = big if i == len(segs) - 1 else small
                Color(*color)
                RoundedRectangle(
                    pos=(x, self.y), size=(w, self.height),
                    radius=[(left, left), (right, right), (right, right), (left, left)],
                )
                x += w + gap


class Row(BoxLayout):
    """One position: description on the left, EUR/USD values on the right."""

    def __init__(self, left_markup, right_markup, **kwargs):
        super().__init__(
            orientation="horizontal", size_hint=(1, None), spacing=dp(8),
            padding=(0, dp(2), 0, dp(2)), **kwargs,
        )
        with self.canvas.before:
            Color(*SEP)
            self._sep = Rectangle(pos=self.pos, size=(self.width, 1))
        self.bind(pos=self._sync_sep, size=self._sync_sep)
        self.lbl_left = WrapLabel(text=left_markup, markup=True, size_hint=(0.58, None), font_size=sp(13))
        self.lbl_right = WrapLabel(
            text=right_markup, markup=True, size_hint=(0.42, None), halign="right", font_size=sp(13),
        )
        for w in (self.lbl_left, self.lbl_right):
            w.pos_hint = {"top": 1}
            w.bind(height=self._sync)
            self.add_widget(w)
        self._sync()

    def _sync_sep(self, *_):
        self._sep.pos = (self.x, self.top - 1)
        self._sep.size = (self.width, 1)

    def _sync(self, *_):
        self.height = max(self.lbl_left.height, self.lbl_right.height) + dp(4)


class Collapsible(Panel):
    """A tappable header that expands/collapses a body built on first open."""

    def __init__(self, header, build_body, chevron=None, bg=SURFACE, radius=None, **kwargs):
        super().__init__(bg=bg, radius=radius if radius is not None else dp(16), **kwargs)
        self._header = header
        self._build_body = build_body
        self._chev = chevron
        self._body = None
        self._open = False
        self.add_widget(header)
        header.bind(on_release=self.toggle)

    def _scroll_parent(self):
        w = self.parent
        while w is not None and not isinstance(w, ScrollView):
            w = w.parent
        return w

    def toggle(self, *_, target_top=None):
        """Open/close. The header keeps its place on screen (or is moved to
        `target_top`, a window y) so the list starts right under it."""
        sv = self._scroll_parent()
        try:
            before = target_top if target_top is not None else self._header.to_window(0, self._header.top)[1]
        except Exception:
            sv = None
        if self._open:
            self.remove_widget(self._body)
            self._body = None  # reconstruit à la réouverture (masquages par défaut)
            self._open = False
        else:
            if self._body is None:
                self._body = self._build_body()
            self.add_widget(self._body)
            self._open = True
        if self._chev is not None:
            self._chev.open = self._open
        if sv is not None:
            for delay in (0, 0.08, 0.25):
                Clock.schedule_once(lambda dt, sv=sv, b=before: self._anchor(sv, b), delay)

    def _anchor(self, sv, target):
        try:
            view = sv.children[0]
            extra = view.height - sv.height
            if extra <= 0 or self.get_root_window() is None:
                return
            now = self._header.to_window(0, self._header.top)[1]
            delta = target - now
            if abs(delta) < 1:
                return
            sv.scroll_y = min(1.0, max(0.0, sv.scroll_y - delta / extra))
        except Exception:
            pass


def style_input(ti: TextInput) -> TextInput:
    ti.background_normal = ""
    ti.background_active = ""
    ti.background_color = rgba("0B101C")
    ti.foreground_color = TEXT
    ti.cursor_color = ACCENT
    ti.padding = [dp(10), dp(10), dp(10), dp(10)]
    return ti


# ------------------------------------------------------------------------- app

class OuahzaApp(App):
    title = "Ouahza"

    priced_ok = True

    def build(self):
        Window.clearcolor = BG
        self.running = False
        self.last_results = None
        self.last_text = ""
        self.settings_path = os.path.join(self.user_data_dir, SETTINGS_FILENAME)
        self.settings = self.load_settings()
        self._apply_secure_screen(self.settings.get("secure_screen", False))

        root = BoxLayout(orientation="vertical")

        top_bar = BoxLayout(
            size_hint=(1, None), height=dp(68), spacing=dp(10),
            padding=(dp(20), dp(12), dp(16), dp(4)),
        )
        top_bar.add_widget(mk_label(
            f"Ouahza  [size={int(sp(11))}][color={MUTED_HEX}]v{APP_VERSION}{' (!)' if VERSION_PROBLEMS else ''}[/color][/size]",
            size=17, bold=True, markup=True,
        ))
        settings_btn = IconButton(size_hint=(None, None), size=(dp(44), dp(44)), bg=SURFACE)
        settings_btn.bind(on_release=self.open_settings)
        top_bar.add_widget(settings_btn)
        root.add_widget(top_bar)

        busy_row = BoxLayout(size_hint=(1, None), height=dp(4), padding=(dp(20), 0, dp(20), 0))
        self.busy = BusyBar()
        busy_row.add_widget(self.busy)
        root.add_widget(busy_row)
        self._busy_n = 0
        self._busy_ev = None

        self.status_label = WrapLabel(
            text="Ouvre Paramètres pour coller ta liste d'adresses, puis appuie sur Actualiser.",
            color=MUTED, font_size=sp(12), padding=(dp(20), 0), min_height=dp(24),
        )
        root.add_widget(self.status_label)
        # Durées de la dernière actualisation (où part le temps) : petite ligne discrète.
        self.perf_label = WrapLabel(
            text="", color=rgba(FAINT_HEX), font_size=sp(10), padding=(dp(20), 0),
        )
        root.add_widget(self.perf_label)

        # Barre figée : en-tête du wallet ouvert quand le sien a défilé hors écran.
        # Superposée à la liste (FloatLayout) : n'en change pas la taille, donc aucun saut.
        self.sticky = FloatLayout(size_hint=(1, 1), pos_hint={"x": 0, "y": 0})
        self._sticky_for = []
        self._sticky_cards = []
        self.wallet_colls = []
        self.label_colls = []

        # Scrollable results: hero total, stacked bar, collapsible label cards.
        self.results_box = BoxLayout(
            orientation="vertical", size_hint=(1, None), spacing=dp(10),
            padding=(dp(16), dp(8), dp(16), dp(24)),
        )
        self.results_box.bind(minimum_height=self.results_box.setter("height"))
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(3), bar_color=(1, 1, 1, 0.25),
                                 size_hint=(1, 1), pos_hint={"x": 0, "y": 0})
        self.scroll.add_widget(self.results_box)
        area = FloatLayout()
        area.add_widget(self.scroll)
        area.add_widget(self.sticky)
        root.add_widget(area)
        self.scroll.bind(scroll_y=self._update_sticky, height=self._update_sticky)
        Clock.schedule_interval(self._update_sticky, 0.1)
        self._show_empty_hint()

        bottom = BoxLayout(
            size_hint=(1, None), height=dp(88), spacing=dp(10),
            padding=(dp(16), dp(12), dp(16), dp(22)),
        )
        with bottom.canvas.before:
            Color(*rgba("1B2339"))
            self._bottom_line = Rectangle(pos=bottom.pos, size=(bottom.width, 1))
        bottom.bind(
            pos=lambda w, _p: setattr(self._bottom_line, "pos", (w.x, w.top - 1)),
            size=lambda w, _s: setattr(self._bottom_line, "size", (w.width, 1)),
        )
        self.run_btn = RoundedButton(text="Actualiser", bg=ACCENT, fg=BG, font_size=sp(16))
        self.run_btn.bind(on_release=self.on_run)
        bottom.add_widget(self.run_btn)
        self.export_btn = RoundedButton(
            text="Copier", bg=SURFACE2, size_hint=(None, 1), width=dp(100), disabled=True,
        )
        self.export_btn.bind(on_release=self.open_export)
        bottom.add_widget(self.export_btn)
        root.add_widget(bottom)

        return root

    def _show_empty_hint(self):
        self.results_box.clear_widgets()
        self.results_box.add_widget(WrapLabel(
            text="Aucun résultat pour le moment.\nAppuie sur Actualiser pour interroger tes wallets.",
            color=MUTED, font_size=sp(14), padding=(dp(4), dp(20)),
        ))

    # ---------------------------------------------------------------- settings

    def load_settings(self) -> dict:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                return sanitize_settings(json.load(f))
        except (FileNotFoundError, ValueError, OSError, RecursionError):
            return dict(DEFAULT_SETTINGS)

    def save_settings(self, data: dict) -> None:
        """Validate, then write atomically (temp file + rename) with owner-only
        permissions: the file holds the wallet list and API keys. It lives in
        the app-private directory (other apps cannot read it) and backups are
        disabled in buildozer.spec."""
        self.settings = sanitize_settings(data)
        tmp = self.settings_path + ".tmp"
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.settings_path)
        except OSError:
            self.status_label.text = "Impossible d'enregistrer la configuration."

    def _apply_secure_screen(self, on: bool) -> None:
        """Optional FLAG_SECURE: hides the app from screenshots, screen
        recording and the recent-apps thumbnail (balances are sensitive)."""
        if platform != "android":
            return
        try:
            from android.runnable import run_on_ui_thread
            from jnius import autoclass

            params = autoclass("android.view.WindowManager$LayoutParams")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity

            @run_on_ui_thread
            def _apply():
                window = activity.getWindow()
                if on:
                    window.addFlags(params.FLAG_SECURE)
                else:
                    window.clearFlags(params.FLAG_SECURE)

            _apply()
        except Exception:
            pass

    # ------------------------------------------------- dernier rapport conservé

    def _report_path(self):
        return os.path.join(self.user_data_dir, "last_report.json")

    def _save_last_report(self, results, priced_ok):
        try:
            tmp = self._report_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(report.dump_results(results, priced_ok, time.time()))
            os.replace(tmp, self._report_path())
        except Exception:  # noqa: BLE001 - never block the app for a cache
            pass

    def _load_last_report(self):
        """Affiche tout de suite les dernières données enregistrées (avant toute requête)."""
        if not self.settings.get("config_text", "").strip():
            return
        try:
            with open(self._report_path(), encoding="utf-8") as f:
                loaded = report.load_results(f.read())
        except Exception:  # noqa: BLE001
            return
        if not loaded or not loaded[0]:
            return
        results, priced_ok, saved_at = loaded
        self.priced_ok = priced_ok
        text_results = results
        if priced_ok:
            import copy
            text_results = copy.deepcopy(results)
            report.filter_unpriced(text_results)
            report.filter_dust(text_results)
        self._finish(results, report.format_table(text_results), None, priced_ok, [],
                     stamp=saved_at, from_disk=True)

    # ----------------------------------------------------------------- updates

    def on_start(self):
        self._load_last_report()
        # Une configuration existe : l'actualisation démarre toute seule (le dernier
        # rapport reste affiché pendant ce temps).
        try:
            if report.parse_input_text(self.settings.get("config_text", "")):
                Clock.schedule_once(lambda dt: self.on_run(None), 0.3)
        except ValueError:
            pass
        if self.settings.get("check_updates", True):
            Clock.schedule_once(lambda dt: self.check_update(manual=False), 2.0)

    def check_update(self, manual=False):
        """Ask GitHub for a newer release (background thread, never blocks the UI)."""
        if manual:
            self.status_label.text = "Recherche de mise à jour..."

        def work():
            info = err = None
            try:
                from providers import updater
                info = updater.check(APP_VERSION, variant=detect_variant())
            except Exception as exc:  # noqa: BLE001 - offline/rate limit: stay silent unless asked
                err = safe_error(exc) or exc.__class__.__name__
            Clock.schedule_once(lambda dt: self._update_result(info, err, manual))
        threading.Thread(target=work, daemon=True).start()

    def _update_result(self, info, err, manual):
        if err:
            if manual:
                self.status_label.text = "Mise à jour : vérification impossible (" + clean_text(err, 80) + ")."
            return
        if not info:
            if manual:
                self.status_label.text = f"Application à jour (v{APP_VERSION})."
            return
        if not manual and info["version"] == self.settings.get("skip_version"):
            return
        self._offer_update(info)

    def _download_apk(self, info) -> bool:
        """Download the APK with Android's DownloadManager: progress and a
        "download complete" notification belong to the system (no browser tab);
        tapping that notification opens the installer. False -> use the browser."""
        url = info["url"]
        if platform != "android" or not url.startswith("https://github.com/") or not url.endswith(".apk"):
            return False
        try:
            from jnius import autoclass
            Context = autoclass("android.content.Context")
            Uri = autoclass("android.net.Uri")
            Request = autoclass("android.app.DownloadManager$Request")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            dm = activity.getSystemService(Context.DOWNLOAD_SERVICE)
            name = "Ouahza-" + re.sub(r"[^0-9.]", "", info["version"]) + ".apk"
            req = Request(Uri.parse(url))
            req.setTitle("Ouahza " + info["version"])
            req.setDescription("Mise à jour : touche cette notification une fois terminé")
            req.setMimeType("application/vnd.android.package-archive")
            req.setNotificationVisibility(Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            # Public Downloads folder: the system download manager / Files app can
            # open it (the app-private folder used before was not openable).
            Environment = autoclass("android.os.Environment")
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
            self._ask_install_permission(activity)
            self._dl_id = dm.enqueue(req)
            self._watch_download()
            return True
        except Exception:  # noqa: BLE001 - fall back to the browser
            return False

    def _ask_install_permission(self, activity) -> None:
        """Android 8+: installing an APK needs the per-app "install unknown
        apps" switch; open its settings page once if it is still off."""
        try:
            from jnius import autoclass
            if activity.getPackageManager().canRequestPackageInstalls():
                return
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            i = Intent("android.settings.MANAGE_UNKNOWN_APP_SOURCES",
                       Uri.parse("package:" + activity.getPackageName()))
            activity.startActivity(i)
        except Exception:  # noqa: BLE001
            pass

    def _watch_download(self) -> None:
        """When the download we started completes, open the system installer
        straight away (the notification stays as a fallback)."""
        if getattr(self, "_dl_receiver", None) is not None:
            return
        try:
            from android.broadcast import BroadcastReceiver
            self._dl_receiver = BroadcastReceiver(
                self._on_download_done, actions=["android.intent.action.DOWNLOAD_COMPLETE"],
            )
            self._dl_receiver.start()
        except Exception:  # noqa: BLE001 - the notification still works
            self._dl_receiver = None

    def _on_download_done(self, context, intent) -> None:
        try:
            from jnius import autoclass
            if intent.getLongExtra("extra_download_id", -1) != getattr(self, "_dl_id", -2):
                return
            Context = autoclass("android.content.Context")
            Intent = autoclass("android.content.Intent")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            uri = activity.getSystemService(Context.DOWNLOAD_SERVICE).getUriForDownloadedFile(self._dl_id)
            if uri is None:  # download failed
                Clock.schedule_once(lambda dt: setattr(self.status_label, "text", "Téléchargement de la mise à jour échoué."))
                return
            view = Intent(Intent.ACTION_VIEW)
            view.setDataAndType(uri, "application/vnd.android.package-archive")
            view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK)
            activity.startActivity(view)
            Clock.schedule_once(lambda dt: setattr(self.status_label, "text", "Installation de la mise à jour..."))
        except Exception:  # noqa: BLE001 - tap the download notification instead
            pass

    def _open_url(self, url: str) -> None:
        """Hand the release link to the system (browser/download manager)."""
        if not url.startswith("https://github.com/"):
            return
        if platform == "android":
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                return
            except Exception:
                pass
        import webbrowser
        webbrowser.open(url)

    def _offer_update(self, info):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        notes = info.get("notes") or ""
        text = (
            f"[b]Version {esc(info['version'])}[/b] disponible (installée : {esc(APP_VERSION)}, variante {esc(detect_variant())}).\n\n"
            + (f"{esc(notes)}\n\n" if notes else "")
            + f"[color={MUTED_HEX}]Tes paramètres sont conservés : Android installe la mise à jour "
            "par-dessus l'app actuelle (ne la désinstalle pas).[/color]"
        )
        box.add_widget(WrapLabel(text=text, markup=True, font_size=sp(13)))
        row = BoxLayout(size_hint=(1, None), height=dp(48), spacing=dp(8))
        later = RoundedButton(text="Plus tard", bg=SURFACE2, font_size=sp(13))
        skip = RoundedButton(text="Ignorer", bg=SURFACE2, font_size=sp(13))
        go = RoundedButton(text="Mettre à jour", bg=ACCENT, fg=BG, font_size=sp(13))
        for b in (later, skip, go):
            row.add_widget(b)
        box.add_widget(row)
        popup = self._popup("Mise à jour disponible", box, size_hint=(0.92, None), height=dp(300))

        def do_skip(_b):
            data = dict(self.settings)
            data["skip_version"] = info["version"]
            self.save_settings(data)
            popup.dismiss()

        def do_go(_b):
            popup.dismiss()
            if self._download_apk(info):
                self.status_label.text = (
                    "Téléchargement lancé : l'installation s'ouvre à la fin "
                    "(sinon touche la notification « Téléchargement terminé »)."
                )
            else:
                self._open_url(info["url"])
                self.status_label.text = "Téléchargement ouvert : lance le fichier .apk téléchargé pour installer."
        later.bind(on_release=lambda _b: popup.dismiss())
        skip.bind(on_release=do_skip)
        go.bind(on_release=do_go)
        popup.open()

    def _popup(self, title, content, **kwargs):
        return Popup(
            title=title, content=content, background="", background_color=SURFACE,
            separator_color=ACCENT, title_color=TEXT, **kwargs,
        )

    def open_settings(self, _instance):
        # The form scrolls (the wallet box keeps a usable height); Annuler/Enregistrer stay pinned below.
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8), size_hint=(1, None), pos_hint={"top": 1})
        def _fit_height(*_a):
            content.height = max(content.minimum_height, form_scroll.height)
        content.bind(minimum_height=_fit_height)

        content.add_widget(WrapLabel(
            text="Configuration des wallets (même format que addresses.txt)",
            min_height=dp(30), font_size=sp(13), color=MUTED,
        ))
        config_input = style_input(TextInput(
            text=self.settings.get("config_text", DEFAULT_CONFIG),
            font_size=sp(12), font_name=MONO_FONT, size_hint=(1, None), height=dp(280),
        ))
        content.add_widget(config_input)

        keys_row = BoxLayout(size_hint=(1, None), height=dp(48), spacing=dp(8))
        keys_row.add_widget(WrapLabel(text="Clé Etherscan (optionnelle)", size_hint=(0.5, 1), font_size=sp(12)))
        etherscan_input = style_input(TextInput(
            text=self.settings.get("etherscan_key", ""), multiline=False, password=True, size_hint=(0.5, 1),
        ))
        keys_row.add_widget(etherscan_input)
        content.add_widget(keys_row)

        beacon_row = BoxLayout(size_hint=(1, None), height=dp(48), spacing=dp(8))
        beacon_row.add_widget(WrapLabel(text="Clé beaconcha.in (optionnelle)", size_hint=(0.5, 1), font_size=sp(12)))
        beacon_input = style_input(TextInput(
            text=self.settings.get("beacon_key", ""), multiline=False, password=True, size_hint=(0.5, 1),
        ))
        beacon_row.add_widget(beacon_input)
        content.add_widget(beacon_row)

        gw_row = BoxLayout(size_hint=(1, None), height=dp(48), spacing=dp(8))
        gw_row.add_widget(WrapLabel(text="Gateway API MultiversX (vide = public)", size_hint=(0.5, 1), font_size=sp(12)))
        gw_input = style_input(TextInput(
            text=self.settings.get("mvx_gateway", ""), multiline=False, size_hint=(0.5, 1),
            hint_text="https://gateway.multiversx.com",
        ))
        gw_row.add_widget(gw_input)
        content.add_widget(gw_row)

        lp_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        lp_checkbox = CheckBox(active=self.settings.get("lp_pricing", True), size_hint=(None, 1), width=dp(44))
        lp_row.add_widget(lp_checkbox)
        lp_row.add_widget(WrapLabel(
            text="Valoriser les LP tokens via les contrats (plus lent)", size_hint=(1, 1), font_size=sp(12),
        ))
        content.add_widget(lp_row)

        trust_row = BoxLayout(size_hint=(1, None), height=dp(64), spacing=dp(8))
        trust_checkbox = CheckBox(active=self.settings.get("lp_trust_all", False), size_hint=(None, 1), width=dp(44))
        trust_row.add_widget(trust_checkbox)
        trust_row.add_widget(WrapLabel(
            text="Mode permissif LP : valoriser aussi les pools dont le code est inconnu (moins sûr)",
            size_hint=(1, 1), font_size=sp(12),
        ))
        content.add_widget(trust_row)

        secure_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        secure_checkbox = CheckBox(active=self.settings.get("secure_screen", False), size_hint=(None, 1), width=dp(44))
        secure_row.add_widget(secure_checkbox)
        secure_row.add_widget(WrapLabel(
            text="Masquer l'app des captures d'écran et des applis récentes", size_hint=(1, 1), font_size=sp(12),
        ))
        content.add_widget(secure_row)

        upd_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        upd_checkbox = CheckBox(active=self.settings.get("check_updates", True), size_hint=(None, 1), width=dp(44))
        upd_row.add_widget(upd_checkbox)
        upd_row.add_widget(WrapLabel(
            text="Vérifier les mises à jour au lancement (GitHub)", size_hint=(1, 1), font_size=sp(12),
        ))
        content.add_widget(upd_row)
        upd_btn = RoundedButton(text="Vérifier maintenant", bg=SURFACE2, size_hint=(1, None), height=dp(44), font_size=sp(13))
        upd_btn.bind(on_release=lambda _b: self.check_update(manual=True))
        content.add_widget(upd_btn)

        buttons_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        cancel_btn = RoundedButton(text="Annuler", bg=SURFACE2, font_size=sp(15))
        save_btn = RoundedButton(text="Enregistrer", bg=ACCENT, fg=BG, font_size=sp(15))
        buttons_row.add_widget(cancel_btn)
        buttons_row.add_widget(save_btn)

        form_scroll = ScrollView(do_scroll_x=False, bar_width=dp(4), bar_color=(1, 1, 1, 0.3))
        content.add_widget(Widget())  # espace libre en bas : le formulaire reste aligné en haut
        form_scroll.bind(height=_fit_height)
        form_scroll.add_widget(content)
        outer = BoxLayout(orientation="vertical", spacing=dp(6), padding=(dp(10), 0, dp(10), dp(10)))
        outer.add_widget(form_scroll)
        outer.add_widget(buttons_row)

        popup = self._popup("Paramètres", outer, size_hint=(0.95, 0.95))

        def do_save(_btn):
            self.save_settings({
                "config_text": config_input.text,
                "etherscan_key": etherscan_input.text.strip(),
                "beacon_key": beacon_input.text.strip(),
                "secure_screen": secure_checkbox.active,
                "lp_pricing": lp_checkbox.active,
                "lp_trust_all": trust_checkbox.active,
                "check_updates": upd_checkbox.active,
                "skip_version": self.settings.get("skip_version", ""),
                "mvx_gateway": gw_input.text.strip(),
            })
            gw_rejected = bool(gw_input.text.strip()) and not self.settings.get("mvx_gateway")
            self._apply_secure_screen(self.settings["secure_screen"])
            popup.dismiss()
            self.status_label.text = (
                "Gateway API MultiversX ignorée (https:// requis) ; le reste est enregistré."
                if gw_rejected else "Configuration enregistrée. Appuie sur Actualiser."
            )

        save_btn.bind(on_release=do_save)
        cancel_btn.bind(on_release=lambda _b: popup.dismiss())
        popup.open()

    def open_export(self, _instance):
        if not self.last_results:
            return
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        popup = self._popup("Copier les résultats", box, size_hint=(0.88, None), height=dp(300))

        def make(label, action):
            btn = RoundedButton(text=label, bg=SURFACE2, size_hint=(1, None), height=dp(56))

            def go(_b):
                action()
                popup.dismiss()

            btn.bind(on_release=go)
            box.add_widget(btn)

        make("Texte", self.copy_text)
        make("JSON", self.copy_json)
        make("CSV", self.copy_csv)
        popup.open()

    # ------------------------------------------------------------------- run

    def on_run(self, _instance):
        if self.running:
            return
        try:
            entries = report.parse_input_text(self.settings.get("config_text", ""))
        except ValueError as exc:
            self.status_label.text = safe_error(exc)
            return
        if not entries:
            self.status_label.text = "Aucune adresse dans la configuration -- ouvre Paramètres pour en coller."
            return

        self.running = True
        self.run_btn.disabled = True
        self.run_btn.text = "Chargement   "
        self.export_btn.disabled = True
        self.status_label.text = f"0 / {len(entries)} wallet(s) traité(s)..."
        self._busy_start()

        threading.Thread(target=self._run_worker, args=(entries,), daemon=True).start()

    def _busy_start(self):
        """Barre qui glisse + points qui s'animent sur le bouton, tant que ça travaille."""
        self.busy.start()
        self._busy_n = 0
        if self._busy_ev is None:
            self._busy_ev = Clock.schedule_interval(self._busy_tick, 0.4)

    def _busy_tick(self, _dt):
        if not self.running:
            return
        self._busy_n = (self._busy_n + 1) % 4
        self.run_btn.text = "Chargement" + "." * self._busy_n + " " * (3 - self._busy_n)

    def _busy_stop(self):
        self.busy.stop()
        if self._busy_ev is not None:
            self._busy_ev.cancel()
            self._busy_ev = None

    def _run_worker(self, entries):
        try:
            os.environ["ETHERSCAN_API_KEY"] = self.settings.get("etherscan_key", "") or ""
            os.environ["BEACONCHAIN_API_KEY"] = self.settings.get("beacon_key", "") or ""
            os.environ["MULTIVERSX_GATEWAY_URL"] = self.settings.get("mvx_gateway", "") or ""
            os.environ["WALLET_LP_PRICING"] = "1" if self.settings.get("lp_pricing", True) else "0"
            os.environ["WALLET_LP_TRUST_ALL"] = "1" if self.settings.get("lp_trust_all", False) else "0"

            def progress(done, total):
                Clock.schedule_once(
                    lambda dt: setattr(self.status_label, "text", f"{done} / {total} wallet(s) traité(s)...")
                )

            from providers import net as _net
            _net.reset_stats()
            t0 = time.monotonic()
            # Prix qui ne dépendent pas des soldes : demandés pendant la lecture des wallets.
            report.start_price_prefetch(entries)
            results = report.fetch_all(entries, workers=4, on_progress=progress)
            t_wallets = time.monotonic() - t0

            # Montants à jour affichés tout de suite, valorisés avec les derniers prix connus.
            previous = self.last_results
            if previous:
                import copy
                partial = copy.deepcopy(results)
                if report.carry_prices(partial, previous):
                    Clock.schedule_once(lambda dt: self._show_partial(partial))

            priced_ok = True
            notes = []
            timings = {}
            try:
                import pricing as _pricing
                from providers import lp as _lp
                _lp.set_cache_path(os.path.join(self.user_data_dir, "lp_cache.json"))

                def _lp_progress(done, total, ok):
                    Clock.schedule_once(lambda dt: setattr(
                        self.status_label, "text", f"Valorisation des LP : {done}/{total} ({ok} valorisés)..."))

                priced_ok = _pricing.apply_pricing(results, on_progress=_lp_progress) is not False
                notes = [clean_text(n, 120) for n in _pricing.LAST_NOTES]
                timings = dict(_pricing.LAST_TIMINGS)
            except Exception:
                priced_ok = False  # offline / pricing down: keep raw balances
            perf = (
                f"Durées : wallets {t_wallets:.0f} s · prix {timings.get('prix', 0):.0f} s"
                f" · LP {timings.get('lp', 0):.0f} s · total {time.monotonic() - t0:.0f} s"
            )
            stats = _net.stats_summary()
            if stats:
                perf += "\n" + stats

            # Les positions sans valeur / < 1 centime sont masquées à l'affichage
            # (avec bouton pour les voir) ; l'export texte suit le même défaut.
            self.priced_ok = priced_ok
            text_results = results
            if priced_ok:
                import copy
                text_results = copy.deepcopy(results)
                report.filter_unpriced(text_results)
                report.filter_dust(text_results)
            text = report.format_table(text_results)
            self._save_last_report(results, priced_ok)
            Clock.schedule_once(lambda dt: self._finish(results, text, None, priced_ok, notes, perf=perf))
        except Exception as exc:
            # Never show a raw traceback: it can embed URLs with API keys.
            err = safe_error(exc) or exc.__class__.__name__
            Clock.schedule_once(lambda dt: self._finish(None, None, err, True, []))

    def _show_partial(self, partial):
        """Soldes frais + derniers prix connus, le temps que les nouveaux prix arrivent."""
        if not self.running:
            return
        self.priced_ok = True
        self.results_box.clear_widgets()
        try:
            self.render_results(partial)
        except Exception:  # noqa: BLE001 - the final render replaces it anyway
            pass

    def _finish(self, results, text, error, priced_ok, notes=(), stamp=None, from_disk=False, perf=""):
        self.running = False
        self._busy_stop()
        self.run_btn.disabled = False
        self.run_btn.text = "Actualiser"
        self.results_box.clear_widgets()
        if error:
            self.status_label.text = "Erreur inattendue."
            self.results_box.add_widget(WrapLabel(
                text=f"[color={RED_HEX}]{esc(clean_text(error, 300))}[/color]", markup=True, font_size=sp(11),
            ))
            return
        self.last_results = results
        self.last_text = text
        try:
            self.render_results(results)
        except Exception as exc:
            self.results_box.add_widget(WrapLabel(
                text=f"[color={RED_HEX}]{esc(safe_error(exc))}[/color]", markup=True, font_size=sp(11),
            ))
        when = time.strftime('%d/%m/%Y à %H:%M', time.localtime(stamp)) if stamp else time.strftime('%d/%m/%Y à %H:%M')
        msg = f"Mis à jour le {when} · {len(results)} wallet(s)"
        if from_disk:
            msg += " · dernières données enregistrées, appuie sur Actualiser"
        self.perf_label.text = perf if not from_disk else self.perf_label.text
        if not priced_ok:
            msg += " · prix indisponibles, soldes bruts affichés"
        for note in notes:
            msg += " · " + note
        if VERSION_PROBLEMS:
            msg += " · (!) fichiers d'une autre version : " + clean_text(", ".join(VERSION_PROBLEMS), 150)
        self.status_label.text = msg
        self.export_btn.disabled = False
        self.scroll.scroll_y = 1

    # ---------------------------------------------------------------- results

    def render_results(self, results):
        box = self.results_box
        self.wallet_colls = []
        self.label_colls = []
        self._sticky_for = []
        self._sticky_cards = []
        self.sticky.clear_widgets()
        priced = [w for w in results if not w.error and w.total_usd is not None]
        errors = [w for w in results if w.error]
        grand_usd = sum(w.total_usd for w in priced)
        grand_eur = sum((w.total_eur or 0.0) for w in priced)

        # Hero: EUR first, USD as the secondary figure.
        hero = Panel(bg=CLEAR, padding=(dp(4), dp(6), dp(4), 0), spacing=dp(2))
        hero.add_widget(WrapLabel(text="TOTAL", color=MUTED, font_size=sp(12), min_height=dp(22)))
        eur_int, eur_dec = f"{grand_eur:,.2f}".split(".")
        hero.add_widget(WrapLabel(
            text=f"[b]€{eur_int}[/b][size={int(sp(22))}][color={FAINT_HEX}].{eur_dec}[/color][/size]",
            markup=True, font_size=sp(44),
        ))
        sub = f"≈ {fmt_usd(grand_usd)}  ·  {len(priced)} valorisé(s) sur {len(results)}"
        if errors:
            sub += f"  ·  [color={RED_HEX}]{len(errors)} en erreur[/color]"
        hero.add_widget(WrapLabel(
            text=sub, markup=True, color=MUTED, font_size=sp(13),
        ))
        box.add_widget(hero)

        # Group wallets by label (config order), then sort groups by value.
        groups: dict = {}
        for w in results:
            groups.setdefault(w.label, []).append(w)
        label_totals = report.compute_label_totals(results)

        entries = []
        for label, wallets in groups.items():
            if label and label in label_totals:
                t = label_totals[label]
                usd, eur = (t["usd"], t["eur"]) if t["priced"] else (None, None)
            else:
                ok = [w for w in wallets if not w.error and w.total_usd is not None]
                usd = sum(w.total_usd for w in ok) if ok else None
                eur = sum((w.total_eur or 0.0) for w in ok) if ok else None
            entries.append({
                "name": label or "Sans label", "wallets": wallets, "usd": usd, "eur": eur,
            })
        entries.sort(key=lambda e: -(e["eur"] if e["eur"] is not None else -1.0))
        for i, e in enumerate(entries):
            e["color"] = SERIES[i] if i < len(SERIES) else OTHER_COLOR
            e["share"] = (e["usd"] / grand_usd) if (grand_usd > 0 and e["usd"] is not None) else 0.0

        segments = [(e["share"], e["color"]) for e in entries if e["share"] > 0]
        if segments:
            box.add_widget(StackBar(segments))
            box.add_widget(WrapLabel(
                text="Répartition par label", color=MUTED, font_size=sp(12), min_height=dp(24),
            ))

        for e in entries:
            header, chev = self._label_header(e)
            lcoll = Collapsible(
                header, build_body=lambda ws=e["wallets"]: self._label_body(ws),
                chevron=chev, bg=SURFACE,
            )
            lcoll.entry = e
            self.label_colls.append(lcoll)
            box.add_widget(lcoll)

    LABEL_PIN_H = 48
    WALLET_PIN_H = 56

    def _update_sticky(self, *_):
        """Épingle (en surimpression, sans toucher à la liste) le label puis le
        wallet ouverts dont l'en-tête vient de passer sous le bord haut, tant que
        leur contenu est encore affiché derrière."""
        try:
            slot = self.scroll.to_window(0, self.scroll.top)[1]
            gap = dp(2)
            chosen = []   # (coll, slot_top, height)
            for colls, h in ((self.label_colls, dp(self.LABEL_PIN_H)), (self.wallet_colls, dp(self.WALLET_PIN_H))):
                best = None
                for c in colls:
                    if not c._open or c.get_root_window() is None:
                        continue
                    ht = c._header.to_window(0, c._header.top)[1]
                    cy = c.to_window(0, c.y)[1]
                    if ht >= slot and cy + dp(2) < slot - h:
                        best = c  # le plus profond gagne
                if best is not None:
                    chosen.append((best, slot, h))
                    slot -= h + gap
        except Exception:
            chosen = []
        ids = [c for c, _s, _h in chosen]
        if ids != self._sticky_for:
            self._sticky_for = ids
            self.sticky.clear_widgets()
            self._sticky_cards = []
            for c, _top, h in chosen:
                if hasattr(c, "entry"):
                    card = self._label_pinned(c.entry)
                else:
                    card, chev = self._wallet_header(c.wallet)
                    chev.open = True
                card._bg = SURFACE2
                card._bg_color.rgba = SURFACE2
                card.size_hint = (None, None)
                card.height = h
                card.bind(on_release=lambda *_a, c=c, card=card: self._close_pinned(c, card))
                self.sticky.add_widget(card)
                self._sticky_cards.append(card)
        for card, (c, top, h) in zip(self._sticky_cards, chosen):
            card.width = max(0, self.sticky.width - dp(32))
            card.pos = (self.sticky.x + dp(16), top - h)

    def _label_pinned(self, e):
        card = TapCard(
            orientation="horizontal", size_hint=(1, None), height=dp(48),
            padding=(dp(16), dp(6), dp(12), dp(6)), spacing=dp(10), radius=dp(12),
        )
        card.add_widget(Dot(e["color"]))
        card.add_widget(mk_label(esc(e["name"]), size=14, bold=True, markup=True))
        card.add_widget(mk_label(
            fmt_eur0(e["eur"]), size=14, bold=True, halign="right", size_hint=(None, 1), width=dp(100),
        ))
        chev = Chevron()
        chev.open = True
        card.add_widget(chev)
        return card

    def _close_pinned(self, coll, card):
        coll.toggle(target_top=card.to_window(0, card.top)[1])

    DUST_USD = 0.01

    @staticmethod
    def _type_stats(wallets):
        """{type: [count, usd, eur]} over every position (native coin = "coin")."""
        stats: dict = {}
        for w in wallets:
            if w.error:
                continue
            items = [("coin", w.native_usd_value, w.native_eur_value)]
            items += [(t.asset_type or "token", t.usd_value, t.eur_value) for t in w.tokens]
            for typ, usd, eur in items:
                st = stats.setdefault(typ, [0, 0.0, 0.0])
                st[0] += 1
                st[1] += usd or 0.0
                st[2] += eur or 0.0
        return stats

    def _is_hidden(self, usd, eur):
        """Sans valeur connue ou < 1 centime (seulement si les prix sont disponibles)."""
        if not self.priced_ok:
            return False
        if usd is None and eur is None:
            return True
        return usd is not None and usd < self.DUST_USD

    @staticmethod
    def _counts_text(stats):
        order = sorted(stats.items(), key=lambda kv: (-kv[1][0], kv[0]))
        return " · ".join(f"{n} {esc(typ)}" for typ, (n, _u, _e) in order)

    def _label_header(self, e):
        card = TapCard(
            orientation="vertical", size_hint=(1, None), height=dp(114),
            padding=(dp(16), dp(12), dp(16), dp(12)), spacing=dp(8),
        )
        row1 = BoxLayout(size_hint=(1, None), height=dp(26), spacing=dp(10))
        row1.add_widget(Dot(e["color"]))
        row1.add_widget(mk_label(esc(e["name"]), size=15, bold=True, markup=True))
        row1.add_widget(mk_label(
            fmt_eur0(e["eur"]), size=16, bold=True, halign="right", size_hint=(None, 1), width=dp(100),
        ))
        chev = Chevron()
        row1.add_widget(chev)
        card.add_widget(row1)

        card.add_widget(Bar(e["share"], e["color"]))

        n = len(e["wallets"])
        row3 = BoxLayout(size_hint=(1, None), height=dp(18))
        row3.add_widget(mk_label(f"{n} wallet{'s' if n > 1 else ''}", size=12, color=MUTED))
        right_txt = (
            f"{fmt_usd0(e['usd'])} · {fmt_pct(e['share'])}" if e["usd"] is not None else "non valorisé"
        )
        row3.add_widget(mk_label(right_txt, size=12, color=MUTED, halign="right"))
        card.add_widget(row3)
        counts = self._counts_text(self._type_stats(e["wallets"]))
        if counts:
            card.add_widget(WrapLabel(
                text=f"[color={MUTED_HEX}]{counts}[/color]", markup=True, font_size=sp(11), min_height=dp(16),
            ))
        return card, chev

    def _label_body(self, wallets):
        body = Panel(bg=CLEAR, padding=(dp(10), 0, dp(10), dp(10)), spacing=dp(8))
        wallets = sorted(wallets, key=lambda w: -(w.total_eur if (not w.error and w.total_eur is not None) else -1.0))
        mine = {id(w) for w in wallets}
        self.wallet_colls = [c for c in self.wallet_colls if id(c.wallet) not in mine]
        for w in wallets:
            header, chev = self._wallet_header(w)
            coll = Collapsible(
                header, build_body=lambda w=w: self._wallet_body(w),
                chevron=chev, bg=SURFACE2, radius=dp(12),
            )
            coll.wallet = w
            self.wallet_colls.append(coll)
            body.add_widget(coll)
        return body

    def _wallet_header(self, w):
        card = TapCard(
            orientation="horizontal", size_hint=(1, None), height=dp(56),
            padding=(dp(14), dp(8), dp(12), dp(8)), spacing=dp(10), radius=dp(12),
        )
        card.add_widget(Dot(CHAIN_COLORS.get(w.chain, OTHER_COLOR), size=8))
        card.add_widget(mk_label(
            f"[b]{esc(w.chain)}[/b]  [size={int(sp(11))}][color={MUTED_HEX}]{esc(short_addr(w.address))}[/color][/size]",
            size=13, markup=True,
        ))
        if w.error:
            total = f"[color={RED_HEX}]erreur[/color]"
        else:
            total = fmt_eur(w.total_eur)
        card.add_widget(mk_label(
            total, size=13, bold=True, halign="right", markup=True, size_hint=(None, 1), width=dp(84),
        ))
        chev = Chevron()
        card.add_widget(chev)
        return card, chev

    def _wallet_body(self, w):
        body = Panel(bg=CLEAR, padding=(dp(14), 0, dp(14), dp(10)), spacing=dp(2))
        addr_btn = RoundedButton(
            text=f"{short_addr(w.address)}   (toucher pour copier)", size_hint=(1, None), height=dp(30),
            bg=CLEAR, fg=MUTED, bold=False, font_size=sp(11), halign="left",
        )
        addr_btn.bind(size=lambda b, _s: setattr(b, "text_size", (b.width, None)))

        def copy_addr(_b, a=w.address, btn=addr_btn):
            Clipboard.copy(a)
            btn.text = f"{short_addr(a)}   (adresse copiée)"
        addr_btn.bind(on_release=copy_addr)
        body.add_widget(addr_btn)
        if w.error:
            body.add_widget(WrapLabel(
                text=f"[color={RED_HEX}]Erreur : {esc(w.error)}[/color]", markup=True, font_size=sp(12),
            ))
            return body
        if w.warning:
            body.add_widget(WrapLabel(
                text=f"[color={ORANGE_HEX}]{esc(w.warning)}[/color]", markup=True, font_size=sp(11),
            ))

        stats = self._type_stats([w])
        if stats:
            summary = Panel(bg=SURFACE, radius=dp(10), padding=(dp(10), dp(6), dp(10), dp(6)), spacing=dp(2))
            for typ, (n, usd, eur) in sorted(stats.items(), key=lambda kv: -kv[1][2]):
                line = BoxLayout(size_hint=(1, None), height=dp(20))
                line.add_widget(mk_label(f"[b]{n}[/b] {esc(typ)}", size=12, markup=True))
                line.add_widget(mk_label(
                    fmt_eur(eur), size=12, color=MUTED, halign="right", size_hint=(None, 1), width=dp(96)))
                line.add_widget(mk_label(
                    "-", size=12, color=MUTED, halign="center", size_hint=(None, 1), width=dp(16)))
                line.add_widget(mk_label(
                    fmt_usd(usd), size=12, color=MUTED, halign="left", size_hint=(None, 1), width=dp(78)))
                summary.add_widget(line)
            body.add_widget(summary)

        state = {"n": 0, "tail": [], "reveal": False}
        native_hidden = self._is_hidden(w.native_usd_value, w.native_eur_value)
        all_sorted = sorted(w.tokens, key=lambda t: -(t.usd_value or 0.0))
        visible = [t for t in all_sorted if not self._is_hidden(t.usd_value, t.eur_value)]
        hidden_n = (len(all_sorted) - len(visible)) + (1 if native_hidden else 0)
        shown = 0
        if not native_hidden:
            body.add_widget(self._row(
                w.native_symbol, "coin", None, w.native_amount, w.native_usd_value, w.native_eur_value,
            ))
            shown += 1
        ordered = visible[:MAX_ROWS_HARD]
        total_tokens = len(visible)

        def add_rows(count):
            for t in ordered[state["n"]:state["n"] + count]:
                tag = t.asset_type if t.asset_type != "token" else None
                name = t.name if t.name and t.name != t.symbol else None
                body.add_widget(self._row(t.symbol, tag, name, t.amount, t.usd_value, t.eur_value))
            state["n"] = min(len(ordered), state["n"] + count)

        def add_tail():
            """Footer widgets (more button, hidden counts); rebuilt after each page."""
            for wdg in state["tail"]:
                body.remove_widget(wdg)
            state["tail"] = []
            left = len(ordered) - state["n"]
            if left > 0:
                btn = RoundedButton(
                    text=f"Afficher {min(ROWS_PAGE, left)} de plus  ({left} restantes)",
                    size_hint=(1, None), height=dp(44), bg=SURFACE2, font_size=sp(13),
                )

                def more(_b):
                    add_rows(ROWS_PAGE)
                    add_tail()
                btn.bind(on_release=more)
                state["tail"].append(btn)
            total_tokens = len(all_sorted) if state["reveal"] else len(visible)
            if total_tokens > len(ordered) and left <= 0:
                state["tail"].append(WrapLabel(
                    text=f"[color={FAINT_HEX}]+ {total_tokens - len(ordered)} position(s) non affichée(s) (limite de sécurité)[/color]",
                    markup=True, font_size=sp(11),
                ))
            if hidden_n and not state["reveal"] and left <= 0:
                hb = RoundedButton(
                    text=f"{hidden_n} position(s) masquée(s) (sans valeur ou < 1 centime) · Afficher",
                    size_hint=(1, None), height=dp(44), bg=SURFACE2, font_size=sp(12),
                )

                def reveal(_b):
                    state["reveal"] = True
                    for wdg in state["tail"]:
                        body.remove_widget(wdg)
                    state["tail"] = []
                    if native_hidden:
                        body.add_widget(self._row(
                            w.native_symbol, "coin", None, w.native_amount, w.native_usd_value, w.native_eur_value,
                        ))
                    ordered[:] = [t for t in all_sorted][:MAX_ROWS_HARD]
                    add_rows(ROWS_PAGE)
                    add_tail()
                hb.bind(on_release=reveal)
                state["tail"].append(hb)
            elif not shown and not ordered:
                state["tail"].append(WrapLabel(
                    text=f"[color={MUTED_HEX}]Aucune position.[/color]", markup=True, font_size=sp(12),
                ))
            for wdg in state["tail"]:
                body.add_widget(wdg)

        add_rows(MAX_ROWS_PER_WALLET)
        shown += state["n"]
        add_tail()
        return body

    @staticmethod
    def _row(symbol, tag, name, amount, usd, eur):
        left = f"[b]{esc(symbol)}[/b]"
        if tag:
            left += f"  [color={MUTED_HEX}]{esc(tag)}[/color]"
        left += f"\n[color={MUTED_HEX}]{fmt_amount(amount)}[/color]"
        if name:
            left += f"\n[size={int(sp(11))}][color={FAINT_HEX}]{esc(name)}[/color][/size]"
        right = f"[b]{fmt_eur(eur)}[/b]\n[color={MUTED_HEX}]{fmt_usd(usd)}[/color]"
        return Row(left, right)

    # ---------------------------------------------------------------- export

    def copy_text(self):
        if not self.last_text:
            return
        Clipboard.copy(self.last_text)
        self.status_label.text = "Texte copié dans le presse-papier."

    def copy_json(self):
        if not self.last_results:
            return
        Clipboard.copy(report.build_json(self.last_results))
        self.status_label.text = "JSON copié dans le presse-papier."

    def copy_csv(self):
        if not self.last_results:
            return
        Clipboard.copy(report.build_csv(self.last_results))
        self.status_label.text = "CSV copié dans le presse-papier."


if __name__ == "__main__":
    OuahzaApp().run()
