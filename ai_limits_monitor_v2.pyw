"""AI Limits Monitor v2: a glass desktop panel (Windows) that shows how much of each AI subscription is used.

For each enabled service it shows, when the vendor reports it:
  Claude: 5-hour session, weekly (all models), weekly per model, monthly extra-usage credits
  Codex (ChatGPT plan): 5-hour (when the plan has one), weekly, extra per-model limits, model availability
  Gemini (via Antigravity's cached readings), DeepSeek balance, and sign-in status cards for other vendors.
Claude and Codex numbers come from the same internal endpoints the apps use for their own /usage screens
(unofficial: they may change or break at any time), read with the logins Claude Code (~/.claude/.credentials.json)
and Codex (~/.codex/auth.json) keep fresh. Tokens are only read, never written, logged or printed.

Refresh every 5 minutes (click the panel to refresh now). Windows notification when a meter crosses 80 % and
95 % (once per reset window). Token-free snapshot of the last reading: <data_dir>\\latest.json.

Settings: ai_limits_settings.json next to this script (or the file named by AI_LIMITS_SETTINGS), each key
overridable by an AI_LIMITS_<KEY> environment variable; see README.md. Without a settings file the public
defaults apply (local time zone, data in %LOCALAPPDATA%\\ai-limits-monitor, avatars next to the script).

Run: pythonw.exe ai_limits_monitor_v2.pyw          panel (drag = move, double-click = compact/full, right-click = menu)
     python.exe ai_limits_monitor_v2.pyw --once    print one reading to the console
     --demo                                        sample data, plain background, nothing saved (screenshots)
     --snapshot <png> [--full|--compact] [--scroll n] [--backdrop]   render once, save a PNG, exit
"""
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CLAUDE_CRED = os.path.join(HOME, ".claude", ".credentials.json")
CODEX_AUTH = os.path.join(HOME, ".codex", "auth.json")
GROK_AUTH = os.path.join(HOME, ".grok", "auth.json")
T3_AG_CACHE = os.path.join(HOME, ".t3", "caches", "antigravity.json")   # T3 keeps Antigravity's own usage windows here
T3_AG_PROVIDERS = os.path.join(HOME, ".t3", "userdata", "providers", "antigravity")
GEMINI_DIR = os.path.join(HOME, ".gemini")
DSH_CRED = os.path.join(HOME, ".dsh", ".credentials.yaml")             # big fat fish (DeepSeek) Harness account grant
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- settings: ai_limits_settings.json next to the script, then AI_LIMITS_<KEY> environment overrides ----------
ALL_SERVICES = ["Claude", "Codex", "Gemini", "DeepSeek", "Grok", "Kimi", "Qwen", "Doubao", "GLM", "MiniMax"]
DEFAULTS = {
    "data_dir": os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local"), "ai-limits-monitor"),
    "avatar_dir": os.path.join(HERE, "avatars"),
    "services": ["Claude", "Codex", "Gemini", "DeepSeek", "Grok"],   # any of ALL_SERVICES, in panel order
    "time_zone": "local",            # "local", or a fixed UTC offset in hours (e.g. 8)
    "time_zone_label": "",           # shown after times in notes, e.g. "UTC"
    "background": "wallpaper",       # "wallpaper" = frosted desktop wallpaper behind the glass, "plain" = neutral
    "refresh_minutes": 5,
    "alert_levels": [80, 95],        # Windows notification when a meter passes these percentages; [] = none
    "kimi_credentials": "",          # Kimi Code login file; "" = %KIMI_CODE_HOME%\credentials\kimi-code.json
    "gemini_badge": "",              # plan pill on the Gemini card, e.g. "AI Pro"
    "gemini_notes": [],              # extra info lines on the Gemini card (your own labels, never checked)
    "demo": False,                   # sample data, plain background, nothing written (also: --demo)
}
SETTINGS_FILE = os.environ.get("AI_LIMITS_SETTINGS") or os.path.join(HERE, "ai_limits_settings.json")


def _env_value(default, raw):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, list):
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return [float(x) for x in items] if default and isinstance(default[0], (int, float)) else items
    if isinstance(default, (int, float)):
        return float(raw)
    return raw


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})   # "_about" = comment
    except FileNotFoundError:
        pass
    except Exception as e:
        sys.stderr.write(f"ai_limits_settings.json ignored: {type(e).__name__}\n")
    for k in DEFAULTS:
        raw = os.environ.get("AI_LIMITS_" + k.upper())
        if raw:
            try:
                s[k] = _env_value(DEFAULTS[k], raw)
            except ValueError:
                pass
    if "--demo" in sys.argv:
        s["demo"] = True
    for k in ("data_dir", "avatar_dir"):   # empty = default
        s[k] = s[k] or DEFAULTS[k]
    for k in ("data_dir", "avatar_dir", "kimi_credentials"):
        s[k] = os.path.normpath(os.path.expandvars(os.path.expanduser(s[k]))) if s[k] else s[k]
    return s


S = load_settings()
DEMO = bool(S["demo"])
READ_ONLY = DEMO or "--snapshot" in sys.argv   # demo / snapshot runs never write state, the snapshot or alerts
TZ = None if str(S["time_zone"]).lower() == "local" else dt.timezone(dt.timedelta(hours=float(S["time_zone"])))
TZ_SUFFIX = (" " + S["time_zone_label"]) if S["time_zone_label"] else ""
LOG_DIR = S["data_dir"]
STATE_FILE = os.path.join(LOG_DIR, "monitor_state.json")
LATEST_FILE = os.path.join(LOG_DIR, "latest.json")
REFRESH_MS = int(float(S["refresh_minutes"]) * 60 * 1000)
ALERT_LEVELS = tuple(S["alert_levels"])

# ---------- colors ----------
BG = "#16181d"
CARD = "#1e2128"
FG = "#e8eaed"
MUTED = "#9aa0a6"
TRACK = "#2c3038"
GREEN = "#3fb950"
AMBER = "#d29922"
RED = "#f85149"
CLAUDE_ACCENT = "#d97757"
CODEX_ACCENT = "#7aa2f7"
PACE = "#c9d1d9"


def color_for(pct):
    if pct is None:
        return MUTED
    if pct >= 90:
        return RED
    if pct >= 70:
        return AMBER
    return GREEN


