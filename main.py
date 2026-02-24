#!/usr/bin/env python3
"""
NAR Funding Radar v1.0 - Senior-level grant scanner
Network Aging Research, Heidelberg University
"""

import json
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_date


BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
STATE_FILE = BASE_DIR / "state.json"
OUTPUT_DIR.mkdir(exist_ok=True)


class NAROpportunity:
    def __init__(self, url: str, source_name: str, **data):
        self.url = url
        self.source_name = source_name
        self.program_name = data.get("program_name", "Unnamed")
        self.category = data.get("category", "UNKNOWN")
        self.deadline = data.get("deadline")
        self.deadline_type = data.get("deadline_type", "unknown")
        self.is_evergreen = data.get("is_evergreen", False)
        self.thematic_fit = data.get("thematic_fit", "none")
        self.thematic_tags = data.get("thematic_tags", [])
        self.score = data.get("score", 0.0)
        self.content_hash = data.get("content_hash", "")

    def to_dict(self):
        return {
            "url": self.url,
            "program_name": self.program_name,
            "source": self.source_name,
            "category": self.category,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "deadline_type": self.deadline_type,
            "is_evergreen": self.is_evergreen,
            "thematic_fit": self.thematic_fit,
            "thematic_tags": self.thematic_tags,
            "score": self.score
        }


def load_yaml(filename: str) -> dict:
    with open(filename, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"items": {}, "last_run": None}


def fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NAR-Funding-Radar/1.0; +https://nar.uni-heidelberg.de)"
    }
    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        time.sleep(2)  # rate limit
        return resp.text
    except Exception as e:
        print(f"❌ Failed {url}: {e}")
        return None


def extract_candidate_links(html: str, base_url: str, cfg: dict) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    
    url_markers = [m.lower() for m in cfg["call_markers"]["url"]]
    anchor_markers = [m.lower() for m in cfg["call_markers"]["anchor"]]
    block_paths = cfg["blocklist"]
    
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        parsed_path = urlparse(href).path.lower()
        
        # блоклист
        if any(path in parsed_path for path in block_paths):
            continue
            
        text = (a.get_text() or "").lower().strip()
        href_lower = href.lower()
        
        # call markers
        if (any(marker in href_lower for marker in url_markers) or 
            any(marker in text for marker in anchor_markers)):
            links.add(href.split("#")[0])
    
    return list(links)[:cfg["max_pages_per_source"]]


def parse_date_candidates(text: str, cfg: dict) -> List[Dict]:
    dates = []
    text_lower = text.lower()
    
    # паттерны дат
    patterns = [
        r'(\d{1,2})\.?\s*(\d{1,2})\.?\s*(\d{4})',  # 15.04.2026
        r'(\d{4})-(\d{1,2})-(\d{1,2})',             # 2026-04-15
        r'(\d{1,2})/(\d{1,2})/(\d{4})',             # 04/15/2026
    ]
    
    context_score = {
        "deadline": 4, "frist": 4, "einreichung": 3, "bewerbung": 3,
        "submission": 4, "apply": 3, "stichtag": 3
    }
    
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                if pattern.startswith('('):  # dd.mm.yyyy
                    d, m, y = map(int, match.groups())
                else:
                    y, m, d = map(int, match.groups())
                
                if cfg["min_year"] <= y <= datetime.now().year + 2 and 1 <= m <= 12:
                    snippet = text_lower[max(0, match.start()-100):match.end()+100]
                    score = sum(context_score.get(word, 0) for word in context_score if word in snippet)
                    dates.append({"date": date(y, m, d), "score": score})
            except ValueError:
                continue
    
    return sorted(dates, key=lambda x: x["score"], reverse=True)


def detect_rolling(text_lower: str) -> bool:
    rolling_indicators = [
        "rolling", "open continuously", "any time", "laufend",
        "jederzeit", "keine frist", "ongoing"
    ]
    return any(indicator in text_lower for indicator in rolling_indicators)


def score_opportunity(opp: NAROpportunity, cfg: dict) -> float:
    score = 0.0
    
    # базовый скоринг
    if opp.deadline_type == "fixed":
        score += 3.0
    if opp.thematic_fit == "core":
        score += 3.0
    elif opp.thematic_fit == "adjacent":
        score += 1.5
    
    # urgency
    if opp.deadline and opp.deadline < date.today() + timedelta(days=42):
        score += 2.0
    
    opp.score = score
    return score


