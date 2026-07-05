#!/usr/bin/env python3
"""
Strait of Hormuz Status Monitor
Fetches news via Google News RSS, scores headlines for closure signals,
and writes data/status.json for the static site.

Stdlib only — no pip installs needed.
"""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------- config

FEEDS = [
    # Google News RSS search feeds (last 7 days, English)
    'https://news.google.com/rss/search?q=%22strait+of+hormuz%22+when:7d&hl=en-US&gl=US&ceid=US:en',
    'https://news.google.com/rss/search?q=%22strait+of+hormuz%22+(closed+OR+closure+OR+blocked)+when:7d&hl=en-US&gl=US&ceid=US:en',
    'https://news.google.com/rss/search?q=%22strait+of+hormuz%22+(tanker+OR+shipping)+when:7d&hl=en-US&gl=US&ceid=US:en',
]

# Signals that the strait is actually closed / blocked
CLOSURE_PATTERNS = [
    (r'\bstrait of hormuz (is |has been |now )?(closed|shut|blocked)\b', 10),
    (r'\b(closes?|closure of|shuts?|blocks?|blockade of) (the )?strait\b', 8),
    (r'\bshipping (halted|suspended|stopped)\b', 7),
    (r'\btraffic (halted|suspended|stopped)\b', 7),
    (r'\bmine(s|d)?\b.{0,40}\bstrait\b', 6),
    (r'\bstrait\b.{0,40}\bmine(s|d)?\b', 6),
]

# Signals of tension / disruption (not full closure)
TENSION_PATTERNS = [
    (r'\b(threatens?|threat|vows?) to (close|shut|block)\b', 4),
    (r'\b(seiz(es|ed|ure)|detain(s|ed)?|boards?|boarded)\b', 3),
    (r'\b(attack(s|ed)?|strikes?|struck|missile|drone)\b', 3),
    (r'\b(escort|reroute(s|d)?|divert(s|ed)?|avoid(s|ing)?)\b', 2),
    (r'\b(escalat|tension|conflict|warning)\w*\b', 1),
    (r'\binsurance (rates?|premiums?) (surge|spike|rise|jump)\w*\b', 2),
]

# Words that suggest the article is about reopening / de-escalation
CALM_PATTERNS = [
    (r'\b(reopen(s|ed|ing)?|resum(es|ed|ing))\b', -4),
    (r'\bde-?escalat\w+\b', -2),
    (r'\b(remains?|still) open\b', -5),
    (r'\bceasefire\b', -2),
]

USER_AGENT = 'Mozilla/5.0 (compatible; HormuzMonitor/1.0)'


# ---------------------------------------------------------------- fetch

def fetch_feed(url: str) -> list[dict]:
    """Fetch one RSS feed, return list of items."""
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        pub = (item.findtext('pubDate') or '').strip()
        source_el = item.find('source')
        source = source_el.text.strip() if source_el is not None and source_el.text else ''
        try:
            published = parsedate_to_datetime(pub)
        except Exception:
            published = None
        items.append({
            'title': title,
            'link': link,
            'source': source,
            'published': published.isoformat() if published else None,
            '_dt': published,
        })
    return items


def collect_articles() -> list[dict]:
    seen = set()
    articles = []
    for url in FEEDS:
        try:
            for item in fetch_feed(url):
                key = item['title'].lower()
                if key and key not in seen:
                    seen.add(key)
                    articles.append(item)
        except Exception as e:
            print(f'[warn] feed failed: {url} -> {e}')
    return articles


# ---------------------------------------------------------------- score

def recency_weight(dt) -> float:
    """Newer articles count more. 0-24h: 1.0, decays to 0.2 by day 7."""
    if dt is None:
        return 0.5
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if age_hours <= 24:
        return 1.0
    if age_hours >= 168:
        return 0.2
    return 1.0 - 0.8 * (age_hours - 24) / 144


THREAT_RE = re.compile(
    r'\b(threat(en(s|ed|ing)?)?|vow(s|ed)?|warn(s|ed|ing)?|could|may|might|'
    r'plan(s|ned)? to|if|would|votes? to|calls? (for|to))\b'
)


