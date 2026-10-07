"""Local candidate retrieval: scores propose candidates, never assign an action."""
import json
import re
from app.services.normalization import normalize

STOP = set("de del la el los las en para por con que un una y ya tengo hay quiero como sobre mañana manana".split())


def tokens(value):
    return {t for t in re.findall(r"\w+", normalize(value)) if len(t) > 2 and t not in STOP}


def project_catalog(projects):
    return [{"id": str(p.id), "name": p.name, "slug": p.slug,
             "aliases": [a.alias for a in p.aliases],
             "area": p.area.name if getattr(p, "area", None) else None,
             "company": p.company.name if getattr(p, "company", None) else None}
            for p in projects]


def candidate_projects(projects, text, *, recent_ids=(), pinned_id=None, max_chars=20000):
    catalog = project_catalog(projects)
    words, normalized = tokens(text), " " + normalize(text) + " "
    recent_ids = {str(i) for i in recent_ids}
    ranked = []
    for p in catalog:
        names = [p["name"], p["slug"], *p["aliases"]]
        score = sum(10 for name in names if " " + normalize(name) + " " in normalized)
        score += 3 * len(words & tokens(" ".join(names)))
        score += len(words & tokens(" ".join(filter(None, [p["company"], p["area"]]))))
        if p["id"] in recent_ids:
            score += 1
        if p["id"] == str(pinned_id):
            score += 10000
        if score:
            ranked.append((score, p))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    selected, used, truncated = [], 2, False
    # Include complete score tiers: a budget must not hide an equally ranked alternative.
    for score in sorted({score for score, _ in ranked}, reverse=True):
        group = [p for s, p in ranked if s == score]
        size = len(json.dumps(group, ensure_ascii=False))
        if used + size > max_chars:
            truncated = True
            break
        selected.extend(group)
        used += size
    return selected, truncated