# ---------- data ----------
def http_json(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_claude():
    out = {"name": "Claude", "plan": "", "meters": [], "notes": [], "error": None}
    try:
        cred = json.load(open(CLAUDE_CRED, encoding="utf-8"))["claudeAiOauth"]
    except Exception:
        out["error"] = "No Claude login found"
        return out
    tier = (cred.get("rateLimitTier") or "").replace("default_claude_", "").replace("_", " ")
    out["plan"] = (tier or cred.get("subscriptionType") or "").title().replace("Max 5X", "Max 5x").replace("Max 20X", "Max 20x")
    if cred.get("expiresAt") and cred["expiresAt"] / 1000 < time.time():
        out["error"] = "Login token expired: open Claude Code once to refresh"
        return out
    try:
        d = http_json("https://api.anthropic.com/api/oauth/usage", {
            "Authorization": "Bearer " + cred["accessToken"],
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "ai-limits-monitor/1",
        })
    except urllib.error.HTTPError as e:
        out["error"] = f"Claude usage API: HTTP {e.code}"
        return out
    except Exception as e:
        out["error"] = f"Claude usage API: {type(e).__name__}"
        return out

    limits = d.get("limits") or []
    if limits:
        for lim in limits:
            kind = lim.get("kind")
            scope = lim.get("scope") or {}
            model = ((scope.get("model") or {}).get("display_name")) or ""
            surface = scope.get("surface") or ""
            if kind == "session":
                label, window = "5-hour", 5 * 3600
            elif kind == "weekly_all":
                label, window = "Weekly · all models", 7 * 86400
            elif kind == "weekly_scoped":
                label, window = "Weekly · " + (model or str(surface) or "scoped"), 7 * 86400
            else:
                label, window = kind.replace("_", " ").capitalize(), None
            out["meters"].append({
                "key": "claude:" + kind + ":" + (model or str(surface)),
                "label": label, "pct": float(lim.get("percent") or 0),
                "resets_at": parse_iso(lim.get("resets_at")), "window": window,
                "active": bool(lim.get("is_active")),
            })
    else:  # older response shape
        for k, label, window in (("five_hour", "5-hour", 5 * 3600), ("seven_day", "Weekly · all models", 7 * 86400),
                                 ("seven_day_opus", "Weekly · Opus", 7 * 86400)):
            v = d.get(k)
            if v:
                out["meters"].append({"key": "claude:" + k, "label": label, "pct": float(v.get("utilization") or 0),
                                      "resets_at": parse_iso(v.get("resets_at")), "window": window, "active": False})

    ex = d.get("extra_usage") or {}
    if ex.get("is_enabled") and ex.get("monthly_limit"):
        div = 10 ** int(ex.get("decimal_places") or 2)
        used, cap = (ex.get("used_credits") or 0) / div, ex["monthly_limit"] / div
        out["meters"].append({"key": "claude:extra", "label": "Extra usage · month",
                              "pct": float(ex.get("utilization") or 0), "resets_at": None, "window": None,
                              "detail": f"${used:,.2f} of ${cap:,.0f}", "active": False})
    return out


def fetch_codex():
    out = {"name": "Codex", "plan": "", "meters": [], "notes": [], "error": None}
    try:
        auth = json.load(open(CODEX_AUTH, encoding="utf-8"))
        tok = auth.get("tokens") or {}
    except Exception:
        out["error"] = "No Codex login found"
        return out
    try:
        d = http_json("https://chatgpt.com/backend-api/wham/usage", {
            "Authorization": "Bearer " + tok.get("access_token", ""),
            "ChatGPT-Account-Id": tok.get("account_id", ""),
            "User-Agent": "codex_cli_rs",
        })
    except urllib.error.HTTPError as e:
        out["error"] = f"Codex usage API: HTTP {e.code}" + (" (open Codex once to refresh login)" if e.code in (401, 403) else "")
        return out
    except Exception as e:
        out["error"] = f"Codex usage API: {type(e).__name__}"
        return out

    plan = (d.get("plan_type") or "").lower()
    out["plan"] = {"prolite": "Pro Lite", "pro": "Pro", "plus": "Plus", "team": "Team", "business": "Business"}.get(plan, plan.title())

    def window_label(sec):
        if not sec:
            return "Limit"
        if sec <= 6 * 3600:
            return f"{round(sec / 3600)}-hour"
        if sec >= 6 * 86400:
            return "Weekly"
        return f"{round(sec / 86400)}-day"

    def add_rl(rl, prefix, keybase):
        for wkey in ("primary_window", "secondary_window"):
            w = (rl or {}).get(wkey)
            if not w:
                continue
            sec = w.get("limit_window_seconds")
            label = window_label(sec) + ((" · " + prefix) if prefix else "")
            out["meters"].append({"key": f"codex:{keybase}:{sec}", "label": label,
                                  "pct": float(w.get("used_percent") or 0),
                                  "resets_at": w.get("reset_at"), "window": sec, "active": False})

    rl = d.get("rate_limit") or {}
    add_rl(rl, "", "main")
    if not rl.get("secondary_window") and not any(m["window"] and m["window"] <= 6 * 3600 for m in out["meters"]):
        pass  # "No 5-hour limit" note removed (2026-11-09)
    for extra in d.get("additional_rate_limits") or []:
        name = extra.get("limit_name") or extra.get("metered_feature") or extra.get("limit_id") or "model"
        add_rl(extra.get("rate_limit") or extra, str(name), str(name))
    cr = d.get("code_review_rate_limit")
    if cr:
        add_rl(cr, "code review", "review")

    for model, mu in (d.get("model_usage") or {}).items():
        ok = mu.get("available", True)
        when = mu.get("available_at")
        txt = f"{model}: available" if ok else f"{model}: blocked" + (f" until {fmt_time(when)}" if when else "")
        out["notes"].append(("ok" if ok else "bad", txt))
    if rl.get("limit_reached"):
        out["notes"].append(("bad", "Limit reached"))
    return out


def fetch_grok():
    """Grok has no readable plan usage from the shell (grok usage needs its daemon route), so the
    section only shows whether the CLI is signed in: blue dot = signed in, rose = not (2026-11-10)."""
    out = {"name": "Grok", "plan": "", "meters": [], "notes": [], "error": None, "signed_in": False}
    try:
        auth = json.load(open(GROK_AUTH, encoding="utf-8"))
        out["signed_in"] = any(isinstance(v, dict) and v.get("key") for v in (auth or {}).values())
    except Exception:
        pass
    out["notes"].append(("ok", "signed in") if out["signed_in"] else ("bad", "not signed in"))
    return out


# ---- free-account status cards (2026-11-10: sign-in status like Grok, status only, in case
# one day they offer free credits): Kimi, Qwen, Doubao, GLM, MiniMax. Free sign-in only, no plan, no top-up.
# A card shows signed in / not signed in and, only when the vendor itself reports it, a free balance / trial
# quota line (marked FREE). Nothing is estimated or invented.
NPM_BIN = os.path.join(os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming")), "npm")
KIMI_SEEN = os.path.join(LOG_DIR, "kimi_usage_seen.json")
KIMI_CRED = S["kimi_credentials"] or os.path.join(os.environ.get("KIMI_CODE_HOME") or os.path.join(HOME, ".kimi-code"),
                                                  "credentials", "kimi-code.json")


def run_cli(name, args, timeout=25):
    """Run a vendor CLI (npm shim) without a console window; returns (exit code, stdout) or (None, '')."""
    try:
        p = subprocess.run([os.path.join(NPM_BIN, name + ".cmd")] + list(args), capture_output=True, timeout=timeout,
                           creationflags=0x08000000, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except Exception:
        return None, ""


def cli_json(name, args, timeout=25):
    rc, txt = run_cli(name, args, timeout)
    try:
        return rc, json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        return rc, None


def status_card(name, signed, lines=None, cap=None, error=None):
    """Status-only card (like Grok). lines = [(kind, text)], kind ok / bad / info / free (a real free-credit line)."""
    out = {"name": name, "plan": "", "meters": [], "notes": [], "error": error, "signed_in": bool(signed),
           "status_lines": list(lines or []), "cap": cap}
    out["notes"].append(("ok", "signed in") if signed else ("bad", "not signed in"))
    return out


_KIMI = {"t": 0.0, "res": None}


def fetch_kimi():
    """Kimi Code (OAuth device login; login file set by kimi_credentials / KIMI_CODE_HOME). Signed in = the login file has a
    refresh token. Free quota source: GET api.kimi.com/coding/v1/usages with the access token, only while that token is
    still valid (the monitor never refreshes or rewrites it). An empty reply = the free account has no plan quota."""
    try:
        d = json.load(open(KIMI_CRED, encoding="utf-8"))
    except Exception:
        d = None
    signed = bool(d and d.get("refresh_token"))
    if not signed:
        return status_card("Kimi", False, [("info", "free sign-in: kimi login")], "free sign-in missing")
    lines, cap = [], "free account"
    try:
        if time.time() < (d.get("expires_at") or 0) - 30 and time.time() - _KIMI["t"] > 600:
            _KIMI["res"] = http_json("https://api.kimi.com/coding/v1/usages",
                                     {"Authorization": "Bearer " + d["access_token"], "User-Agent": "ai-limits-monitor/2"})
            _KIMI["t"] = time.time()
            try:   # remember the last real reading (no token in it) for the times the 15-minute token is idle
                if READ_ONLY:
                    raise RuntimeError("demo / snapshot runs write nothing")
                os.makedirs(LOG_DIR, exist_ok=True)
                json.dump({"checked": dt.date.today().isoformat(), "keys": list(_KIMI["res"])[:6]},
                          open(KIMI_SEEN, "w", encoding="utf-8"))
            except Exception:
                pass
    except Exception:
        pass
    res = _KIMI["res"]
    if res is None:
        try:
            seen = json.load(open(KIMI_SEEN, encoding="utf-8"))
        except Exception:
            seen = None
        if seen is None:
            lines.append(("info", "usage not checked yet (sign-in token idle)"))
        elif seen.get("keys"):
            lines.append(("free", f"usage data was reported by Kimi on {seen['checked']}: open Kimi Code"))
        else:
            lines.append(("info", f"no plan quota reported (checked {seen['checked']})"))
    elif res:
        keys = ", ".join(list(res)[:4])
        lines.append(("free", f"usage data reported by Kimi ({keys}): open Kimi Code to read it"))
        cap = "Kimi reports usage data"
    else:
        lines.append(("info", "no plan quota reported  ·  free account"))
    return status_card("Kimi", True, lines, cap)


SIGNIN_STATUS = os.path.join(LOG_DIR, "signin_status.json")


def fetch_qwen():
    """Qwen 千问 = the consumer app https://www.qianwen.com/ signed in inside a browser (the QwenCloud CLI needs a
    separate Alibaba Cloud account and is not used). Browsers keep their cookie store locked and encrypted, so there
    is no live check: the sign-in is recorded by hand in <data_dir>\\signin_status.json
    ({"qwen": {"signed_in": true, "verified": "YYYY-MM-DD", "free_quota": ""}}), and the card says when.
    The account page lists no quota or credits (checked 2026-11-10); if that changes the line goes in 'free_quota'."""
    try:
        s = json.load(open(SIGNIN_STATUS, encoding="utf-8")).get("qwen") or {}
    except Exception:
        s = {}
    if not s.get("signed_in"):
        return status_card("Qwen", False, [("info", "free sign-in: open qianwen.com in Chrome")], "free sign-in missing")
    lines = []
    if s.get("free_quota"):
        lines.append(("free", str(s["free_quota"])))
    lines.append(("info", f"qianwen.com app in Chrome  ·  verified {s.get('verified', '?')}"))
    lines.append(("info", "no quota shown" if not s.get("free_quota") else "quota as shown on the site"))
    return status_card("Qwen", True, lines, "qianwen.com app · free" + (" · quota shown" if s.get("free_quota") else ""))


_ARK = {"t": 0.0, "lines": None, "cap": None}


def fetch_doubao():
    """Doubao / Volcengine Ark (arkcli volc-sso browser login, account = individual Douyin sign-in). Signed in =
    `arkcli auth status`. Free quota = `arkcli usage balance --type free-quota` (the per-model free inference quota
    Volcengine itself reports); plan = `--type plan` (empty = no subscription). Checked every 30 min."""
    rc, st = cli_json("arkcli", ["auth", "status"])
    if st is None:
        return status_card("Doubao", False, [], "sign-in check failed", "arkcli not answering")
    if not st.get("logged_in"):
        return status_card("Doubao", False, [("info", "free sign-in: arkcli auth login volc-sso")], "free sign-in missing")
    if _ARK["lines"] is None or time.time() - _ARK["t"] > 1800:
        lines, cap = [], "free account"
        _, fq = cli_json("arkcli", ["usage", "balance", "--type", "free-quota", "--modality", "LLM", "--page-size", "50"], 50)
        items = (fq or {}).get("items") if isinstance(fq, dict) else None
        if items:
            rem = [int((i.get("free_usage") or {}).get("remaining") or 0) for i in items]
            tot = [int((i.get("free_usage") or {}).get("total") or 0) for i in items]
            if sum(rem) > 0:
                n = len(items)
                per = f"{max(rem) // 1000}k" if len(set(rem)) == 1 else f"{sum(rem) // 1000}k total"
                used = sum(tot) - sum(rem)
                lines.append(("free", f"{n} models × {per} tokens" + ("" if used else ", none used")))
                st_all = {i.get("state") for i in items}
                if st_all == {"Unavailable"}:
                    lines.append(("info", "listed Unavailable until the service is opened"))
                cap = f"free quota: {n} models × {per} tokens"
        elif fq is None:
            lines.append(("info", "free quota: not readable"))
        else:
            lines.append(("info", "no free quota reported"))
        _, pl = cli_json("arkcli", ["usage", "balance", "--type", "plan"], 40)
        if isinstance(pl, dict) and pl.get("items") == []:
            lines.append(("info", "no Agent / Coding Plan"))
        elif isinstance(pl, dict) and pl.get("items"):
            lines.append(("info", "a plan is active: check the Ark console"))
        _ARK.update(t=time.time(), lines=lines, cap=cap)
    return status_card("Doubao", True, _ARK["lines"], _ARK["cap"])


_GLM = {"t": 0.0, "res": None}


def fetch_glm():
    """GLM 智谱 via Z.ai: a free account's API key (ZAI_API_KEY user environment variable, key only ever sent in the
    request header). Signed in = the key is accepted by a token-free model listing (checked every 30 min).
    Z.ai has no balance / quota endpoint for free keys, so none is shown; glm-4.7-flash is the free model
    Z.ai's price list marks free (that is a standing free tier, not a granted credit)."""
    key = user_env("ZAI_API_KEY")
    if not key:
        return status_card("GLM", False, [("info", "free account key: z.ai API keys")], "free key missing")
    if _GLM["res"] is None or time.time() - _GLM["t"] > 1800:
        try:
            http_json("https://api.z.ai/api/paas/v4/models", {"Authorization": "Bearer " + key, "User-Agent": "ai-limits-monitor/2"})
            res = ("ok", None)
        except urllib.error.HTTPError as e:
            res = ("rejected", e.code)
        except Exception:
            res = ("net", None)
        _GLM.update(t=time.time(), res=res)
    kind, code = _GLM["res"]
    if kind == "ok":
        return status_card("GLM", True, [("info", "glm-4.7-flash is Z.ai's free model"),
                                          ("info", "no balance or quota reported for free keys")], "Z.ai free key")
    if kind == "rejected":
        return status_card("GLM", False, [("bad", f"key rejected (HTTP {code})")], "key rejected")
    return status_card("GLM", True, [("info", "key set  ·  check failed (network)")], "Z.ai key set")


_MM = {"t": 0.0, "lines": None, "cap": None}


def fetch_minimax():
    """MiniMax (mmx CLI, OAuth browser login to platform.minimax.io; no key, no plan). Signed in = `mmx auth status`
    finds credentials. Quota = `mmx quota show` (Token Plan counters, every 30 min): all totals 0 means no plan /
    no quota; any non-zero total is flagged FREE so a trial grant would be noticed (it is the vendor's own number)."""
    rc, st = cli_json("mmx", ["auth", "status", "--non-interactive"])
    if not isinstance(st, dict) or st.get("error") or not st.get("method"):
        return status_card("MiniMax", False, [("info", "free sign-in: mmx auth login")], "free sign-in missing")
    if _MM["lines"] is None or time.time() - _MM["t"] > 1800:
        lines, cap = [], "free account"
        _, q = cli_json("mmx", ["quota", "show", "--non-interactive"], 40)
        rows = (q or {}).get("model_remains") if isinstance(q, dict) else None
        if rows is None:
            lines.append(("info", "quota: not readable"))
        else:
            got = [r for r in rows if (r.get("current_interval_total_count") or 0) > 0 or (r.get("current_weekly_total_count") or 0) > 0]
            if got:
                txt = "  ·  ".join(f"{r.get('model_name')} {r.get('current_interval_total_count') or 0}/5h, "
                                   f"{r.get('current_weekly_total_count') or 0}/wk" for r in got)
                lines.append(("free", "quota reported: " + txt))
                cap = "MiniMax reports quota"
            else:
                lines.append(("info", "no plan quota reported  ·  free account"))
        _MM.update(t=time.time(), lines=lines, cap=cap)
    return status_card("MiniMax", True, _MM["lines"], _MM["cap"])


def fetch_gemini():
    """Gemini via Antigravity (2026-11-10). Antigravity reports its own usage windows (the numbers of its /usage
    command); T3 Code stores the latest reading in ~/.t3/caches/antigravity.json, read here (no token touched).
    Windows as Antigravity reports them (e.g. Gemini 5-hour + weekly, shared Claude & GPT windows). Sign-in = the cache
    says authenticated, or ~/.gemini/oauth_creds.json exists. GEMINI_API_KEY, if set, is checked with a token-free
    models listing (connected / rejected); the API reports neither usage nor billing tier, so none is shown.
    The plan pill and any extra lines come only from the settings (gemini_badge, gemini_notes), never from a check."""
    out = {"name": "Gemini", "plan": "", "meters": [], "notes": [], "error": None}
    try:
        d = json.load(open(T3_AG_CACHE, encoding="utf-8"))
    except Exception:
        d = None
    signed = bool(d and (d.get("auth") or {}).get("status") == "authenticated")
    if not signed and os.path.isfile(os.path.join(GEMINI_DIR, "oauth_creds.json")):
        signed = True
    out["signed"] = signed
    out["notes"].append(("ok", "Antigravity signed in") if signed else ("bad", "Antigravity not signed in"))
    ul = (d or {}).get("usageLimits") or {}
    checked = parse_iso(ul.get("checkedAt"))
    gone = 0
    for w in ul.get("windows") or []:
        pct = w.get("usedPercent")
        if pct is None:
            continue
        wid = w.get("id") or ""
        mins = w.get("windowDurationMins")
        resets = parse_iso(w.get("resetsAt"))
        if resets and resets < time.time():
            gone += 1        # that window reset after the reading: its new value is unknown
            continue
        kind = "session" if w.get("kind") == "session" else "weekly_all"
        span = "5-hour" if mins == 300 else ("Weekly" if mins == 10080 else (w.get("label") or "").split("·")[-1].strip())
        who = (w.get("label") or "").split("·")[0].replace("(shared)", "").strip()
        who = "" if who == "Gemini" else who.replace("Gemini 4 ", "").replace("Gemini ", "")
        short = {"Claude & GPT": "C&G "}.get(who, who + " " if who else "")
        out["meters"].append({"key": f"gemini:{kind}:{wid}", "label": f"{who} · {span}" if who else span,
                              "order": (0 if not who else 2 if "Claude" in who else 1, 0 if kind == "session" else 1),
                              "short": short + span.replace("5-hour", "5h").replace("Weekly", "Week"),
                              "minor": bool(who),   # compact view skips these while unused
                              "pct": float(pct), "resets_at": resets, "window": mins * 60 if mins else None,
                              "active": False})
    out["meters"].sort(key=lambda m: m.pop("order"))
    idle = [m for m in out["meters"] if m["minor"] and m["pct"] <= 0]
    if idle:   # unused secondary windows: one quiet line instead of empty bars (still real 0 % readings)
        out["meters"] = [m for m in out["meters"] if m not in idle]
        groups = {}
        for m in idle:
            who, _, span = m["short"].rpartition(" ")
            groups.setdefault(who.replace("C&G", "Claude & GPT"), []).append(span.replace("Week", "wk"))
        out["notes"].append(("info", "unused: " + "  ·  ".join(f"{k} {', '.join(v)}" for k, v in groups.items())))
    if not ul:
        out["notes"].append(("info", "Antigravity quota: unknown (no reading yet)"))
    elif checked and time.time() - checked > 1800:
        out["notes"].append(("info", f"Antigravity reading from {fmt_time(checked)}{TZ_SUFFIX}"))
    if gone:
        out["notes"].append(("info", "a window reset since the last reading: unknown"))
    if S["gemini_badge"]:
        out["badge"] = S["gemini_badge"]    # plan pill, from the settings (e.g. "AI Pro")
    out["notes"].append(gemini_key_note())
    for line in S["gemini_notes"] or []:
        out["notes"].append(("info", str(line)))
    return out


_GKEY = {"t": 0.0, "note": None}


def gemini_key_note():
    """State of the Gemini API key (GEMINI_API_KEY user env). Checked with a token-free models listing at most
    every 30 min; the key goes only in the request header, never in a URL, log or the panel. The API reports
    no usage or rate-limit numbers (no headers, no endpoint), so none are shown."""
    key = user_env("GEMINI_API_KEY")
    if not key:
        return ("bad", "API key not connected")
    if _GKEY["note"] and time.time() - _GKEY["t"] < 1800:
        return _GKEY["note"]
    try:
        http_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", {"x-goog-api-key": key})
        note = ("ok", "API key connected · usage not reported")
    except urllib.error.HTTPError as e:
        note = ("bad", f"API key rejected (HTTP {e.code})")
    except Exception:
        note = ("info", "API key set · check failed (network)")
    _GKEY.update(t=time.time(), note=note)
    return note


def user_env(name):
    """A user environment variable, from the registry if the process env predates it; the value stays in memory."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        import winreg as _wr
        with _wr.OpenKey(_wr.HKEY_CURRENT_USER, "Environment") as k:
            return _wr.QueryValueEx(k, name)[0]
    except Exception:
        return None


def _money(v):
    try:
        return float(v)
    except Exception:
        return None


# ---- big fat fish: 蓝色大肥鱼 = DeepSeek (the community's name for its blue whale-maid girl, who loves white rice) ----
def fetch_big_fat_fish():
    """Big fat fish (DeepSeek) account balance (2026-11-10), read the way DeepSeek Harness reads it: the Platform account grant
    from ~/.dsh/.credentials.yaml goes in the x-dsh-auth-token header to platform.deepseek.com
    /api/v0/users/get_user_summary. normal_wallets = topped-up credit, bonus_wallets = granted credit.
    The grant is only read, never written, logged or printed. No reading -> error text, never a made-up 0."""
    out = {"name": "DeepSeek", "plan": "", "meters": [], "notes": [], "error": None}
    try:
        import re as _re
        txt = open(DSH_CRED, encoding="utf-8").read()
        tok = _re.search(r"deepseek-account-platform/default:.*?token:\s*(\S+)", txt, _re.S).group(1)
    except Exception:
        out["error"] = "DeepSeek Harness not signed in"
        return out
    try:
        d = http_json("https://platform.deepseek.com/api/v0/users/get_user_summary",
                      {"x-dsh-auth-token": tok, "Accept": "application/json", "User-Agent": "ai-limits-monitor/2"})
    except urllib.error.HTTPError as e:
        out["error"] = f"DeepSeek balance: HTTP {e.code}"
        return out
    except Exception as e:
        out["error"] = f"DeepSeek balance: {type(e).__name__}"
        return out
    if d.get("code") == 40003:
        out["error"] = "DeepSeek sign-in expired: open DeepSeek Harness"
        return out
    biz = ((d.get("data") or {}).get("biz_data")) or {}
    if d.get("code") != 0 or not isinstance(biz, dict) or "normal_wallets" not in biz:
        out["error"] = "DeepSeek balance: unknown reply"
        return out

    def total(rows):
        t = {}
        for r in rows or []:
            v = _money(r.get("balance"))
            if v is not None:
                c = r.get("currency") or "?"
                t[c] = t.get(c, 0.0) + v
        return t
    topped, granted = total(biz.get("normal_wallets")), total(biz.get("bonus_wallets"))
    spent = {r.get("currency") or "?": _money(r.get("amount")) for r in biz.get("total_costs") or []}
    cur = next(iter(topped or granted or {"CNY": 0}), "CNY")
    sym = {"CNY": "¥", "USD": "$"}.get(cur, cur + " ")
    tp, gr = topped.get(cur, 0.0), granted.get(cur, 0.0)
    bal = tp + gr
    out["balance"] = {"currency": cur, "total": round(bal, 2), "topped_up": round(tp, 2), "granted": round(gr, 2),
                      "spent_total": round(spent.get(cur) or 0, 2)}
    out["pills"] = [(f"{sym}{bal:,.2f}", "bad" if bal <= 0 else "ok")]
    # rice bowls, not coins: the big fat fish's balance is her bowl of rice (2026-11-10)
    out["notes"].append(("rice_bad" if bal <= 0 else "rice_ok", f"Balance {sym}{bal:,.2f}" + ("  ·  API calls fail" if bal <= 0 else "")))
    out["notes"].append(("info", f"topped-up {sym}{tp:,.2f}  ·  granted {sym}{gr:,.2f}"
                         + (f"  ·  spent {sym}{spent[cur]:,.2f}" if spent.get(cur) else "")))
    return out


AVATAR_KEY = {"Claude": "claude", "Codex": "codex_gpt", "Grok": "grok", "Gemini": "gemini", "DeepSeek": "deepseek",
              "GLM": "glm", "Kimi": "kimi", "Qwen": "qwen", "Wenxin": "wenxin", "Doubao": "doubao", "MiniMax": "minimax"}
DISPLAY_NAME = {"Codex": "GPT · Codex", "Qwen": "Qwen 千问", "Wenxin": "文心一言", "Doubao": "Doubao 豆包", "GLM": "GLM 智谱"}   # CJK strings are drawn in Microsoft YaHei UI


def fmt_time(ts):
    """Clock time in the configured zone (time_zone setting: local, or a fixed UTC offset)."""
    if not ts:
        return ""
    t = dt.datetime.fromtimestamp(ts, TZ)
    now = dt.datetime.now(TZ)
    if t.date() == now.date():
        return t.strftime("%H:%M")
    return t.strftime("%a %H:%M")


def fmt_left(ts):
    if not ts:
        return ""
    s = int(ts - time.time())
    if s <= 0:
        return "resetting"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


# ---------- alerts ----------
def load_state():
    if DEMO:
        return {}
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    if READ_ONLY:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def toast(title, body):
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{title.replace(chr(39), '')}')) > $null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{body.replace(chr(39), '')}')) > $null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)"
    )
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=0x08000000)
    except Exception:
        pass


def check_alerts(results, st):
    fired = 0
    if READ_ONLY:
        return fired
    alerts = st.setdefault("alerts", {})
    for svc in results:
        for m in svc["meters"]:
            # one alert per level per reset window (monthly meters: per calendar month)
            period = str(int(m["resets_at"])) if m.get("resets_at") else dt.date.today().strftime("%Y-%m")
            rec = alerts.get(m["key"])
            if not rec or rec.get("period") != period:
                rec = {"period": period, "sent": []}
            for lvl in ALERT_LEVELS:
                if m["pct"] >= lvl and lvl not in rec["sent"]:
                    rec["sent"].append(lvl)
                    extra = f", resets in {fmt_left(m['resets_at'])}" if m.get("resets_at") else ""
                    detail = f" ({m['detail']})" if m.get("detail") else ""
                    toast(f"{svc['name']} {m['label']}: {m['pct']:.0f}%", f"Passed {lvl}%{detail}{extra}")
                    fired += 1
            alerts[m["key"]] = rec
    return fired


def write_latest(results):
    if READ_ONLY:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    snap = {"checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "services": results}
    tmp = LATEST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LATEST_FILE)


# ---------- UI (v2: glass panel, per-pixel alpha via UpdateLayeredWindow) ----------
import ctypes.wintypes as wt
import colorsys
import math
import random
import numpy as np
import winreg
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
HANDLE = ctypes.c_void_p
user32.GetDC.restype = HANDLE; user32.GetDC.argtypes = [HANDLE]
user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
user32.GetAncestor.restype = HANDLE; user32.GetAncestor.argtypes = [HANDLE, ctypes.c_uint]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t; user32.GetWindowLongPtrW.argtypes = [HANDLE, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t; user32.SetWindowLongPtrW.argtypes = [HANDLE, ctypes.c_int, ctypes.c_ssize_t]
user32.UpdateLayeredWindow.argtypes = [HANDLE, HANDLE, ctypes.c_void_p, ctypes.c_void_p, HANDLE, ctypes.c_void_p,
                                       wt.DWORD, ctypes.c_void_p, wt.DWORD]
user32.MonitorFromPoint.restype = HANDLE; user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [HANDLE, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = HANDLE; gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.CreateDIBSection.restype = HANDLE
gdi32.CreateDIBSection.argtypes = [HANDLE, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), HANDLE, wt.DWORD]
gdi32.SelectObject.restype = HANDLE; gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.DeleteObject.argtypes = [HANDLE]; gdi32.DeleteDC.argtypes = [HANDLE]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def push_layered(hwnd, img, x, y):
    """Show an RGBA image as the window's pixels (true per-pixel alpha)."""
    w, h = img.size
    r, g, b, a = img.split()
    data = Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a),
                                ImageChops.multiply(r, a), a)).tobytes()
    screen = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(mdc, hbmp)
    blend = BLENDFUNCTION(0, 0, 255, 1)
    user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                               mdc, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, screen)