def scan_source(source: dict, cfg: dict) -> List[NAROpportunity]:
    print(f"🔍 {source['name']}")
    opportunities = []
    
    html = fetch_page(source["url"])
    if not html:
        return []
    
    # собираем candidate links
    links = extract_candidate_links(html, source["url"], cfg)
    print(f"  📄 {len(links)} candidates")
    
    for link in links:
        page_html = fetch_page(link)
        if not page_html:
            continue
        
        soup = BeautifulSoup(page_html, "html.parser")
        
        # название
        title = (soup.title or soup.find("h1")).get_text(strip=True) if soup.title else link
        title = re.sub(r'^\s*[•\-\|]\s*', '', title).strip()
        
        # текст для анализа
        full_text = soup.get_text()
        text_lower = full_text.lower()
        
        # дедлайн
        date_cands = parse_date_candidates(full_text, cfg)
        if date_cands:
            best_date = date_cands[0]["date"]
            deadline_type = "fixed"
        elif detect_rolling(text_lower):
            best_date = None
            deadline_type = "rolling"
        else:
            best_date = None
            deadline_type = "unknown"
        
        is_evergreen = deadline_type != "fixed"
        
        # тематика
        core_hits = [kw for kw in cfg["core_keywords"] if kw.lower() in text_lower]
        adj_hits = [kw for kw in cfg["adjacent_keywords"] if kw.lower() in text_lower]
        
        if core_hits:
            fit_type = "core"
            tags = core_hits[:3]
        elif len(adj_hits) >= 2:
            fit_type = "adjacent"
            tags = adj_hits[:3]
        else:
            continue  # нерелевантно
        
        opp = NAROpportunity(
            url=link,
            source_name=source["name"],
            program_name=title[:100],
            category=source["category"],
            deadline=best_date,
            deadline_type=deadline_type,
            is_evergreen=is_evergreen,
            thematic_fit=fit_type,
            thematic_tags=tags
        )
        
        score_opportunity(opp, cfg)
        if (opp.deadline_type == "fixed" and opp.score >= cfg["scoring"]["min_score_fixed"]) or \
           (opp.is_evergreen and opp.score >= cfg["scoring"]["min_score_evergreen"]):
            opportunities.append(opp)
    
    print(f"  ✅ {len(opportunities)} selected")
    return opportunities


def generate_digest(opportunities: List[NAROpportunity]) -> str:
    urgent = [o for o in opportunities if o.deadline and o.deadline < date.today() + timedelta(days=42)]
    evergreen = [o for o in opportunities if o.is_evergreen]
    
    urgent = sorted(urgent, key=lambda o: (o.deadline or date.max, -o.score))
    evergreen = sorted(evergreen, key=lambda o: -o.score)
    
    lines = []
    lines.append("# NAR Heidelberg – Funding Radar")
    lines.append(f"_Run: {datetime.now().strftime('%Y-%m-%d %H:%M CET')}_")
    lines.append("")
    
    # URGENT
    lines.append("## 🚨 URGENT (< 6 weeks)")
    if urgent:
        for i, opp in enumerate(urgent[:7], 1):
            lines.extend([
                f"**{i}. {opp.program_name}**",
                f"*Funder:* {opp.source_name}",
                f"*Deadline:* {opp.deadline.strftime('%d.%m.%Y') if opp.deadline else 'Rolling'}",
                f"*Fit:* {opp.thematic_fit.upper()} ({', '.join(opp.thematic_tags)})",
                f"[→ {opp.url}]({opp.url})",
                ""
            ])
    else:
        lines.append("_No urgent deadlines._")
    
    # Evergreen
    lines.append("## ♾️ Evergreen / Rolling")
    if evergreen:
        for i, opp in enumerate(evergreen[:8], 1):
            lines.extend([
                f"**{i}. {opp.program_name}**",
                f"*Funder:* {opp.source_name}",
                f"*Fit:* {opp.thematic_fit.upper()} ({', '.join(opp.thematic_tags)})",
                f"[→ {opp.url}]({opp.url})",
                ""
            ])
    else:
        lines.append("_No evergreen opportunities._")
    
    # Stats
    lines.extend([
        "---",
        f"**Stats:** {len(opportunities)} total | {len(urgent)} urgent | {len(evergreen)} evergreen",
        f"**Sources scanned:** {len(set(o.source_name for o in opportunities))}"
    ])
    
    return "\n".join(lines)


def main():
    print("🚀 NAR Funding Radar starting...")
    
    cfg = load_yaml("config.yml")
    sources = load_yaml("sources.yml")["sources"]
    
    all_opps = []
    state = load_state()
    
    for source in sources:
        opps = scan_source(source, cfg)
        all_opps.extend(opps)
        time.sleep(1)  # source delay
    
    # генерируем дайджест
    digest = generate_digest(all_opps)
    
    # сохраняем
    (OUTPUT_DIR / "latest.md").write_text(digest, encoding="utf-8")
    (OUTPUT_DIR / "latest.json").write_text(json.dumps([o.to_dict() for o in all_opps], indent=2), encoding="utf-8")
    
    print(f"✅ Done! {len(all_opps)} opportunities → output/latest.md")
    print("📊 Preview:")
    print(digest[:500] + "...")
    
    state["last_run"] = datetime.now().isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
