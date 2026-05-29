#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ParsMon -> Telegram alert monitor.

Reads two pinned messages from a Telegram group:
  1) "Чек #N" status digest  -> counters for points 5 / 8 / 11 / 9 / 2.1
  2) scooter list message     -> S.xxxx numbers per problem category

On change (new Чек, changed counter, or new scooter numbers) it posts a
compact alert to the alert chat so an operator can verify status in the
technical app.

Only standard library is used (urllib). No external deps.

Config via environment variables (see README.md):
    BOT_TOKEN        Telegram bot token (8025342588:....)
    SOURCE_CHAT_ID   group with the pinned messages   (default -1002300878026)
    ALERT_CHAT_ID    chat where alerts are sent        (default -1003727768555)
    CHEK_MSG_ID      message_id of the "Чек #N" message (read mode = forward)
    SCOOTER_MSG_ID   message_id of the scooter message  (read mode = forward)
    READ_MODE        "getupdates" (default) | "forward" | "getchat"
    LOG_CHAT_ID      chat used to read messages in forward mode (default = ALERT_CHAT_ID)
    STATE_FILE       path to persisted state (default ./state.json)

Run modes:
    python monitor.py --selftest   parse the bundled sample messages and print result
    python monitor.py --once       run one poll cycle (default)
    python monitor.py --loop       run continuously (for a server / VPS)