ACTIVE_WINDOW = 45  # seconds since the last transcript write that still counts as "in use"


def newest_mtime(root, depth):
    best = 0.0
    try:
        for e in os.scandir(root):
            if e.is_file() and e.name.endswith(".jsonl"):
                best = max(best, e.stat().st_mtime)
            elif depth > 0 and e.is_dir():
                best = max(best, newest_mtime(e.path, depth - 1))
    except OSError:
        pass
    return best


def active_now():
    if DEMO:
        return {}
    now = time.time()
    claude = newest_mtime(os.path.join(HOME, ".claude", "projects"), 3)
    codex = 0.0
    for d in (dt.date.today(), dt.date.today() - dt.timedelta(days=1)):
        codex = max(codex, newest_mtime(os.path.join(HOME, ".codex", "sessions", d.strftime("%Y"), d.strftime("%m"),
                                                     d.strftime("%d")), 0))
    grok = newest_mtime(os.path.join(HOME, ".grok", "sessions"), 2)
    gem = newest_file_mtime(os.path.join(GEMINI_DIR, "tmp"), 2)          # Gemini CLI
    try:
        for e in os.scandir(T3_AG_PROVIDERS):                               # Antigravity agents run by T3
            gem = max(gem, newest_file_mtime(os.path.join(e.path, "antigravity-acp", "conversations"), 0))
    except OSError:
        pass
    big_fat_fish = newest_mtime(os.path.join(HOME, ".dsh", "sessions"), 3)   # 蓝色大肥鱼 = DeepSeek Harness
    return {"Claude": now - claude < ACTIVE_WINDOW, "Codex": now - codex < ACTIVE_WINDOW,
            "Grok": now - grok < ACTIVE_WINDOW, "Gemini": now - gem < ACTIVE_WINDOW,
            "DeepSeek": now - big_fat_fish < ACTIVE_WINDOW}


