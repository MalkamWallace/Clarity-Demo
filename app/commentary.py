"""
Slide-ready commentary for the top movers.

Two modes:
  template  Always available. Deterministic, terse, slide-style bullets.
  ai        Used when ANTHROPIC_API_KEY is set. Claude rewrites the bullets with
            light driver context. It only sees numbers the engine already computed
            and is told not to invent or recompute anything. If the call fails,
            the template bullets are returned.
"""
from __future__ import annotations

import json
import os

SHORT = {"Software & Services": "S&S", "Infrastructure": "Infra", "Devices": "Devices", "Total": "Total"}
BASIS_LABEL = {"yoy": "YoY", "qoq": "QoQ", "plan": "vs plan"}


def money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.1f}M"


def pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def template_bullet(m: dict, basis: str) -> str:
    bu = SHORT.get(m["bu"], m["bu"])
    head = f"{bu} {m['line']} {pct(m['pct'])} {BASIS_LABEL[basis]} to ${m['value']:,.1f}M ({money(m['change'])})"
    tail = ""
    if basis != "plan" and m.get("vs_plan") is not None and abs(m["vs_plan_pct"]) >= 0.05:
        word = "over" if m["vs_plan"] > 0 else "under"
        tail = f"; ${abs(m['vs_plan']):,.1f}M {word} plan"
    if m.get("anomaly"):
        tail += "; outside 5-yr range"
    return head + tail


def build(movers: list[dict], basis: str, context: dict) -> dict:
    bullets = [template_bullet(m, basis) for m in movers]
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not movers:
        return {"mode": "template", "bullets": bullets}
    try:
        return {"mode": "ai", "bullets": _ai_bullets(movers, basis, context, bullets, key)}
    except Exception as e:  # noqa: BLE001  any API problem falls back to templates
        return {"mode": "template", "bullets": bullets, "note": f"AI commentary unavailable ({type(e).__name__})"}


PROMPT = """You write FP&A slide commentary. House style: one line per item, under 16 words,
lead with the business unit and line item, then the key figure, then the most likely driver if the
data shows one (for example a cost line growing far faster than revenue). No adjectives, no hedging.

Rules:
- Use only numbers that appear in the data below. Do not calculate new figures.
- Keep the order of items. Return exactly {n} lines.
- Output JSON: {{"bullets": ["...", "..."]}}

Comparison basis: {basis}
Movers (already ranked): {movers}
Related lines for context: {context}
Draft bullets to improve: {drafts}"""


def _ai_bullets(movers, basis, context, drafts, key) -> list[str]:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=os.environ.get("CLARITY_MODEL", "claude-sonnet-5-5"),
        max_tokens=1200,
        messages=[{"role": "user", "content": PROMPT.format(
            n=len(movers), basis=BASIS_LABEL[basis],
            movers=json.dumps(movers, default=float), context=json.dumps(context, default=float),
            drafts=json.dumps(drafts))}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    out = [str(b).strip() for b in data["bullets"]]
    if len(out) != len(movers):
        raise ValueError("wrong bullet count")
    return out
