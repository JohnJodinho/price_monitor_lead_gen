"""
engines/fingerprint.py — Per-property browser fingerprint randomization.

Each call to build_fetch_profile() returns a FetchProfile with a fully
consistent, internally coherent set of browser identity signals:

  - timezone_id + locale + Accept-Language  (always coherent as a trio)
  - User-Agent + Sec-CH-UA + Sec-CH-UA-Platform  (always from the same OS/version)
  - Screen resolution + viewport  (viewport derived from screen)
  - Device scale factor (1.0 or 2.0)
  - Color scheme
  - hardwareConcurrency + deviceMemory  (injected via page_setup init script)
  - navigator.platform  (matches UA OS)

Usage in a fetch call:
    profile = build_fetch_profile()
    kwargs = {
        ...
        "timezone_id":    profile.timezone_id,
        "locale":         profile.locale,
        "useragent":      profile.user_agent,
        "hide_canvas":    True,
        "dns_over_https": True,
        "extra_headers":  profile.extra_headers,
        "additional_args": profile.additional_args,
        "page_setup":     profile.page_setup_fn,
    }
"""

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Any


# ── Timezone / locale / accept-language pools (always a coherent trio) ────────
_LOCALE_PROFILES = [
    ("America/New_York",    "en-US", "en-US,en;q=0.9"),
    ("America/Chicago",     "en-US", "en-US,en;q=0.9"),
    ("America/Los_Angeles", "en-US", "en-US,en;q=0.9"),
    ("America/Denver",      "en-US", "en-US,en;q=0.9"),
    ("America/Toronto",     "en-CA", "en-CA,en;q=0.9,en-US;q=0.8"),
    ("America/Vancouver",   "en-CA", "en-CA,en;q=0.9"),
    ("Europe/London",       "en-GB", "en-GB,en;q=0.9"),
    ("Europe/Paris",        "fr-FR", "fr-FR,fr;q=0.9,en;q=0.8"),
]

# ── User-Agent / Client Hints / platform pools (coherent per OS/version) ──────
_UA_PROFILES = [
    {
        "user_agent":          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec_ch_ua":           '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform":  '"Windows"',
        "navigator_platform":  "Win32",
    },
    {
        "user_agent":          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec_ch_ua":           '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform":  '"macOS"',
        "navigator_platform":  "MacIntel",
    },
    {
        "user_agent":          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "sec_ch_ua":           '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform":  '"Windows"',
        "navigator_platform":  "Win32",
    },
    {
        "user_agent":          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "sec_ch_ua":           '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform":  '"macOS"',
        "navigator_platform":  "MacIntel",
    },
    {
        "user_agent":          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "sec_ch_ua":           '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform":  '"Windows"',
        "navigator_platform":  "Win32",
    },
    {
        "user_agent":          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "sec_ch_ua":           '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform":  '"macOS"',
        "navigator_platform":  "MacIntel",
    },
]

# ── Screen resolutions: (screen_w, screen_h, viewport_w, viewport_h) ──────────
# viewport height = screen height minus browser chrome (~150px)
_SCREEN_PROFILES = [
    (1920, 1080, 1920, 920),
    (1440, 900,  1440, 750),
    (1536, 864,  1536, 714),
    (1280, 800,  1280, 650),
    (1366, 768,  1366, 618),
    (2560, 1440, 2560, 1290),
    (1600, 900,  1600, 750),
]

_HW_CONCURRENCY = [2, 4, 6, 8, 12, 16]
_DEVICE_MEMORY  = [4, 8, 16]       # GB — realistic desktop values
# Weighted toward dark (matches Scrapling's default) but introduces light variety
_COLOR_SCHEMES  = ["dark", "dark", "dark", "light", "light"]


@dataclass
class FetchProfile:
    # Locale / timezone
    timezone_id:      str
    locale:           str
    accept_language:  str

    # UA / Client Hints
    user_agent:           str
    sec_ch_ua:            str
    sec_ch_ua_platform:   str
    navigator_platform:   str

    # Screen
    screen_w:           int
    screen_h:           int
    viewport_w:         int
    viewport_h:         int
    device_scale_factor: float
    color_scheme:       str

    # Hardware
    hardware_concurrency: int
    device_memory:        int

    # ── Derived helpers ────────────────────────────────────────────────────

    @property
    def extra_headers(self) -> Dict[str, str]:
        return {
            "Accept-Language":   self.accept_language,
            "Sec-CH-UA":         self.sec_ch_ua,
            "Sec-CH-UA-Platform": self.sec_ch_ua_platform,
            "Sec-CH-UA-Mobile":  "?0",
        }

    @property
    def additional_args(self) -> Dict[str, Any]:
        """Passed directly to Playwright's browser.new_context() via Scrapling's additional_args kwarg.
        Overrides StealthySessionMixin's hardcoded 1920×1080 screen/viewport."""
        return {
            "screen":              {"width": self.screen_w, "height": self.screen_h},
            "viewport":            {"width": self.viewport_w, "height": self.viewport_h},
            "device_scale_factor": self.device_scale_factor,
            "color_scheme":        self.color_scheme,
        }

    @property
    def init_script(self) -> str:
        """JS snippet injected before any navigation to spoof navigator properties."""
        platform = self.navigator_platform
        hwc = self.hardware_concurrency
        mem = self.device_memory
        return f"""
(() => {{
    const _define = (obj, prop, val) => {{
        try {{
            Object.defineProperty(obj, prop, {{ get: () => val, configurable: false }});
        }} catch(e) {{}}
    }};
    _define(navigator, 'hardwareConcurrency', {hwc});
    _define(navigator, 'deviceMemory',        {mem});
    _define(navigator, 'platform',            '{platform}');
}})();
"""

    @property
    def page_setup_fn(self) -> Callable:
        """Returns an async callable for Scrapling's page_setup kwarg.
        Injects navigator overrides before the page navigates to the target URL."""
        script = self.init_script

        async def _setup(page):
            try:
                await page.add_init_script(script=script)
            except Exception:
                pass  # non-fatal — fingerprint injection is best-effort

        return _setup


def build_fetch_profile() -> FetchProfile:
    """
    Build a randomised, internally consistent browser fingerprint profile.
    Call once per property fetch — not once per run — to maximise variance
    across requests in the same session.
    """
    tz, locale, accept_lang = random.choice(_LOCALE_PROFILES)
    ua_prof = random.choice(_UA_PROFILES)
    scr_w, scr_h, vp_w, vp_h = random.choice(_SCREEN_PROFILES)

    return FetchProfile(
        timezone_id=tz,
        locale=locale,
        accept_language=accept_lang,
        user_agent=ua_prof["user_agent"],
        sec_ch_ua=ua_prof["sec_ch_ua"],
        sec_ch_ua_platform=ua_prof["sec_ch_ua_platform"],
        navigator_platform=ua_prof["navigator_platform"],
        screen_w=scr_w,
        screen_h=scr_h,
        viewport_w=vp_w,
        viewport_h=vp_h,
        device_scale_factor=random.choice([1.0, 1.0, 2.0]),   # weighted: hi-DPI less common
        color_scheme=random.choice(_COLOR_SCHEMES),
        hardware_concurrency=random.choice(_HW_CONCURRENCY),
        device_memory=random.choice(_DEVICE_MEMORY),
    )