def newest_file_mtime(root, depth):
    best = 0.0
    try:
        for e in os.scandir(root):
            if e.is_file():
                best = max(best, e.stat().st_mtime)
            elif depth > 0 and e.is_dir():
                best = max(best, newest_file_mtime(e.path, depth - 1))
    except OSError:
        pass
    return best


def rainbow(h, sat=0.55):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, sat, 1.0)
    return np.array([b * 255, g * 255, r * 255], np.float32)   # BGR


def push_pm(hwnd, data, w, h, x, y):
    """Like push_layered, but data is already premultiplied BGRA bytes."""
    screen = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(mdc, hbmp)
    blend = BLENDFUNCTION(0, 0, 255, 1)
    user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                               mdc, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, screen)


def work_rect(x, y):
    """Work area (monitor minus taskbar) of the monitor holding (x, y): left, top, width, height in physical px."""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wt.POINT(x, y), 2), ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def monitor_rect(x, y):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wt.POINT(x, y), 2), ctypes.byref(mi))
    r = mi.rcMonitor
    return r.left, r.top, r.right - r.left, r.bottom - r.top


# palette: low saturation, matched to the wallpaper (slate glass, sage / sand / rose, terracotta / dusty blue)
TXT = (236, 240, 244)
SUB = (186, 196, 208)
FAINT = (150, 161, 176)
OK_C = (152, 188, 162)
WARN_C = (216, 192, 142)
HIGH_C = (214, 152, 152)
ACCENT = {"Claude": (208, 152, 130), "Codex": (142, 174, 204), "Grok": (182, 150, 170),
          "Gemini": (150, 168, 214), "DeepSeek": (128, 160, 200),
          "Kimi": (186, 186, 212), "Qwen": (160, 150, 206), "Doubao": (200, 170, 140), "GLM": (150, 152, 166),
          "MiniMax": (214, 140, 150)}
SLATE = (26, 32, 46)

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
AVATAR_DIR = S["avatar_dir"]     # <key>_512.png heads + <key>_body.png figures (or Full_Body\<key>_body.png)


METER_C = {
    "session": (138, 196, 186),       # 5-hour: soft teal
    "weekly_all": (176, 158, 216),    # weekly: soft purple (all weekly meters share it)
    "weekly_scoped": (176, 158, 216), # weekly per model (Fable): same purple
    "extra": (214, 150, 162),         # extra-usage credits: rose
    "codex": (176, 158, 216),         # Codex weekly: same purple
}


def meter_color(m):
    k = m.get("key", "")
    if k.startswith("codex"):
        return METER_C["codex"]
    for part in ("weekly_scoped", "weekly_all", "session", "extra"):
        if part in k:
            return METER_C[part]
    return OK_C


def tone(pct):
    if pct is None:
        return FAINT
    if pct >= 90:
        return HIGH_C
    if pct >= 70:
        return WARN_C
    return OK_C


def plain_backdrop(w, h):
    """Neutral blue-grey behind the glass (background = "plain", and every demo run): no desktop content."""
    top, bottom = np.array((92, 102, 120), np.float32), np.array((58, 66, 82), np.float32)
    k = np.linspace(0.0, 1.0, max(1, h), dtype=np.float32)[:, None, None]
    col = top * (1 - k) + bottom * k
    return Image.fromarray(np.repeat(col, max(1, w), axis=1).astype(np.uint8), "RGB")