def score_article(title: str) -> tuple[float, list[str]]:
    t = title.lower()
    score = 0.0
    tags = []
    # Hypothetical/threat phrasing means closure language is NOT an actual closure
    is_hypothetical = bool(THREAT_RE.search(t))
    for pattern, weight in CLOSURE_PATTERNS:
        if re.search(pattern, t):
            if is_hypothetical:
                score += 2  # counts toward tension instead
                tags.append('tension')
            else:
                score += weight
                tags.append('closure')
    for pattern, weight in TENSION_PATTERNS:
        if re.search(pattern, t):
            score += weight
            tags.append('tension')
    for pattern, weight in CALM_PATTERNS:
        if re.search(pattern, t):
            score += weight
            tags.append('calm')
    return score, sorted(set(tags))


def evaluate(articles: list[dict]) -> dict:
    total = 0.0
    closure_hits = 0
    scored = []
    for art in articles:
        raw, tags = score_article(art['title'])
        weighted = raw * recency_weight(art.get('_dt'))
        total += weighted
        if 'closure' in tags and weighted >= 5:
            closure_hits += 1
        scored.append({**art, 'score': round(weighted, 2), 'tags': tags})

    # Status thresholds — the strait is open ~always, so default OPEN
    # and require strong, multiple recent closure signals to escalate.
    if closure_hits >= 3 and total >= 30:
        status, label = 'CLOSED', 'Closure reported'
    elif closure_hits >= 1 and total >= 15:
        status, label = 'DISRUPTED', 'Major disruption reported'
    elif total >= 8:
        status, label = 'ELEVATED', 'Elevated tension'
    else:
        status, label = 'OPEN', 'Normal traffic'

    # Top headlines: most relevant first, then most recent
    scored.sort(key=lambda a: (a['score'], a['published'] or ''), reverse=True)
    top = [
        {k: a[k] for k in ('title', 'link', 'source', 'published', 'score', 'tags')}
        for a in scored[:12]
    ]

    return {
        'status': status,
        'label': label,
        'tension_index': round(total, 1),
        'closure_signals': closure_hits,
        'articles_scanned': len(articles),
        'updated': datetime.now(timezone.utc).isoformat(),
        'headlines': top,
    }


# ---------------------------------------------------------------- brent

def fetch_brent():
    """Brent crude futures from stooq.com (free, no key, delayed quotes).
    Returns {'price': float, 'change_pct': float} or None on failure."""
    url = 'https://stooq.com/q/l/?s=cb.f&f=sd2t2ohlcv&h&e=csv'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            lines = resp.read().decode().strip().splitlines()
        # header: Symbol,Date,Time,Open,High,Low,Close,Volume
        cols = lines[1].split(',')
        open_p, close_p = float(cols[3]), float(cols[6])
        change = (close_p - open_p) / open_p * 100 if open_p else 0.0
        return {'price': round(close_p, 2), 'change_pct': round(change, 2)}
    except Exception as e:
        print(f'[warn] brent fetch failed: {e}')
        return None


# ---------------------------------------------------------------- history

HISTORY_PATH = 'data/history.json'
HISTORY_DAYS = 31


def update_history(result: dict) -> list:
    try:
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    except Exception:
        history = []
    history.append({
        't': result['updated'],
        'i': result['tension_index'],
        's': result['status'],
    })
    # keep only the last HISTORY_DAYS days
    cutoff = datetime.now(timezone.utc).timestamp() - HISTORY_DAYS * 86400
    def ts(e):
        try:
            return datetime.fromisoformat(e['t']).timestamp()
        except Exception:
            return 0
    history = [e for e in history if ts(e) >= cutoff]
    with open(HISTORY_PATH, 'w') as f:
        json.dump(history, f)
    return history


# ---------------------------------------------------------------- main

def main():
    print('Fetching feeds...')
    articles = collect_articles()
    print(f'Collected {len(articles)} unique articles')
    result = evaluate(articles)
    result['brent'] = fetch_brent()
    print(f"Status: {result['status']} | tension index {result['tension_index']}")
    with open('data/status.json', 'w') as f:
        json.dump(result, f, indent=2)
    print('Wrote data/status.json')
    history = update_history(result)
    print(f'History: {len(history)} points')


if __name__ == '__main__':
    main()