"""

import os
import re
import sys
import json
import time
import html
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SOURCE_CHAT_ID = os.environ.get("SOURCE_CHAT_ID", "-1002300878026")
ALERT_CHAT_ID  = os.environ.get("ALERT_CHAT_ID", "-1003727768555")
CHEK_MSG_ID    = os.environ.get("CHEK_MSG_ID", "").strip()
SCOOTER_MSG_ID = os.environ.get("SCOOTER_MSG_ID", "").strip()
READ_MODE      = os.environ.get("READ_MODE", "getupdates").strip().lower()
LOG_CHAT_ID    = os.environ.get("LOG_CHAT_ID", ALERT_CHAT_ID)
STATE_FILE     = os.environ.get("STATE_FILE", "state.json")
LOOP_SECONDS   = int(os.environ.get("LOOP_SECONDS", "120"))

API = "https://api.telegram.org/bot{token}/{method}"

# The five points the user tracks.  Key -> (display label in Spanish, regex
# prefix for the Чек message, substring to match a category header in the
# scooter message).  NOTE: prefixes/substrings stay in Russian because they
# must match the source messages; only the label is shown to operators.
POINTS = {
    "5":   ("Error crítico",     r"^5\.\s",   "критическая ошибка"),
    "8":   ("Sin conexión",      r"^8\.\s",   "нет связи"),
    "9":   ("Fuera de zona",     r"^9\.\s",   "за зоной завершения"),
    "11":  ("Alarma",            r"^11\.\s",  "тревога"),
    "2.1": ("Batería baja >12h", r"^2\.1\b",  "низкий заряд батареи"),
}
# Fixed order for display.
ORDER = ["5", "8", "9", "11", "2.1"]

# Emoji per status category (replaces ordinal numbers).
EMOJI = {
    "5":   "\U0001f525",   # 🔥
    "8":   "\U0001f4e1",   # 📡
    "9":   "\U0001f6a9",   # 🚩
    "11":  "⚠️", # ⚠️
    "2.1": "⚡",       # ⚡
}

# Max scooters shown per subcategory before truncation.
MAX_SCOOTERS_PER_CAT = 15

# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def _count_before_ts(line):
    """Return the integer that directly precedes 'тс' on a line, or None."""
    m = re.findall(r"(\d+)\s*тс", line)
    return int(m[-1]) if m else None

def parse_chek(text):
    """Parse the 'Чек #N' digest.  Returns dict with chek number, header and
    counters for the five tracked points."""
    result = {"chek": None, "header": None, "counters": {}}
    if not text:
        return result

    m = re.search(r"Чек\s*#\s*(\d+)", text)
    if m:
        result["chek"] = int(m.group(1))

    # Header line like "Santiago 29.05 14:30"
    hm = re.search(r"^([A-Za-zА-Яа-я ]+\s+\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2})",
                    text, re.MULTILINE)
    if hm:
        result["header"] = hm.group(1).strip()

    for key, (_label, prefix, _sub) in POINTS.items():
        for line in text.splitlines():
            line = line.strip()
            if re.match(prefix, line):
                cnt = _count_before_ts(line)
                if cnt is not None:
                    result["counters"][key] = cnt
                break
    return result

# Header line of a category block, e.g.
#   "🔥 Критическая ошибка более 12 часов: 2 тс."
#   "✅ Нет связи более 6 часов: 0 тс."
_HEADER_RE  = re.compile(r"^\W*(\w.*?):\s*(\d+)\s*тс", re.UNICODE)
_SCOOTER_HOURS_RE = re.compile(r"(S\.\d+)\s*(?:\((\d+)ч\.?\))?")

def parse_scooters(text):
    """Parse the scooter-list message into {category_name: [(code, hours), ...]}.
    hours is a string like '15' or '' if not present."""
    blocks = {}
    current = None
    if not text:
        return blocks
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hm = _HEADER_RE.match(line)
        if hm:
            current = hm.group(1).strip()
            blocks[current] = []
            continue
        if current is not None:
            for m in _SCOOTER_HOURS_RE.finditer(line):
                code = m.group(1)
                hours = m.group(2) or ""
                blocks[current].append((code, hours))
    return blocks

def scooters_for_points(scooter_blocks):
    """Map parsed category blocks to the five tracked points by substring."""
    out = {k: [] for k in POINTS}
    for cat_name, entries in scooter_blocks.items():
        low = cat_name.lower()
        for key, (_label, _prefix, sub) in POINTS.items():
            if sub in low:
                out[key] = entries
                break
    return out

def _code(entry):
    """Extract scooter code from entry (tuple or plain string)."""
    if isinstance(entry, (list, tuple)):
        return entry[0]
    return entry

def _hours(entry):
    """Extract hours string from entry (tuple or plain string)."""
    if isinstance(entry, (list, tuple)) and len(entry) > 1:
        return entry[1]
    return ""

def build_state(chek_text, scooter_text):
    """Combine both messages into a single comparable state object."""
    chek   = parse_chek(chek_text)
    blocks = parse_scooters(scooter_text)
    scoot  = scooters_for_points(blocks)
    return {
        "chek":     chek["chek"],
        "header":   chek["header"],
        "counters": chek["counters"],
        "scooters": scoot,
    }

# --------------------------------------------------------------------------- #
# Change detection + alert formatting
# --------------------------------------------------------------------------- #
def diff_states(old, new):
    """Return a dict describing what changed, or None if nothing relevant."""
    if old is None:
        old = {}
    changed = {
        "new_chek":          new.get("chek") != old.get("chek"),
        "counter_changes":   {},
        "new_scooters":      {},
        "is_first":          not old,
    }
    old_c = old.get("counters", {})
    for k in ORDER:
        if new["counters"].get(k) != old_c.get(k):
            changed["counter_changes"][k] = (old_c.get(k), new["counters"].get(k))

    old_s = old.get("scooters", {})
    for k in ORDER:
        new_codes = [_code(e) for e in new["scooters"].get(k, [])]
        old_codes = [_code(e) for e in old_s.get(k, [])]
        old_set   = set(old_codes)
        added = [c for c in new_codes if c not in old_set]
        if added:
            changed["new_scooters"][k] = added

    relevant = (changed["new_chek"] or changed["counter_changes"]
                or changed["new_scooters"])
    return changed if relevant else None

def format_alert(new, changed):
    """Build the alert text (HTML parse mode)."""
    lines = []
    if changed.get("is_first"):
        head = "\U0001f514 <b>Monitor iniciado</b>"
    elif changed["new_chek"]:
        head = "\U0001f514 <b>Nuevo chequeo #{}</b>".format(new.get("chek"))
    else:
        head = "⚠️ <b>Cambio de estado (chequeo #{})</b>".format(new.get("chek"))
    if new.get("header"):
        head += " — {}".format(html.escape(new["header"]))
    lines.append(head)
    if changed["new_chek"] or changed.get("is_first"):
        lines.append("Verifique el estado en la aplicación técnica.")
    lines.append("")

    # ── Counters (with emoji, no ordinal numbers) ──
    lines.append("<b>Contadores:</b>")
    for k in ORDER:
        label = POINTS[k][0]
        emoji = EMOJI.get(k, "•")
        cnt   = new["counters"].get(k)
        cnt_s = "—" if cnt is None else str(cnt)
        mark  = ""
        if k in changed["counter_changes"]:
            old_v, new_v = changed["counter_changes"][k]
            if old_v is not None and not changed.get("is_first"):
                mark = "  (antes {})".format(old_v)
        lines.append("{} {}: <b>{}</b>{}".format(emoji, label, cnt_s, mark))

    # ── Scooter list (one per line, with hours, truncated at 15) ──
    scoot_lines = []
    for k in ORDER:
        entries = new["scooters"].get(k, [])
        if not entries:
            continue
        added_codes = set(changed["new_scooters"].get(k, []))
        emoji = EMOJI.get(k, "•")
        label = POINTS[k][0]
        total = len(entries)

        scoot_lines.append("")
        scoot_lines.append("{} {} ({}):".format(emoji, label, total))

        display = entries[:MAX_SCOOTERS_PER_CAT]
        for entry in display:
            code  = _code(entry)
            hours = _hours(entry)
            is_new = code in added_codes
            prefix = "\U0001f195" if is_new else emoji   # 🆕 or category emoji
            h_str  = " ({}h)".format(hours) if hours else ""
            scoot_lines.append("  {} {}{}".format(prefix, code, h_str))

        if total > MAX_SCOOTERS_PER_CAT:
            scoot_lines.append("  <i>...y {} más</i>".format(
                total - MAX_SCOOTERS_PER_CAT))

    if scoot_lines:
        lines.append("")
        lines.append("<b>Scooters:</b>")
        lines.extend(scoot_lines)

    return "\n".join(lines)

# --------------------------------------------------------------------------- #
# Telegram transport
# --------------------------------------------------------------------------- #
def _api(method, params=None):
    url  = API.format(token=BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req  = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def send_alert(text):
    return _api("sendMessage", {
        "chat_id":    ALERT_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })

def _norm(chat_id):
    """Normalise a chat id to its numeric core for comparison."""
    s = str(chat_id)
    s = s.lstrip("-")
    if s.startswith("100"):
        s = s[3:]
    return s

def read_via_getchat():
    """Return text of the single currently-pinned message (last pinned)."""
    res    = _api("getChat", {"chat_id": SOURCE_CHAT_ID})
    pinned = res.get("result", {}).get("pinned_message", {})
    return pinned.get("text") or pinned.get("caption") or ""

def read_via_forward(msg_id):
    """Read a message by id by forwarding it to LOG_CHAT_ID, then delete it.
    Returns the message text.  Does NOT consume getUpdates (safe alongside the
    producer bot)."""
    if not msg_id:
        return ""
    res = _api("forwardMessage", {
        "chat_id":      LOG_CHAT_ID,
        "from_chat_id": SOURCE_CHAT_ID,
        "message_id":   msg_id,
        "disable_notification": "true",
    })
    msg    = res.get("result", {})
    text   = msg.get("text") or msg.get("caption") or ""
    fwd_id = msg.get("message_id")
    if fwd_id:
        try:
            _api("deleteMessage", {"chat_id": LOG_CHAT_ID, "message_id": fwd_id})
        except Exception:
            pass
    return text

def read_via_getupdates(state):
    """Consume getUpdates and keep the latest text of the Чек and scooter
    messages, classified by their content signature.  Requires the bot to
    receive group messages (privacy mode OFF or bot is admin)."""
    offset = state.get("update_offset", 0)
    params = {"timeout": 0, "allowed_updates": json.dumps(
        ["message", "edited_message", "channel_post", "edited_channel_post"])}
    if offset:
        params["offset"] = offset
    res = _api("getUpdates", params)
    chek_text  = state.get("chek_text", "")
    scoot_text = state.get("scooter_text", "")
    max_id     = offset - 1
    for upd in res.get("result", []):
        max_id = max(max_id, upd.get("update_id", max_id))
        msg = (upd.get("message") or upd.get("edited_message")
               or upd.get("channel_post") or upd.get("edited_channel_post") or {})
        chat = msg.get("chat", {})
        if _norm(chat.get("id", "")) != _norm(SOURCE_CHAT_ID):
            continue
        text = msg.get("text") or msg.get("caption") or ""
        if not text:
            continue
        if re.search(r"Чек\s*#\s*\d+", text):
            chek_text  = text
        elif _SCOOTER_HOURS_RE.search(text) and _HEADER_RE.search(text):
            scoot_text = text
    state["update_offset"]  = max_id + 1
    state["chek_text"]      = chek_text
    state["scooter_text"]   = scoot_text
    return chek_text, scoot_text

# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# --------------------------------------------------------------------------- #
# Main cycle
# --------------------------------------------------------------------------- #
def fetch_texts(state):
    if READ_MODE == "forward":
        return (read_via_forward(CHEK_MSG_ID),
                read_via_forward(SCOOTER_MSG_ID))
    if READ_MODE == "getchat":
        t = read_via_getchat()
        # getChat returns only the last pinned message; classify it.
        if re.search(r"Чек\s*#\s*\d+", t):
            return t, state.get("scooter_text", "")
        return state.get("chek_text", ""), t
    # default: getupdates
    return read_via_getupdates(state)

def run_once():
    state = load_state()
    chek_text, scoot_text = fetch_texts(state)
    new     = build_state(chek_text, scoot_text)
    prev    = state.get("snapshot")
    changed = diff_states(prev, new)
    if changed:
        text = format_alert(new, changed)
        try:
            resp = send_alert(text)
            ok   = resp.get("ok")
        except Exception as e:
            ok = False
            print("send error:", e)
        print("ALERT sent:", ok)
    else:
        print("no change")
    state["snapshot"] = new
    save_state(state)
    return changed is not None

def run_loop():
    while True:
        try:
            run_once()
        except Exception as e:
            print("cycle error:", e)
        time.sleep(LOOP_SECONDS)

# --------------------------------------------------------------------------- #
# Self test (no network) — uses the two real sample messages
# --------------------------------------------------------------------------- #
SAMPLE_CHEK = """Чек #76
Santiago 29.05 14:30
Состояние парка:
Менее 27% - 362 ТС (15% от дост. парка).
Средний заряд: 56%.