class Wallpaper:
    def __init__(self):
        self.path, self.mtime, self.img = None, None, None

    def get(self):
        path = None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
                path = winreg.QueryValueEx(k, "WallPaper")[0]
        except OSError:
            pass
        if not path or not os.path.exists(path):
            path = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Themes\TranscodedWallpaper")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        if path != self.path or mt != self.mtime:
            try:
                self.img = Image.open(path).convert("RGB")
                self.path, self.mtime = path, mt
            except Exception:
                self.img = None
        return self.img

    def sample(self, x, y, w, h):
        """The wallpaper pixels behind screen rect (x, y, w, h), 'fill' placement per monitor."""
        if DEMO or str(S["background"]).lower() == "plain":
            return plain_backdrop(w, h)
        img = self.get()
        if img is None:
            return Image.new("RGB", (w, h), SLATE)
        mx, my, mw, mh = monitor_rect(x + w // 2, y + h // 2)
        iw, ih = img.size
        s = max(mw / iw, mh / ih)
        ox, oy = (iw * s - mw) / 2, (ih * s - mh) / 2
        x0, y0 = (x - mx + ox) / s, (y - my + oy) / s
        bw, bh = w / s, h / s
        bw, bh = min(bw, iw), min(bh, ih)  # a panel taller than the screen: stretch the sample, never a negative box
        x0 = min(max(0.0, x0), iw - bw)   # near a screen edge: slide the sample inside the image
        y0 = min(max(0.0, y0), ih - bh)
        return img.resize((w, h), Image.BILINEAR, box=(x0, y0, x0 + bw, y0 + bh))


class Panel:
    W = 344          # card width, logical px
    M = 26           # transparent margin for the soft shadow
    SS = 2           # supersampling

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.st = load_state()
        self.compact = bool(self.st.get("compact"))
        self.results, self.checked, self.busy = None, None, False
        self.q = queue.Queue()
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.wall = Wallpaper()
        self.fonts = {}
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh)
        self.menu.add_command(label="Compact / full", command=self.toggle)
        self.menu.add_command(label="Open usage pages", command=self.open_pages)
        self.menu.add_separator()
        self.menu.add_command(label="Hide  (Ctrl+Alt+H shows it)", command=self.hide)
        self.menu.add_command(label="Close monitor", command=self.root.destroy)
        r = self.root
        r.bind("<ButtonPress-1>", self.start_drag)
        r.bind("<B1-Motion>", self.on_drag)
        r.bind("<ButtonRelease-1>", self.end_drag)
        r.bind("<Double-Button-1>", lambda e: self.toggle())
        r.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        r.bind("<MouseWheel>", self.on_wheel)
        self.drag, self.moved, self.sdrag, self.vp = None, False, None, None
        self.size = (10, 10)
        pos = self.st.get("pos2")
        if pos:
            self.x, self.y = pos
        else:
            sw = r.winfo_screenwidth()
            self.x, self.y = sw - self.px(self.W + 2 * self.M) - self.px(4), self.px(30)
        r.geometry(f"10x10+{self.x}+{self.y}")
        r.deiconify()
        r.update_idletasks()
        self.hwnd = user32.GetAncestor(ctypes.c_void_p(r.winfo_id()), 2)
        ex = user32.GetWindowLongPtrW(self.hwnd, -20)
        user32.SetWindowLongPtrW(self.hwnd, -20, ex | 0x80000 | 0x80)   # WS_EX_LAYERED | WS_EX_TOOLWINDOW
        self.render()
        self.refresh()
        r.after(200, self.poll_queue)
        r.after(30000, self.tick)
        self.anim_t = time.time()
        self.active = {}
        threading.Thread(target=self.watch_activity, daemon=True).start()
        self.hidden = False
        self.cmdq = queue.Queue()
        threading.Thread(target=self.hotkey_loop, daemon=True).start()
        threading.Thread(target=self.show_event_loop, daemon=True).start()
        if self.st.get("hidden") and not READ_ONLY:
            self.hide()
        r.after(40, self.animate)

    def px(self, v):
        return int(round(v * self.scale))

    def u(self, v):  # logical -> supersampled pixels
        return int(round(v * self.scale * self.SS))

    @staticmethod
    def has_cjk(s):
        return any(ord(c) >= 0x2E80 for c in s)

    def font(self, size, bold=False, cjk=False):
        key = (size, bold, cjk)
        if key not in self.fonts:
            f = None
            if cjk:   # Comic Sans has no CJK glyphs: strings with Chinese use Microsoft YaHei UI (ASCII fallback below)
                for name, idx in (("msyhbd.ttc" if bold else "msyh.ttc", 1), ("msyhbd.ttc" if bold else "msyh.ttc", 0),
                                  ("simsun.ttc", 0)):
                    try:
                        f = ImageFont.truetype(os.path.join(FONT_DIR, name), self.u(size), index=idx)
                        break
                    except Exception:
                        f = None
            if f is None:
                try:
                    f = ImageFont.truetype(os.path.join(FONT_DIR, "comicbd.ttf" if bold else "comic.ttf"), self.u(size))
                except Exception:
                    f = ImageFont.load_default()
            self.fonts[key] = f
        return self.fonts[key]

    # ---- interaction ----
    def in_list(self, e):
        """True when the pointer is over the compact view's scrollable provider list."""
        vp = getattr(self, "vp", None)
        if not (self.compact and vp and vp["max"] > 0):
            return False
        lx, ly = e.x_root - self.x, e.y_root - self.y
        return vp["x"] <= lx <= vp["x"] + vp["w"] and vp["y"] <= ly <= vp["y"] + vp["h"]

    def scroll_by(self, d):
        vp = getattr(self, "vp", None)
        if vp:
            vp["target"] = max(0.0, min(vp["max"], vp["target"] + d))

    def on_wheel(self, e):
        if self.compact and getattr(self, "vp", None):
            self.scroll_by(-e.delta / 120.0 * self.vp["step"])

    def start_drag(self, e):
        self.drag = (e.x_root - self.x, e.y_root - self.y)
        self.moved = False
        self.sdrag = (e.y_root, self.vp["target"]) if self.in_list(e) else None   # drag in the list scrolls it

    def on_drag(self, e):
        if self.sdrag:
            if abs(e.y_root - self.sdrag[0]) > 3:
                self.moved = "scroll"
            vp = self.vp
            vp["target"] = vp["off"] = max(0.0, min(vp["max"], self.sdrag[1] - (e.y_root - self.sdrag[0])))
            return
        if self.drag:
            self.moved = True
            self.x, self.y = e.x_root - self.drag[0], e.y_root - self.drag[1]
            self.root.geometry(f"+{self.x}+{self.y}")

    def end_drag(self, e):
        if self.moved == "scroll":
            # snap to the nearest row so a whole provider is in view
            vp = self.vp
            vp["target"] = max(0.0, min(vp["max"], round(vp["target"] / vp["step"]) * vp["step"]))
        elif self.moved:
            self.st["pos2"] = [self.x, self.y]
            save_state(self.st)
            self.render()      # re-sample the wallpaper at the new spot
        else:
            self.refresh()
        self.drag = None
        self.sdrag = None

    def toggle(self):
        self.compact = not self.compact
        self.st["compact"] = self.compact
        save_state(self.st)
        self.render()

    def hide(self):
        self.hidden = True
        user32.ShowWindow(self.hwnd, 0)          # SW_HIDE
        self.st["hidden"] = True
        save_state(self.st)

    def show(self):
        if not self.hidden:
            return
        self.hidden = False
        user32.ShowWindow(self.hwnd, 8)          # SW_SHOWNA (no focus steal)
        self.st["hidden"] = False
        save_state(self.st)
        self.render()

    def hotkey_loop(self):
        # Ctrl+Alt+H toggles the panel (global hotkey, own message loop on this thread)
        if not user32.RegisterHotKey(None, 1, 0x0002 | 0x0001 | 0x4000, ord("H")):
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:            # WM_HOTKEY
                self.cmdq.put("toggle_hide")

    def show_event_loop(self):
        # a second launch (Start menu / shortcut) signals this event to bring the panel back
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = wintypes.HANDLE
        ev = k32.CreateEventW(None, False, False, SHOW_EVENT)
        while ev:
            if k32.WaitForSingleObject(wintypes.HANDLE(ev), 0xFFFFFFFF) == 0:
                self.cmdq.put("show")

    def open_pages(self):
        for link in ("https://claude.ai/settings/usage", "https://chatgpt.com/codex/settings/usage"):
            os.startfile(link)

    # ---- data ----
    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.render()
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        res = []
        for f in ALL_FETCHERS:
            try:
                res.append(f())
            except Exception as e:
                res.append({"name": {"big_fat_fish": "DeepSeek", "glm": "GLM", "minimax": "MiniMax"}.get(f.__name__[6:], f.__name__[6:].title()),
                            "plan": "", "meters": [], "notes": [],
                            "error": type(e).__name__})
        self.q.put(res)

    def poll_queue(self):
        while True:
            try:
                cmd = self.cmdq.get_nowait()
            except (queue.Empty, AttributeError):
                break
            if cmd == "show" or (cmd == "toggle_hide" and self.hidden):
                self.show()
            elif cmd == "toggle_hide":
                self.hide()
        try:
            res = self.q.get_nowait()
        except queue.Empty:
            pass
        else:
            if self.results:
                old = {s["name"]: s for s in self.results}
                for s in res:
                    if s["error"] and not s["meters"] and old.get(s["name"], {}).get("meters"):
                        s["meters"] = old[s["name"]]["meters"]
                        s["stale"] = True
            self.results, self.checked, self.busy = res, time.time(), False
            try:
                if check_alerts(res, self.st) and self.hidden:
                    self.show()
                save_state(self.st)
                write_latest(res)
            except Exception:
                pass
            self.render()
            self.root.after(REFRESH_MS, self.refresh)
        self.root.after(300, self.poll_queue)

    def tick(self):
        self.render()
        self.root.after(30000, self.tick)

    # ---- drawing primitives (all on RGBA layers, composited with masks: no dark fringes) ----
    def paint(self, layer, mask, rgba, pos=(0, 0)):
        solid = Image.new("RGBA", mask.size, rgba[:3] + (0,))
        a = rgba[3] if len(rgba) > 3 else 255
        solid.putalpha(mask.point(lambda v: v * a // 255))
        layer.alpha_composite(solid, pos)

    def rrect(self, layer, x0, y0, x1, y1, r, rgba):
        X0, Y0, X1, Y1 = self.u(x0), self.u(y0), self.u(x1), self.u(y1)
        m = Image.new("L", (max(1, X1 - X0), max(1, Y1 - Y0)), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, m.width - 1, m.height - 1), radius=self.u(r), fill=255)
        self.paint(layer, m, rgba, (X0, Y0))

    def text(self, layer, x, y, s, size, rgba, bold=False, anchor="l"):
        runs = self.runs(s, size, bold)
        w = sum(f.getlength(t) for t, f in runs)
        X = self.u(x) - (int(w) if anchor == "r" else 0)
        asc = max(f.getmetrics()[0] for _, f in runs)
        desc = max(f.getmetrics()[1] for _, f in runs)
        m = Image.new("L", (int(w) + 4, asc + desc + 4), 0)
        d, px_ = ImageDraw.Draw(m), 0.0
        for t, f in runs:   # baseline-aligned runs: Latin in Comic Sans, Chinese in Microsoft YaHei UI
            d.text((px_, asc), t, font=f, fill=255, anchor="ls")
            px_ += f.getlength(t)
        self.paint(layer, m, rgba, (X, self.u(y)))

    def runs(self, s, size, bold):
        """Split s into runs of Latin / CJK text, each with its own font (spaces stay with the run before them)."""
        out = []
        for ch in s:
            cj = self.has_cjk(ch) if ch != " " else (out[-1][1] if out else False)
            if out and out[-1][1] == cj:
                out[-1][0] += ch
            else:
                out.append([ch, cj])
        return [(t, self.font(size, bold, cj)) for t, cj in out] or [("", self.font(size, bold))]

    def avatar_img(self, key, size):
        """Round portrait + soft halo that fades out fully. Returns (RGBA image, pad) in supersampled px."""
        cache = self.__dict__.setdefault("_av", {})
        n = self.u(size)
        if (key, n) in cache:
            return cache[(key, n)]
        try:
            im = Image.open(os.path.join(AVATAR_DIR, key + "_512.png")).convert("RGBA").resize((n, n), Image.LANCZOS)
        except Exception:
            im = Image.new("RGBA", (n, n), SUB + (255,))
        if key == "deepseek_big":   # the big fat fish (DeepSeek) mascot of the compact view
            im = im.transpose(Image.FLIP_LEFT_RIGHT)   # she faces right, toward the usage
        big = Image.new("L", (n * 4, n * 4), 0)
        ImageDraw.Draw(big).ellipse((0, 0, n * 4 - 1, n * 4 - 1), fill=255)
        im.putalpha(big.resize((n, n), Image.LANCZOS))
        blur = max(self.u(3), int(n * 0.07))
        pad = blur * 3 + self.u(3)          # room for the whole blur tail: nothing gets cut
        canvas = Image.new("RGBA", (n + 2 * pad, n + 2 * pad), (0, 0, 0, 0))
        halo = Image.new("L", canvas.size, 0)
        g = self.u(2)
        ImageDraw.Draw(halo).ellipse((pad - g, pad - g, pad + n + g, pad + n + g), fill=255)
        self.paint(canvas, halo.filter(ImageFilter.GaussianBlur(blur)), (255, 255, 255, 95))
        canvas.alpha_composite(im, (pad, pad))
        ring = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(ring).ellipse((pad, pad, pad + n - 1, pad + n - 1), outline=255, width=max(1, self.u(1.2)))
        self.paint(canvas, ring, (255, 255, 255, 140))
        cache[(key, n)] = (canvas, pad)
        return cache[(key, n)]

    def body_img(self, key, height):
        """Full-body character (transparent PNG, trimmed) scaled to `height` logical px, supersampled. None if missing."""
        cache = self.__dict__.setdefault("_body", {})
        n = self.u(height)
        if (key, n) not in cache:
            try:
                p = os.path.join(AVATAR_DIR, "Full_Body", key + "_body.png")   # asset library layout
                if not os.path.exists(p):
                    p = os.path.join(AVATAR_DIR, key + "_body.png")              # repo layout
                im = Image.open(p).convert("RGBA")
                im = im.crop(im.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox() or (0, 0, *im.size))
                w = max(1, round(im.width * n / im.height))
                im = im.resize((w, n), Image.LANCZOS)
                if n < im.height * 2:   # keep line art crisp after the big downscale
                    rgb = im.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
                    rgb.putalpha(im.getchannel("A"))
                    im = rgb
                cache[(key, n)] = im
            except Exception:
                cache[(key, n)] = None
        return cache[(key, n)]

    def figure(self, layer, cx, top, height, key):
        """Full-body figure centered on cx with a soft ground shadow; returns its logical (x, y, w, h)."""
        im = self.body_img(key, height)
        if im is None:
            return None
        X, Y = self.u(cx) - im.width // 2, self.u(top)
        sh = Image.new("L", (im.width + self.u(24), self.u(16)), 0)
        ImageDraw.Draw(sh).ellipse((self.u(12), self.u(4), sh.width - self.u(12), sh.height - self.u(4)), fill=255)
        self.paint(layer, sh.filter(ImageFilter.GaussianBlur(self.u(3))), (8, 12, 22, 70),
                   (X - self.u(12), Y + im.height - self.u(9)))
        layer.alpha_composite(im, (X, Y))
        k = self.scale * self.SS
        return (X / k, Y / k, im.width / k, im.height / k)

    def rice_bowl(self, layer, x, y, size, tint):
        """Small bowl of steaming white rice (the big fat fish's balance icon), drawn 4x and scaled down: crisp."""
        cache = self.__dict__.setdefault("_bowl", {})
        n = self.u(size)
        key = (n, tint)
        if key not in cache:
            k = 4
            S = n * k
            im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            lw = max(2, S // 22)
            # rice mound (soft white dome) behind the rim
            d.ellipse((S * 0.20, S * 0.30, S * 0.80, S * 0.68), fill=(246, 246, 242, 255))
            for gx, gy in ((0.36, 0.42), (0.50, 0.37), (0.62, 0.44), (0.44, 0.52), (0.57, 0.53)):
                d.ellipse((S * gx - lw, S * gy - lw * 0.6, S * gx + lw, S * gy + lw * 0.6), fill=(218, 222, 226, 255))
            # bowl: glassy tinted half-ellipse with a light rim and a little foot
            d.chord((S * 0.10, S * 0.30, S * 0.90, S * 0.96), 0, 180, fill=tint + (235,))
            d.chord((S * 0.20, S * 0.62, S * 0.80, S * 0.92), 0, 180, fill=tuple(min(255, c + 28) for c in tint) + (120,))
            d.rounded_rectangle((S * 0.36, S * 0.90, S * 0.64, S * 0.98), radius=lw, fill=tint + (235,))
            d.line((S * 0.11, S * 0.63, S * 0.89, S * 0.63), fill=(255, 255, 255, 200), width=lw)
            # steam: two soft curls
            for sx in (0.40, 0.58):
                d.arc((S * (sx - 0.07), S * 0.02, S * (sx + 0.07), S * 0.16), 200, 340, fill=(255, 255, 255, 150), width=lw)
                d.arc((S * (sx - 0.07), S * 0.12, S * (sx + 0.07), S * 0.26), 20, 160, fill=(255, 255, 255, 150), width=lw)
            cache[key] = im.resize((n, n), Image.LANCZOS)
        layer.alpha_composite(cache[key], (self.u(x), self.u(y)))

    def avatar(self, layer, x, y, size, key):
        img, pad = self.avatar_img(key, size)
        layer.alpha_composite(img, (self.u(x) - pad, self.u(y) - pad))

    def text_w(self, s, size, bold=False):
        return sum(f.getlength(t) for t, f in self.runs(s, size, bold)) / (self.scale * self.SS)

    # ---- content ----
    COLW = 404                       # full view: one column's width (two columns on a tall list / short screen)
    ROW, GAP, VIS = 74, 8, 3
    FIG_MIN, FIG_MAX = 128, 236      # full view: full-body figure height range (logical px)          # compact list: row height, gap, rows in view (the rest scrolls)

    def key_meter(self, svc):
        """The meter that binds first (highest %), skipping extra credits and idle minor windows."""
        ms = [m for m in svc["meters"] if not m.get("key", "").endswith(":extra") and not (m.get("minor") and m["pct"] <= 0)]
        return max(ms, key=lambda m: m["pct"]) if ms else None

    def short_label(self, m):
        return m.get("short") or (m["label"].replace("Weekly · all models", "Week").replace("Weekly · ", "")
                                  .replace("Extra usage · month", "Extra $").replace("Weekly", "Week").replace("5-hour", "5h"))

    def fit(self, s, size, width, bold=False):
        while s and self.text_w(s, size, bold) > width:
            s = s[:-2].rstrip() + "…"
        return s

    def build_compact(self):
        W = self.W = 400
        self._bars = []
        BIG, BX = 104, 8
        col = BX + BIG + 8             # the provider list starts here
        av = 60
        tx = col + 8 + av + 14         # name / bar column
        rows = self.results or []
        ROW, GAP, VIS = self.ROW, self.GAP, self.VIS
        list_h = max(ROW, len(rows) * ROW + (len(rows) - 1) * GAP)
        vis_h = min(list_h, VIS * ROW + (VIS - 1) * GAP)
        y0 = 14
        H = y0 + VIS * ROW + (VIS - 1) * GAP + 14
        under = Image.new("RGBA", (self.u(W), self.u(H)), (0, 0, 0, 0))
        top = Image.new("RGBA", under.size, (0, 0, 0, 0))
        lu = Image.new("RGBA", (self.u(W), self.u(list_h)), (0, 0, 0, 0))
        lt = Image.new("RGBA", lu.size, (0, 0, 0, 0))
        if not rows:
            self.text(lt, tx, 26, "Loading…", 12, SUB + (255,))
        y = 0
        for svc in rows:
            name = svc["name"]
            self.rrect(lu, col - 2, y, W - 14, y + ROW, 18, (255, 255, 255, 18))
            self.rrect(lu, col - 2, y + 18, col + 1, y + ROW - 18, 1.5, ACCENT.get(name, SUB) + (190,))
            self.avatar(lt, col + 10, y + (ROW - av) / 2, av, AVATAR_KEY.get(name, "codex_gpt"))
            self._avs.append((name, col + 10, y + (ROW - av) / 2, av, True))
            dn = DISPLAY_NAME.get(name, name)
            self.text(lt, tx, y + 10, dn, 12.5, TXT + (250,), True)
            if svc.get("badge"):
                bx = tx + self.text_w(dn, 12.5, True) + 8
                bw = self.text_w(svc["badge"], 9.5, True) + 12
                self.rrect(lu, bx, y + 12, bx + bw, y + 29, 8.5, ACCENT.get(name, SUB) + (70,))
                self.text(lt, bx + 6, y + 13, svc["badge"], 9.5, TXT + (240,), True)
            right, rcol, cap, capc, frac, mcol = "", SUB, "", FAINT, None, None
            km = self.key_meter(svc)
            if km:
                mcol = meter_color(km)
                right, rcol = f"{self.short_label(km)} {km['pct']:.0f}%", (HIGH_C if km["pct"] >= 90 else mcol)
                frac = max(0.0, min(km["pct"], 100.0)) / 100.0
                cap = "  ·  ".join(f"{self.short_label(m)} {m['pct']:.0f}%" for m in svc["meters"]
                                   if m is not km and not (m.get("minor") and m["pct"] <= 0))
            elif "signed_in" in svc:
                right, rcol = ("signed in", (124, 168, 222)) if svc["signed_in"] else ("signed out", HIGH_C)
                cap = svc.get("cap") or "status only"
            elif svc.get("pills"):
                txt, kind = svc["pills"][0]
                right, rcol = txt, (HIGH_C if kind == "bad" else OK_C)
                rest = [n[1] if isinstance(n, (tuple, list)) else n for n in svc.get("notes", [])[1:2]]
                cap = rest[0] if rest else ""
            if svc.get("error"):
                cap, capc = ("last values · " if svc.get("stale") else "") + svc["error"], WARN_C
            bx0, bx1 = tx, W - 28
            if right:
                self.text(lt, bx1, y + 11.5, right, 11, rcol + (255,), True, anchor="r")
                if svc.get("pills") and not km:   # big fat fish (DeepSeek): rice bowl beside the balance
                    self.rice_bowl(lt, bx1 - self.text_w(right, 11, True) - 22, y + 9.5, 17, rcol)
            self.rrect(lu, bx0, y + 37, bx1, y + 42, 2.5, (255, 255, 255, 34))
            if frac:
                self.rrect(lt, bx0, y + 37, max(bx0 + 5, bx0 + (bx1 - bx0) * frac), y + 42, 2.5, mcol + (235,))
            elif frac is None and right:   # status rows: a short colored stroke instead of a meter
                self.rrect(lt, bx0, y + 37, bx0 + 28, y + 42, 2.5, rcol + (200,))
            if cap:
                self.text(lt, tx, y + 49, self.fit(cap, 9.5, bx1 - bx0), 9.5, capc + (235,))
            y += ROW + GAP
        self._list = {"u": lu, "t": lt, "h": list_h, "y": y0, "vis": vis_h}
        self._big = (BX, (H - BIG) / 2, BIG)    # drawn by the animation loop, not baked in
        return under, top, H

    def build_content(self):
        # full view taller than its screen (e.g. the 1440 px display): rebuild once with smaller figures
        self.FIG_MIN, self.FIG_MAX = Panel.FIG_MIN, Panel.FIG_MAX
        self.cols = 1
        out = self.build_content_once()
        if not self.compact:
            # work area of the panel's monitor (screen minus taskbar): never taller than that (2026-11-10).
            # Too tall in one column -> two columns when the screen is wide enough, then smaller figures if still needed.
            _, _, mw, mh = work_rect(self.x + 100, self.y + 10)
            tall = lambda o: (o[2] + 2 * self.M) * self.scale > mh
            if tall(out) and (2 * self.COLW + 2 * self.M) * self.scale <= mw:
                self.cols = 2
                out = self.build_content_once()
            if tall(out):
                self.FIG_MIN, self.FIG_MAX = 96, 190
                out = self.build_content_once()
            if tall(out):
                self.FIG_MIN, self.FIG_MAX = 80, 150
                out = self.build_content_once()
        return out

    def build_content_once(self):
        self._big = None
        self._avs = []
        self._figs = []
        self._list = None
        if self.compact:
            return self.build_compact()
        COLW = self.COLW
        cols = getattr(self, "cols", 1)
        W = self.W = COLW * cols
        self._bars = []
        under = Image.new("RGBA", (self.u(W), self.u(2400)), (0, 0, 0, 0))
        top = Image.new("RGBA", under.size, (0, 0, 0, 0))
        y = 14                          # header avatar removed (2026-11-10)
        if not self.results:
            self.text(top, 20, y + 10, "Loading…", 12, SUB + (255,))
            return under, top, y + 44
        X0, XR = 134, COLW - 26        # content column (the full-body figure stands left of it); one column's coordinates
        FCX = 74                       # figure column center
        cards = []                     # drawn one by one first (heights are needed to balance the columns), placed below
        for svc in self.results:
            acc = ACCENT.get(svc["name"], SUB)
            cu = Image.new("RGBA", (self.u(COLW), self.u(700)), (0, 0, 0, 0))
            ct = Image.new("RGBA", cu.size, (0, 0, 0, 0))
            bars0 = len(self._bars)
            cy = 0
            if "signed_in" in svc:   # status-only card: name, dot + status, then the vendor-reported lines
                dc = (124, 168, 222) if svc["signed_in"] else HIGH_C
                dn = DISPLAY_NAME.get(svc["name"], svc["name"])
                self.text(ct, X0, cy, dn, 12.5, TXT + (250,), True)
                sx = X0 + self.text_w(dn, 12.5, True) + 12
                self.rrect(ct, sx, cy + 7, sx + 8, cy + 15, 4, dc + (255,))
                self.text(ct, sx + 14, cy + 1, "signed in" if svc["signed_in"] else "not signed in", 10.5, dc + (255,), True)
                cy += 26
                for kind, txt in svc.get("status_lines", []):   # free-account cards: real vendor-reported lines only
                    if kind == "free":   # the vendor shows free credits / trial quota: highlighted
                        self.rrect(cu, X0 - 4, cy - 2, XR, cy + 18, 9, OK_C + (60,))
                        self.text(ct, X0 + 4, cy, self.fit("FREE  " + txt, 10.5, XR - X0 - 8), 10.5, OK_C + (255,), True)
                    else:
                        self.text(ct, X0, cy, self.fit(txt, 10.5, XR - X0), 10.5, FAINT + (255,))
                    cy += 19
            else:
                if svc.get("badge"):   # plan pill (Gemini: AI Pro)
                    bw = self.text_w(svc["badge"], 10.5, True) + 18
                    self.rrect(cu, X0, cy, X0 + bw, cy + 21, 10.5, acc + (70,))
                    self.text(ct, X0 + 9, cy + 2, svc["badge"], 10.5, TXT + (245,), True)
                    cy += 31
                if svc.get("error"):
                    self.text(ct, X0, cy, ("last values · " if svc.get("stale") else "") + svc["error"], 10.5,
                              WARN_C + (255,))
                    cy += 20
                for m in svc["meters"]:
                    self.text(ct, X0, cy, m["label"], 12, TXT + (240,))
                    self.text(ct, XR, cy, f"{m['pct']:.0f}%", 12, (HIGH_C if m["pct"] >= 90 else meter_color(m)) + (255,), True, anchor="r")
                    cy += 22
                    bx0, bx1 = X0, XR
                    self.rrect(cu, bx0, cy, bx1, cy + 5, 2.5, (255, 255, 255, 34))
                    frac = max(0.0, min(m["pct"], 100.0)) / 100.0
                    if frac > 0:
                        self.rrect(ct, bx0, cy, max(bx0 + 5, bx0 + (bx1 - bx0) * frac), cy + 5, 2.5,
                                   meter_color(m) + (235,))
                        self._bars.append((bx0, max(bx0 + 5, bx0 + (bx1 - bx0) * frac), cy, meter_color(m), svc["name"], m.get("key", "")))
                    if m.get("window") and m.get("resets_at"):
                        f2 = (time.time() - (m["resets_at"] - m["window"])) / m["window"]
                        if 0 < f2 < 1:
                            tx_ = bx0 + (bx1 - bx0) * f2
                            self.rrect(ct, tx_ - 1, cy - 3, tx_ + 1, cy + 8, 1, (255, 255, 255, 200))
                    cy += 11
                    sub = m.get("detail") or ""
                    if m.get("resets_at"):
                        sub = (sub + "  ·  " if sub else "") + f"resets {fmt_time(m['resets_at'])}  ·  in {fmt_left(m['resets_at'])}"
                    if sub:
                        self.text(ct, X0, cy, self.fit(sub, 10.5, XR - X0), 10.5, FAINT + (255,))
                        cy += 19
                    cy += 7
                for n in svc.get("notes", []):
                    kind, txt = n if isinstance(n, (tuple, list)) else ("info", n)
                    col = {"ok": (124, 168, 222), "bad": HIGH_C}.get(kind, FAINT)
                    if kind.startswith("rice"):   # big fat fish balance: a rice bowl, not a coin
                        self.rice_bowl(ct, X0 - 2, cy - 0.5, 17, HIGH_C if kind == "rice_bad" else OK_C)
                        self.text(ct, X0 + 20, cy, self.fit(txt, 10.5, XR - X0 - 20), 10.5, SUB + (255,))
                    elif kind in ("ok", "bad"):
                        self.rrect(ct, X0 + 1, cy + 6, X0 + 7, cy + 12, 3, col + (255,))
                        self.text(ct, X0 + 14, cy, self.fit(txt, 10.5, XR - X0 - 14), 10.5, SUB + (255,))
                    else:
                        self.text(ct, X0, cy, self.fit(txt, 10.5, XR - X0), 10.5, FAINT + (255,))
                    cy += 19
                cy -= 4
            # card: tall enough for the figure; the meters sit centered beside it
            fmin = self.FIG_MIN if "signed_in" not in svc else (self.FIG_MIN - 24 if self.FIG_MIN > 100 else 80)
            fh = int(max(fmin, min(self.FIG_MAX, cy + 4)))
            ch = max(cy + 26, fh + 14)
            dy = round((ch - cy) / 2)
            key = AVATAR_KEY.get(svc["name"], "codex_gpt")
            probe = self.body_img(key, 100)
            if probe is not None:   # wide figures (tails, wings, hair) stay inside their column
                fh = min(fh, int((2 * (X0 - FCX) - 14) * probe.height / probe.width))
            bars = self._bars[bars0:]
            del self._bars[bars0:]
            cards.append({"svc": svc, "acc": acc, "cu": cu, "ct": ct, "cy": cy, "ch": ch, "dy": dy, "fh": fh,
                          "key": key, "bars": bars})
        # columns: one, or two balanced by height (a prefix of the cards in each, so the order stays readable)
        hs = [c["ch"] + 8 for c in cards]
        split = len(cards)
        if cols == 2 and len(cards) > 1:
            split = min(range(1, len(cards)), key=lambda k: max(sum(hs[:k]), sum(hs[k:])))
        colcards = [cards[:split], cards[split:]][:cols] if cols == 2 else [cards]
        ybot = y
        for ci, group in enumerate(colcards):
            ox, y = ci * COLW, 14
            for c in group:
                svc, cy, ch, dy, fh, key = c["svc"], c["cy"], c["ch"], c["dy"], c["fh"], c["key"]
                off = (self.u(ox), self.u(y + dy))
                under.alpha_composite(c["cu"].crop((0, 0, c["cu"].width, self.u(cy + 8))), off)
                top.alpha_composite(c["ct"].crop((0, 0, c["ct"].width, self.u(cy + 8))), off)
                self._bars.extend((a + ox, b + ox, cc + y + dy, d, e, f) for a, b, cc, d, e, f in c["bars"])
                # inner glass card with the colored edge, like the wallpaper's cards
                self.rrect(under, ox + 12, y, ox + COLW - 12, y + ch, 16, (255, 255, 255, 16))
                self.rrect(under, ox + 12, y + 16, ox + 15, y + ch - 16, 1.5, c["acc"] + (200,))
                fr = self.figure(top, ox + FCX, y + (ch - fh) / 2, fh, key)
                if fr:
                    self._figs.append((svc["name"], key, fh, fr))
                else:   # no full-body file: fall back to the round head
                    self.avatar(top, ox + FCX - 30, y + (ch - 60) / 2, 60, key)
                    self._avs.append((svc["name"], ox + FCX - 30, y + (ch - 60) / 2, 60, False))
                y += ch + 8
            ybot = max(ybot, y) if ci else y
        y = ybot
        self.text(top, 20, y, ("sample data  ·  " if DEMO else "") + "tick = even pace  ·  click refresh  ·  double-click compact",
                  9.5, FAINT + (190,))
        y += 22
        return under, top, y + 4

    def render(self):
        try:
            under, top, h_log = self.build_content()
        except Exception:
            return
        S, M, W = self.SS, self.M, self.W
        cw, ch = self.u(W), self.u(h_log)
        tw, th = cw + 2 * self.u(M), ch + 2 * self.u(M)
        out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        mx = self.u(M)
        card = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(card).rounded_rectangle((mx, mx, mx + cw - 1, mx + ch - 1), radius=self.u(20), fill=255)
        # soft shadow
        sh = Image.new("L", (tw, th), 0)
        sh.paste(card.crop((0, 0, tw, th - self.u(6))), (0, self.u(6)))
        sh = sh.filter(ImageFilter.GaussianBlur(self.u(13)))
        self.paint(out, sh, (8, 12, 22, 110))
        # frosted glass: the wallpaper behind the card, blurred, desaturated, deepened toward slate
        pw, ph = tw // S, th // S
        mx_, my_, mw_, mh_ = work_rect(self.x + pw // 2, self.y + 10)   # work area: the panel never sits under the taskbar
        self.x = max(mx_, min(self.x, mx_ + mw_ - pw))
        self.y = max(my_, min(self.y, my_ + mh_ - ph))
        pad = 40
        bg = self.wall.sample(self.x - pad, self.y - pad, pw + 2 * pad, ph + 2 * pad)
        bg = bg.filter(ImageFilter.GaussianBlur(26)).crop((pad, pad, pad + pw, pad + ph))
        bg = ImageEnhance.Color(bg).enhance(0.7)
        bg = Image.blend(bg, Image.new("RGB", bg.size, SLATE), 0.42)
        bg = bg.resize((tw, th), Image.BICUBIC).convert("RGBA")
        bg.putalpha(card.point(lambda v: v * 248 // 255))
        out.alpha_composite(bg)
        # liquid sheen: soft vertical veil + specular glow along the top edge
        veil = Image.linear_gradient("L").resize((tw, th)).point(lambda v: 255 - v)
        self.paint(out, ImageChops.multiply(veil.point(lambda v: v * 30 // 255), card), (255, 255, 255, 255))
        glow = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(glow).ellipse((mx - self.u(40), mx - self.u(70), mx + int(cw * 0.75), mx + self.u(46)), fill=255)
        glow = ImageChops.multiply(glow.filter(ImageFilter.GaussianBlur(self.u(24))), card)
        self.paint(out, glow, (255, 255, 255, 22))
        # content
        out.alpha_composite(under.crop((0, 0, cw, ch)), (mx, mx))
        out.alpha_composite(top.crop((0, 0, cw, ch)), (mx, mx))
        # hairline rim: brighter at the top, fading down
        rim = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(rim).rounded_rectangle((mx, mx, mx + cw - 1, mx + ch - 1), radius=self.u(20),
                                              outline=255, width=max(1, self.u(1)))
        fade = Image.linear_gradient("L").resize((tw, th)).point(lambda v: 150 - v * 110 // 255)
        self.paint(out, ImageChops.multiply(rim, fade), (255, 255, 255, 255))
        img = out.resize((pw, ph), Image.LANCZOS)
        self.size = img.size
        self.root.geometry(f"{pw}x{ph}+{self.x}+{self.y}")
        self.last_img = img
        self.list_pm = None
        L = getattr(self, "_list", None)
        if L:
            # compact provider list: its own layer, scrolled inside a fixed viewport every frame
            lim = L["u"].copy()
            lim.alpha_composite(L["t"])
            sc = self.scale
            lw, lh = round(W * sc), max(1, round(L["h"] * sc))
            lim = lim.resize((lw, lh), Image.LANCZOS)
            a = np.asarray(lim, dtype=np.uint16)
            al = a[..., 3:4]
            self.list_pm = np.ascontiguousarray(np.concatenate(
                [a[..., 2:3] * al // 255, a[..., 1:2] * al // 255, a[..., 0:1] * al // 255, al], axis=2).astype(np.float32))
            vh = round(L["vis"] * sc)
            old = self.vp or {}
            mx_ = max(0, lh - vh)
            self.vp = {"x": round(M * sc), "y": round((M + L["y"]) * sc), "w": lw, "h": vh, "max": mx_,
                       "step": (self.ROW + self.GAP) * sc,
                       "off": min(old.get("off", 0.0), mx_), "target": min(old.get("target", 0.0), mx_)}
        else:
            self.vp = None
        self.set_base(img)
        self.push_frame()

    # ---- animation: pulsing glow + particles drifting left along each bar ----
    FPS = 60

    def set_base(self, img):
        a = np.asarray(img, dtype=np.uint8)
        alpha = a[..., 3:4].astype(np.uint16)
        pm = (a[..., :3].astype(np.uint16) * alpha // 255).astype(np.uint8)
        self.base_pm = np.ascontiguousarray(np.concatenate([pm[..., 2:3], pm[..., 1:2], pm[..., 0:1], a[..., 3:4]], axis=2))
        sc = self.scale
        old = getattr(self, "fx", [])
        self.fx = []
        for i, (x0, x1, y, col, svcname, mkey) in enumerate(self._bars):
            X0, X1 = (self.M + x0) * sc, (self.M + x1) * sc
            Y = (self.M + y + 2.5) * sc
            light = tuple(min(255, int(c + (255 - c) * 0.45)) for c in col)
            # soft halo around the filled part of the bar
            pad = int(7 * sc)
            gw, gh = int(X1 - X0) + 2 * pad, int(5 * sc) + 2 * pad
            g = Image.new("L", (max(1, gw), max(1, gh)), 0)
            ImageDraw.Draw(g).rounded_rectangle((pad, pad, gw - pad, gh - pad), radius=int(2.5 * sc), fill=255)
            g = np.asarray(g.filter(ImageFilter.GaussianBlur(3.2 * sc)), dtype=np.float32) / 255.0
            prev = old[i]["parts"] if i < len(old) else []
            fx = {"x0": X0, "x1": X1, "y": Y, "col": np.array(light[::-1], np.float32),
                  "glow": g, "gx": int(X0) - pad, "gy": int(Y - gh / 2), "phase": i * 1.3, "parts": prev, "svc": svcname, "key": mkey}
            n = max(0, min(14, int((X1 - X0) / (16 * sc))))
            fx["parts"] = [p for p in prev if p["x"] <= X1][:n]
            while len(fx["parts"]) < n:
                fx["parts"].append(self.spawn(fx, fresh=True))
            self.fx.append(fx)
        self.rings = []
        for name, ax, ay, asz, in_list in getattr(self, "_avs", []):
            pad = int(12 * sc)
            n = int(asz * sc)
            ring = Image.new("L", (n + 2 * pad, n + 2 * pad), 0)
            ImageDraw.Draw(ring).ellipse((pad - int(3 * sc), pad - int(3 * sc), pad + n + int(3 * sc), pad + n + int(3 * sc)),
                                         outline=255, width=max(2, int(3 * sc)))
            ring = np.asarray(ring.filter(ImageFilter.GaussianBlur(3.5 * sc)), dtype=np.float32) / 255.0
            self.rings.append({"svc": name, "map": ring, "list": in_list,
                               "x": (self.M + ax) * sc - pad, "y": (ay if in_list else self.M + ay) * sc - pad})
        for name, key, fh, (fx_, fy_, fw_, fh_) in getattr(self, "_figs", []):
            # in-use halo for a full-body figure: a soft glow around its silhouette (outside it only)
            gcache = self.__dict__.setdefault("_figglow", {})
            gk = (key, fh, sc)
            if gk not in gcache:
                im = self.body_img(key, fh)
                pad = int(10 * sc)
                al = im.getchannel("A").resize((max(1, round(fw_ * sc)), max(1, round(fh_ * sc))), Image.LANCZOS)
                big = Image.new("L", (al.width + 2 * pad, al.height + 2 * pad), 0)
                big.paste(al, (pad, pad))
                glow = big.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(3.5 * sc))
                g = np.clip(np.asarray(glow, dtype=np.float32) / 255.0 * 1.3, 0, 1)
                g = g * (1.0 - np.asarray(big, dtype=np.float32) / 255.0) * 0.8   # outside the silhouette only
                gcache[gk] = (g, pad)
            g, pad = gcache[gk]
            self.rings.append({"svc": name, "map": g, "list": False,
                               "x": (self.M + fx_) * sc - pad, "y": (self.M + fy_) * sc - pad})
        self.big = None
        if getattr(self, "_big", None):
            bx, by, bs = self._big
            img, pad = self.avatar_img("deepseek_big", bs)
            cache = self.__dict__.setdefault("_bigcache", {})
            if bs not in cache:
                # pre-drawn poses: 41 lean steps x 4 sub-pixel heights, premultiplied BGRA (smooth and cheap)
                img = img.resize((img.width // self.SS, img.height // self.SS), Image.LANCZOS)
                frames = []
                for li in range(41):
                    lean = li / 40.0
                    row = []
                    for sy in range(4):
                        r = img.rotate(-8.0 * lean, resample=Image.BICUBIC, translate=(lean * 4 * sc, sy / 4.0))
                        a = np.asarray(r, dtype=np.uint16)
                        al = a[..., 3:4]
                        row.append(np.ascontiguousarray(np.concatenate(
                            [a[..., 2:3] * al // 255, a[..., 1:2] * al // 255, a[..., 0:1] * al // 255, al],
                            axis=2).astype(np.uint8)))
                    frames.append(row)
                cache[bs] = frames
            self.big = {"frames": cache[bs], "x": int((self.M + bx) * sc - pad / self.SS),
                        "y": int((self.M + by) * sc - pad / self.SS)}
        if not hasattr(self, "sprites"):
            self.sprites = []
            for r in (1.3, 1.9, 2.6):
                size = int(r * 6 * sc) | 1
                c = size // 2
                yy, xx = np.mgrid[0:size, 0:size]
                d2 = ((xx - c) ** 2 + (yy - c) ** 2) / (r * sc) ** 2
                self.sprites.append((np.exp(-d2 * 0.9) * 0.85 + np.exp(-d2 * 0.18) * 0.25).clip(0, 1).astype(np.float32))

    def is_busy(self, fx):
        if not getattr(self, "active", {}).get(fx.get("svc")):
            return False
        if fx.get("key", "").endswith(":extra"):
            # credits are only spent once a plan limit is used up
            for svc in self.results or []:
                if svc["name"] == fx.get("svc"):
                    return any(m["pct"] >= 100 for m in svc["meters"] if not m["key"].endswith(":extra"))
            return False
        return True

    def watch_activity(self):
        while True:
            try:
                self.active = active_now()
            except Exception:
                pass
            time.sleep(4)

    def spawn(self, fx, fresh=False):
        span = fx["x1"] - fx["x0"]
        start = fx["x1"] - random.uniform(0, 4 * self.scale)
        if fresh:
            start = random.uniform(fx["x0"], fx["x1"])
        return {"x": start, "start": start, "travel": random.uniform(0.35, 1.0) * span,
                "v": random.uniform(14, 42) * self.scale, "dy": random.uniform(-3.2, 3.2) * self.scale,
                "s": random.randrange(3), "wob": random.uniform(0, 6.28), "hue": random.random()}

    def blend(self, f, x, y, amap, col, k):
        h, w = amap.shape
        x, y = int(round(x)), int(round(y))
        H, W = f.shape[:2]
        sx0, sy0 = max(0, -x), max(0, -y)
        dx0, dy0 = max(0, x), max(0, y)
        dx1, dy1 = min(W, x + w), min(H, y + h)
        if dx1 <= dx0 or dy1 <= dy0:
            return
        a = amap[sy0:sy0 + dy1 - dy0, sx0:sx0 + dx1 - dx0] * k
        sub = f[dy0:dy1, dx0:dx1].astype(np.float32)
        sub[..., :3] += a[..., None] * col
        sub[..., 3] = np.maximum(sub[..., 3], a * 255)
        sub[..., :3] = np.minimum(sub[..., :3], sub[..., 3:4])
        f[dy0:dy1, dx0:dx1] = sub.astype(np.uint8)

    def frame_buf(self):
        """Persistent DIB section; frames are drawn straight into its memory (no per-frame copies)."""
        h, w = self.base_pm.shape[:2]
        d = getattr(self, "_dib", None)
        if d and d["size"] == (w, h):
            return d["view"]
        if d:
            gdi32.SelectObject(d["mdc"], d["old"])
            gdi32.DeleteObject(d["bmp"])
            gdi32.DeleteDC(d["mdc"])
        screen = user32.GetDC(None)
        mdc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mdc, bmp)
        view = np.ctypeslib.as_array((ctypes.c_uint8 * (w * h * 4)).from_address(bits.value)).reshape(h, w, 4)
        self._dib = {"size": (w, h), "mdc": mdc, "bmp": bmp, "old": old, "view": view}
        return view

    def present(self):
        d = self._dib
        w, h = d["size"]
        screen = user32.GetDC(None)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(self.x, self.y)), ctypes.byref(wt.SIZE(w, h)),
                                   d["mdc"], ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
        user32.ReleaseDC(None, screen)

    def blend_over(self, f, x, y, src):
        h, w = src.shape[:2]
        x, y = int(round(x)), int(round(y))
        H, W = f.shape[:2]
        sx0, sy0 = max(0, -x), max(0, -y)
        dx0, dy0 = max(0, x), max(0, y)
        dx1, dy1 = min(W, x + w), min(H, y + h)
        if dx1 <= dx0 or dy1 <= dy0:
            return
        sp = src[sy0:sy0 + dy1 - dy0, sx0:sx0 + dx1 - dx0]
        sub = f[dy0:dy1, dx0:dx1].astype(np.float32)
        sub = sp + sub * (1.0 - sp[..., 3:4] / 255.0)
        f[dy0:dy1, dx0:dx1] = sub.clip(0, 255).astype(np.uint8)

    def push_frame(self):
        if getattr(self, "base_pm", None) is None:
            return
        t = time.time()
        f = self.frame_buf()
        np.copyto(f, self.base_pm)
        big = getattr(self, "big", None)
        if big:
            c = t % 7.0                      # 7 s cycle: look right, hold, come back, rest
            if c < 1.2:
                lean = c / 1.2
            elif c < 4.0:
                lean = 1.0
            elif c < 5.2:
                lean = 1.0 - (c - 4.0) / 1.2
            else:
                lean = 0.0
            lean = lean * lean * (3 - 2 * lean)   # smoothstep
            bob = math.sin(t * 1.7) * 1.8 * self.scale
            yi = math.floor(bob)
            sy = int(round((bob - yi) * 4))
            if sy == 4:
                yi, sy = yi + 1, 0
            fr = big["frames"][int(round(lean * 40))][sy]
            self.blend_over(f, big["x"], big["y"] + yi, fr.astype(np.float32))
        act = getattr(self, "active", {})
        vp, lp = getattr(self, "vp", None), getattr(self, "list_pm", None)
        view = None
        if vp and lp is not None:
            off = int(round(vp["off"]))
            vh = min(vp["h"], lp.shape[0] - off)
            view = f[vp["y"]:vp["y"] + vp["h"]]
            src = lp[off:off + vh]
            fade = np.ones((vh, 1, 1), np.float32)
            e = max(1, int(14 * self.scale))
            if off > 0:
                fade[:e, 0, 0] = np.linspace(0.0, 1.0, e)
            if off < vp["max"]:
                fade[-e:, 0, 0] = np.linspace(1.0, 0.0, e)
            self.blend_over(view, vp["x"], 0, src * fade)
            if vp["max"] > 0:   # thin scroll thumb at the right edge
                th = max(int(18 * self.scale), int(vp["h"] * vp["h"] / lp.shape[0]))
                ty = int((vp["h"] - th) * (vp["off"] / vp["max"]))
                key = ("thumb", th)
                cache = self.__dict__.setdefault("_thumb", {})
                if key not in cache:
                    tw = max(3, int(3 * self.scale))
                    m = Image.new("L", (tw * 4, th * 4), 0)
                    ImageDraw.Draw(m).rounded_rectangle((0, 0, tw * 4 - 1, th * 4 - 1), radius=tw * 2, fill=255)
                    cache[key] = np.asarray(m.resize((tw, th), Image.LANCZOS), dtype=np.float32) / 255.0
                self.blend(view, vp["x"] + vp["w"] - int(8 * self.scale), ty, cache[key], np.array([255, 255, 255], np.float32), 0.30)
        for rg in getattr(self, "rings", []):
            if act.get(rg["svc"]):
                k = 0.55 + 0.35 * math.sin(t * 7)
                if rg.get("list"):
                    if view is not None:
                        self.blend(view, rg["x"], rg["y"] - vp["off"], rg["map"], rainbow(t * 0.45, 0.6), k)
                else:
                    self.blend(f, rg["x"], rg["y"], rg["map"], rainbow(t * 0.45, 0.6), k)
        for fx in getattr(self, "fx", []):
            busy = self.is_busy(fx)
            if not busy and fx.get("key", "").endswith(":extra"):
                continue  # extra credits: completely still unless they are actually being spent
            if busy:
                k = 0.5 + 0.32 * math.sin(t * 7.5 + fx["phase"])
                self.blend(f, fx["gx"], fx["gy"], fx["glow"], rainbow(t * 0.35 + fx["phase"] * 0.1, 0.45), k)
            else:
                k = 0.32 + 0.18 * math.sin(t * 2.2 + fx["phase"])
                self.blend(f, fx["gx"], fx["gy"], fx["glow"], fx["col"], k)
            for p in fx["parts"]:
                prog = (p["start"] - p["x"]) / max(1.0, p["travel"])
                fade = math.sin(math.pi * min(1.0, max(0.0, prog)))
                spr = self.sprites[p["s"]]
                yy = fx["y"] + p["dy"] + math.sin(t * 3 + p["wob"]) * 0.8 * self.scale
                pc = rainbow(p["hue"] + t * 0.6 + p["x"] / 140.0) if busy else fx["col"]
                self.blend(f, p["x"] - spr.shape[1] / 2, yy - spr.shape[0] / 2, spr, pc, (1.0 if busy else 0.95) * fade)
        self.present()

    def animate(self):
        now = time.time()
        dt_ = min(0.1, now - self.anim_t)
        self.anim_t = now
        vp = getattr(self, "vp", None)
        if vp and not self.sdrag and abs(vp["target"] - vp["off"]) > 0.3:
            vp["off"] += (vp["target"] - vp["off"]) * min(1.0, dt_ * 12)   # eased scroll
        elif vp and not self.sdrag:
            vp["off"] = vp["target"]
        for fx in getattr(self, "fx", []):
            if fx.get("key", "").endswith(":extra") and not self.is_busy(fx):
                continue
            for i, p in enumerate(fx["parts"]):
                p["x"] -= p["v"] * dt_ * (2.6 if self.is_busy(fx) else 1.0)
                if p["start"] - p["x"] >= p["travel"] or p["x"] < fx["x0"]:
                    fx["parts"][i] = self.spawn(fx)
        if self.hidden:
            self.root.after(250, self.animate)
            return
        try:
            self.push_frame()
        except Exception:
            pass
        self.root.after(int(1000 / self.FPS), self.animate)

    def run(self):
        self.root.mainloop()


FETCHERS = {"Claude": fetch_claude, "Codex": fetch_codex, "Gemini": fetch_gemini,
            "DeepSeek": fetch_big_fat_fish, "Grok": fetch_grok,                     # big fat fish = DeepSeek
            "Kimi": fetch_kimi, "Qwen": fetch_qwen, "Doubao": fetch_doubao,         # free-account status cards
            "GLM": fetch_glm, "MiniMax": fetch_minimax}


# ---------- demo: sample data only (screenshots, trying the look); nothing is read from any account ----------
def demo_results():
    now = time.time()
    H, D = 3600, 86400
    m = lambda key, label, pct, left, window, **kw: dict(key=key, label=label, pct=float(pct), window=window,
                                                         resets_at=(now + left) if left else None, active=False, **kw)
    return {
        "Claude": {"name": "Claude", "plan": "Sample", "notes": [], "error": None, "meters": [
            m("claude:session:", "5-hour", 34, 2 * H + 10 * 60, 5 * H),
            m("claude:weekly_all:", "Weekly · all models", 58, 3 * D + 5 * H, 7 * D),
            m("claude:weekly_scoped:Opus", "Weekly · Opus", 41, 3 * D + 5 * H, 7 * D),
            m("claude:extra", "Extra usage · month", 12, None, None, detail="$6.00 of $50")]},
        "Codex": {"name": "Codex", "plan": "Sample", "error": None,
                  "meters": [m("codex:main:604800", "Weekly", 27, 4 * D + 2 * H, 7 * D)],
                  "notes": [("ok", "frontier model: available")]},
        "Gemini": {"name": "Gemini", "plan": "", "error": None, "badge": "Pro", "meters": [
            dict(m("gemini:session:gemini-5h", "5-hour", 12, 3 * H + 40 * 60, 5 * H), short="5h", minor=False),
            dict(m("gemini:weekly_all:gemini-weekly", "Weekly", 9, 5 * D, 7 * D), short="Week", minor=False)],
            "notes": [("ok", "Antigravity signed in"), ("ok", "API key connected · usage not reported")]},
        "DeepSeek": {"name": "DeepSeek", "plan": "", "meters": [], "error": None,
                     "pills": [("¥12.50", "ok")],
                     "notes": [("rice_ok", "Balance ¥12.50"), ("info", "topped-up ¥10.00  ·  granted ¥2.50")]},
        "Grok": dict(status_card("Grok", True), status_lines=[]),
        "Kimi": status_card("Kimi", True, [("info", "no plan quota reported  ·  free account")], "free account"),
        "Qwen": status_card("Qwen", False, [("info", "free sign-in: open qianwen.com")], "free sign-in missing"),
        "Doubao": status_card("Doubao", True, [("free", "18 models × 500k tokens, none used")], "free quota"),
        "GLM": status_card("GLM", True, [("info", "no balance or quota reported for free keys")], "free key"),
        "MiniMax": status_card("MiniMax", True, [("info", "no plan quota reported  ·  free account")], "free account"),
    }


def _demo_fetcher(name):
    f = lambda: demo_results()[name]
    f.__name__ = "fetch_" + name.lower()
    return f


ALL_FETCHERS = tuple((_demo_fetcher(n) if DEMO else FETCHERS[n]) for n in S["services"] if n in FETCHERS)

SHOW_EVENT = r"Local\AILimitsMonitorShow" + ("Snapshot" if READ_ONLY else "")


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AILimitsMonitor")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if "--once" in sys.argv:
        for s in (f() for f in ALL_FETCHERS):
            print(s["name"], s["plan"], "ERROR: " + s["error"] if s["error"] else "")
            for m in s["meters"]:
                rs = f"resets {fmt_time(m['resets_at'])} ({fmt_left(m['resets_at'])})" if m.get("resets_at") else ""
                print(f"  {m['label']:<24} {m['pct']:5.1f}%  {m.get('detail', '')}  {rs}")
            for n in s["notes"]:
                print("  -", n if isinstance(n, str) else n[1])
            for k, t in s.get("status_lines", []):
                print("   ", "FREE" if k == "free" else "·", t)
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if not READ_ONLY and not single_instance():   # demo / snapshot runs may sit beside the live panel
        k32 = ctypes.windll.kernel32
        k32.OpenEventW.restype = wintypes.HANDLE
        ev = k32.OpenEventW(0x0002, False, SHOW_EVENT)   # EVENT_MODIFY_STATE
        if ev:
            k32.SetEvent(wintypes.HANDLE(ev))
        sys.exit(0)
    p = Panel()
    if "--snapshot" in sys.argv:   # render once with live data, save a PNG for checking, exit
        res = []
        p.worker()
        p.root.after(100, p.poll_queue)
        if "--compact" in sys.argv or "--full" in sys.argv:
            p.compact = "--compact" in sys.argv
        def snap():
            if p.results:
                p.render()
                if "--scroll" in sys.argv and p.vp:
                    p.vp["off"] = p.vp["target"] = min(p.vp["max"], float(sys.argv[sys.argv.index("--scroll") + 1]) * p.vp["step"])
                p.push_frame()
                fr = np.array(p.frame_buf(), dtype=np.float32)       # composed frame, premultiplied BGRA
                al = fr[..., 3:4]
                rgb = np.where(al > 0, fr[..., :3] * 255.0 / np.maximum(al, 1), 0)[..., ::-1]
                shot = Image.fromarray(np.concatenate([rgb, al], axis=2).clip(0, 255).astype(np.uint8), "RGBA")
                if "--backdrop" in sys.argv:   # flatten onto a plain light-grey card (README screenshots)
                    pad = 24
                    bd = Image.new("RGBA", (shot.width + 2 * pad, shot.height + 2 * pad), (226, 229, 234, 255))
                    bd.alpha_composite(shot, (pad, pad))
                    shot = bd.convert("RGB")
                shot.save(sys.argv[sys.argv.index("--snapshot") + 1])
                p.root.destroy()
            else:
                p.root.after(300, snap)
        p.root.after(500, snap)
    p.run()
