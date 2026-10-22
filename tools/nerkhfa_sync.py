#!/usr/bin/env python3
"""Sync market.json from the @NerkhFA Telegram channel.

Runs inside GitHub Actions (see .github/workflows/nerkhfa-sync.yml):
reads the local market.json checkout, fetches the channel's public
preview (t.me/s/NerkhFA), and if the newest rates board there is newer
than market.json, patches the covered fields in place. The workflow
then commits. Fields the channel does not post (gbp, try, oil, ...)
stay as the hourly fetch workflow set them.
Exit 0 = patched or cleanly skipped; exit 2 = fetch/parse error.
"""
import html as ihtml
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

CHANNEL_URL = "https://t.me/s/NerkhFA"
MARKET_PATH = sys.argv[1] if len(sys.argv) > 1 else "market.json"
TEHRAN = ZoneInfo("Asia/Tehran")
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
JMONTHS = {"فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5,
           "شهریور": 6, "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10,
           "بهمن": 11, "اسفند": 12}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nerkhfa-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def post_text(chunk):
    m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', chunk, re.S)
    if not m:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", m.group(1))
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", ihtml.unescape(t)).strip()


def num_after(text, label):
    m = re.search(re.escape(label) + r"[^\d]{0,12}([\d][\d,]*)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_board(text):
    d = {}
    d["usd"] = num_after(text, "دلار آمریکا فروش")
    d["eur"] = num_after(text, "یورو فروش")
    d["cad"] = num_after(text, "دلار کانادا فروش")
    d["aed"] = num_after(text, "درهم امارات")
    d["coin_emami"] = num_after(text, "سکه امامی")
    d["coin_bahar"] = num_after(text, "سکه بهار آزادی")
    d["coin_half"] = num_after(text, "نیم سکه")
    d["coin_quarter"] = num_after(text, "ربع سکه")
    d["coin_gram"] = num_after(text, "سکه گرمی")
    d["gold_18k"] = num_after(text, "طلای 18 عیار هر گرم")
    d["gold_mesghal"] = num_after(text, "مثقال طلای آبشده")
    m = re.search(r"لحظه بروزرسانی:\s*\S+\s+(\d+)\s+(\S+)\s+(\d+)\s+ساعت\s+(\d+):(\d+)",
                  text.translate(FA_DIGITS))
    if m:
        jd, jm_name, jy, hh, mm = m.groups()
        if jm_name in JMONTHS:
            d["_jalali"] = (int(jy), JMONTHS[jm_name], int(jd), int(hh), int(mm))
    return {k: v for k, v in d.items() if v is not None}


def to_epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.strptime(v.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TEHRAN).timestamp()
        except Exception:
            try:
                return float(v)
            except Exception:
                return 0
    return 0


def main():
    try:
        page = fetch(CHANNEL_URL)
    except Exception as e:
        print("RESULT error fetch:", e)
        return 2
    chunks = re.split(r'data-post="NerkhFA/\d+"', page)[1:]
    boards = []
    ounce = None
    for ch in chunks:
        tm = re.search(r'datetime="([^"]+)"', ch)
        if not tm:
            continue
        try:
            epoch = datetime.fromisoformat(tm.group(1).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        text = post_text(ch)
        if "دلار آمریکا فروش" in text and "لحظه بروزرسانی" in text:
            b = parse_board(text)
            if b.get("usd") and b.get("gold_18k"):
                boards.append((epoch, b))
        if "اونس طلا" in text:
            v = num_after(text, "اونس طلا")
            if v:
                ounce = (epoch, v)
    if not boards:
        print("RESULT error no board post parsed")
        return 2
    boards.sort(key=lambda x: x[0])
    b_epoch, board = boards[-1]

    with open(MARKET_PATH, encoding="utf-8") as f:
        market = json.load(f)
    cur_updated = to_epoch(market.get("updated_at"))
    if b_epoch <= cur_updated + 60:
        print(f"RESULT skip channel board ({datetime.fromtimestamp(b_epoch, TEHRAN):%H:%M}) "
              f"not newer than market.json ({datetime.fromtimestamp(cur_updated, TEHRAN):%H:%M})")
        return 0

    limits = {"usd": 0.12, "eur": 0.12, "cad": 0.12, "aed": 0.12,
              "gold_18k": 0.12, "gold_mesghal": 0.12,
              "coin_emami": 0.20, "coin_bahar": 0.20, "coin_half": 0.20,
              "coin_quarter": 0.20, "coin_gram": 0.20}
    patched = {}
    for k, lim in limits.items():
        v = board.get(k)
        cur = market.get(k)
        if v and cur:
            if abs(v - float(cur)) / float(cur) <= lim:
                patched[k] = v
            else:
                print(f"WARN {k}={v} deviates >{lim:.0%} from current {cur}; kept old")
        elif v:
            patched[k] = v
    if ounce and ounce[0] >= b_epoch - 3600 and market.get("usd_xau"):
        if abs(ounce[1] - float(market["usd_xau"])) / float(market["usd_xau"]) <= 0.12:
            patched["usd_xau"] = ounce[1]
    if "usd" not in patched or "gold_18k" not in patched:
        print("RESULT error anchor fields failed sanity")
        return 2

    market.update(patched)
    bt = datetime.fromtimestamp(b_epoch, TEHRAN)
    market["updated_at"] = bt.strftime("%Y-%m-%d %H:%M:%S")
    market["time"] = bt.strftime("%H:%M")
    if "_jalali" in board:
        jy, jm, jd, _, _ = board["_jalali"]
        market["date_shamsi"] = f"{jy}/{jm:02d}/{jd:02d}"
    else:
        market["date_shamsi"] = bt.strftime("%Y/%m/%d")

    with open(MARKET_PATH, "w", encoding="utf-8") as f:
        json.dump(market, f, ensure_ascii=False, indent=1)
    print(f"RESULT patched usd={patched.get('usd')} eur={patched.get('eur')} "
          f"gold={patched.get('gold_18k')} emami={patched.get('coin_emami')} "
          f"time={market['time']} date={market['date_shamsi']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