1. 2729 всего тс.
1.1. 91% 2472 в доступе.
2. НЗ 74 тс.
2.1 НЗ >12ч. 13 тс.
3. Перемещение 90 тс.
4. Требует ремонта 11 тс.
5. Критическая ошибка 3 тс.
6. Авторем. 6 тс.
7. Готов к экспл. 0 тс.
8. Нет связи 0 тс.
9. ЗЗ 0 тс.
10. Ож.рем. СЦ 2 тс.
11. Тревога 5 тс.
12. В работе СБ 12 тс.
"""

SAMPLE_SCOOTERS = """f525 В доступе с сервисным режимом: 4 тс.
S.269862
S.270727

f525 55 ошибка: 48 тс.
S.269548
S.269861

f525 Низкий заряд батареи более 12 часов: 13 тс.
S.271317 (15ч.)
S.271729 (14ч.)
S.321137 (716ч.)

✅ Нет связи более 6 часов: 0 тс.

✅ Тревога более 6 часов: 0 тс.

✅ За зоной завершения более 6 часов: 0 тс.

f525 Ожидание ремонта СЦ более 6 часов: 2 тс.
S.306978
S.320746

f525 Критическая ошибка более 12 часов: 2 тс.
S.269612
S.275132
"""

def selftest():
    print("=== parse_chek ===")
    chek = parse_chek(SAMPLE_CHEK)
    print(json.dumps(chek, ensure_ascii=False, indent=2))

    print("\n=== parse_scooters (raw blocks) ===")
    blocks = parse_scooters(SAMPLE_SCOOTERS)
    for k, v in blocks.items():
        print(f"  {k}: {len(v)} -> {v}")

    print("\n=== state ===")
    state = build_state(SAMPLE_CHEK, SAMPLE_SCOOTERS)
    print(json.dumps(state, ensure_ascii=False, indent=2))

    print("\n=== alert (first run) ===")
    changed = diff_states(None, state)
    print(format_alert(state, changed))

    print("\n=== alert (simulated change: new chek, +1 critical scooter) ===")
    new2 = json.loads(json.dumps(state))
    new2["chek"] = 77
    new2["header"] = "Santiago 29.05 15:30"
    new2["counters"]["5"] = 4
    new2["scooters"]["5"] = [("S.269612", "26"), ("S.275132", "14"), ("S.999999", "")]
    changed2 = diff_states(state, new2)
    print(format_alert(new2, changed2))

    # sanity assertions
    assert chek["chek"] == 76
    assert chek["counters"] == {"5": 3, "8": 0, "9": 0, "11": 5, "2.1": 13}, chek["counters"]
    assert [_code(e) for e in state["scooters"]["5"]] == ["S.269612", "S.275132"]
    assert [_code(e) for e in state["scooters"]["2.1"]] == ["S.271317", "S.271729", "S.321137"]
    assert [_hours(e) for e in state["scooters"]["2.1"]] == ["15", "14", "716"]
    assert state["scooters"]["8"] == []
    print("\nALL ASSERTIONS PASSED ✅")

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "--once"
    if arg == "--selftest":
        selftest()
    elif arg == "--loop":
        run_loop()
    else:
        run_once()

if __name__ == "__main__":
    main()
