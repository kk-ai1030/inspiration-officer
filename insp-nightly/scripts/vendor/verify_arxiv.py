#!/usr/bin/env python3
"""Verify that arXiv references in cards exist and match the expected title.

Uses the public abs page (https://arxiv.org/abs/<id>) and compares <meta name="citation_title">
with the title recorded in triage.json. The export API is avoided because it rate-limits (429).

Usage:
    python3 verify_arxiv.py cards.json --triage triage.json [--out arxiv_check.json] [--sleep 3]
    python3 verify_arxiv.py --self-test

Exit codes: 0 all verified · 1 at least one mismatch / not found · 4 network failure for some refs
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path


# Unicode Greek -> the LaTeX/Latin name, so "$\\Lambda$-enhanced" (arXiv page meta) and "$Λ$-enhanced"
# (arXiv API) normalise to the same string. Found in T3: a correct card was dropped because of this.
_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ϵ": "epsilon", "ζ": "zeta",
    "η": "eta", "θ": "theta", "ϑ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "µ": "mu",
    "ν": "nu", "ξ": "xi", "ο": "omicron", "π": "pi", "ϖ": "pi", "ρ": "rho", "ϱ": "rho", "σ": "sigma",
    "ς": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi", "ϕ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
}
_TEX_WORDS = {"varepsilon": "epsilon", "vartheta": "theta", "varphi": "phi", "varrho": "rho", "varsigma": "sigma",
              "varpi": "pi", "textrm": "", "mathrm": "", "mathcal": "", "mathbf": "", "mathit": "", "text": "",
              "left": "", "right": "", "rm": ""}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", html.unescape(s or "")).lower()
    s = "".join(f" {_GREEK[ch]} " if ch in _GREEK else ch for ch in s)
    s = re.sub(r"\\([a-z]+)", lambda m: " " + _TEX_WORDS.get(m.group(1), m.group(1)) + " ", s)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))  # é -> e
    return " ".join(re.sub(r"[^\w]+", " ", s).replace("_", " ").split())


def title_match(a: str | None, b: str | None, fuzzy: float = 0.9) -> tuple[bool, float]:
    """Exact after norm(); otherwise accept if SequenceMatcher ratio >= fuzzy. Returns (ok, ratio)."""
    na, nb = norm(a or ""), norm(b or "")
    if not na or not nb:
        return False, 0.0
    if na == nb:
        return True, 1.0
    r = difflib.SequenceMatcher(None, na, nb).ratio()
    return r >= fuzzy, round(r, 3)


def norm_id(x: str) -> str:
    return re.sub(r"v\d+$", "", re.sub(r"^arxiv:", "", str(x).strip(), flags=re.I))


def extract_title(page: str) -> str | None:
    m = re.search(r'<meta\s+name="citation_title"\s+content="([^"]*)"', page, flags=re.I)
    return html.unescape(m.group(1)) if m else None


def fetch(ref: str, timeout: int = 30) -> tuple[str, str | None]:
    url = f"https://arxiv.org/abs/{ref}"
    req = urllib.request.Request(url, headers={"User-Agent": "insp-card-verify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok", extract_title(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return ("not_found" if exc.code == 404 else f"http_{exc.code}"), None
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"network:{exc}", None


def self_test() -> int:
    page = '<html><head><meta name="citation_title" content="Is Complex Agentic Memory Always Needed? Naive RAG Is Stronger Than We Thought" /></head></html>'
    t = extract_title(page)
    if norm(t or "") != norm("Is Complex Agentic Memory Always Needed? Naive RAG Is Stronger Than We Thought"):
        print("SELF-TEST FAIL: extract_title")
        return 1
    pairs_ok = [  # (arXiv page meta, arXiv API) — real T3 case first
        ("$\\Lambda$-enhanced gray-molasses loading of a nanophotonic trap", "$Λ$-enhanced gray-molasses loading of a nanophotonic trap"),
        ("Spin squeezing with $\\alpha$ particles", "Spin squeezing with α particles"),
        ("Cooling of $^{87}$Rb atoms", "Cooling of $^{87}$Rb atoms"),
        ("Na\u00efve RAG", "Naive RAG"),
        ("A very long title about tweezer arrays of neutral atoms, take one", "A very long title about tweezer arrays of neutral atoms: take one!"),
    ]
    for x, y in pairs_ok:
        if not title_match(x, y)[0]:
            print(f"SELF-TEST FAIL: should match: {x!r} vs {y!r} -> {norm(x)!r} / {norm(y)!r}")
            return 1
    if title_match("Naive RAG is stronger", "Complex memory is always needed")[0]:
        print("SELF-TEST FAIL: different titles must not match")
        return 1
    if extract_title("<html></html>") is not None:
        print("SELF-TEST FAIL: missing meta should give None")
        return 1
    print("SELF-TEST OK: verify_arxiv")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="核验卡片中的 arXiv 引用")
    ap.add_argument("cards", nargs="?", type=Path)
    ap.add_argument("--triage", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--sleep", type=float, default=3.0)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.cards and a.triage):
        ap.error("需要 cards 与 --triage")
    doc = json.loads(a.cards.read_text(encoding="utf-8"))
    tri = json.loads(a.triage.read_text(encoding="utf-8"))
    titles = {norm_id(c["id"]): c.get("title", "") for c in tri.get("shortlisted", [])}
    report, worst = {}, 0
    for card in doc.get("cards", []):
        src = card.get("source", {})
        if src.get("kind") != "arxiv":
            continue
        ref = norm_id(src.get("ref", ""))
        status, page_title = fetch(ref)
        ok = status == "ok" and title_match(page_title, titles.get(ref, ""))[0]
        report[card.get("id")] = {"ref": ref, "status": status, "page_title": page_title,
                                  "expected_title": titles.get(ref), "verified": ok}
        if status.startswith("network") or status.startswith("http_"):
            worst = max(worst, 4)
        elif not ok:
            worst = max(worst, 1)
        print(f"{'OK  ' if ok else 'FAIL'} {card.get('id')} {ref} [{status}]")
        time.sleep(a.sleep)
    if a.out:
        a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
