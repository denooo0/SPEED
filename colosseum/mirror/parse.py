"""MIRROR — parsing signal-channel messages into structured trades.

Channel messages are wildly inconsistent: emoji, mixed casing, "sl" vs "stop
loss" vs "S/L", multiple TPs, ranged entries, "now" market orders, edits,
follow-ups ("TP1 hit, move SL to BE"). This parser handles the common dialects
deterministically and hands the residue to an optional LLM fallback.

DESIGN PRINCIPLE: a message that cannot be parsed with confidence is recorded as
UNPARSED, never guessed. The whole value of this project is measuring what a
channel ACTUALLY did — a hallucinated entry price silently corrupts that
measurement, which is worse than a gap in coverage you can see.

Also captured, because they matter enormously for honest evaluation:
  * follow-up messages that MOVE the stop (break-even moves flatter the record
    dramatically and most published stats quietly assume them)
  * cancellations and "close now" instructions
  * whether TPs are hit in sequence or the position is scaled
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class MsgKind(str, Enum):
    SIGNAL = "signal"
    UPDATE = "update"          # move SL, partial close
    RESULT = "result"          # "TP1 hit"
    CANCEL = "cancel"
    NOISE = "noise"            # promo, commentary
    UNPARSED = "unparsed"


@dataclass
class ParsedSignal:
    msg_id: str
    t_ns: int
    symbol: str
    direction: str                    # long | short
    entry: Optional[float]
    entry_high: Optional[float]       # ranged entries: "buy 2400-2404"
    stop: Optional[float]
    targets: List[float] = field(default_factory=list)
    kind: MsgKind = MsgKind.SIGNAL
    raw: str = ""
    confidence: float = 1.0
    is_market: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"msg_id": self.msg_id, "t_ns": self.t_ns, "symbol": self.symbol,
                "direction": self.direction, "entry": self.entry,
                "entry_high": self.entry_high, "stop": self.stop,
                "targets": list(self.targets), "kind": self.kind.value,
                "is_market": self.is_market, "confidence": self.confidence,
                "notes": self.notes}

    @property
    def risk(self) -> Optional[float]:
        if self.entry is None or self.stop is None:
            return None
        return abs(self.entry - self.stop)

    def r_of(self, price: float) -> Optional[float]:
        r = self.risk
        if not r:
            return None
        d = (price - self.entry) if self.direction == "long" else (self.entry - price)
        return d / r


@dataclass
class ParsedUpdate:
    msg_id: str
    t_ns: int
    action: str                  # sl_to_be | sl_move | partial | close | tp_hit | sl_hit
    value: Optional[float] = None
    refers_to: Optional[str] = None
    raw: str = ""


# Matches 2418.40, 61200, 1,234.56, 2418
#
# THE BUG THIS REPLACES: `\d{1,3}(?:[,\s]?\d{3})*` greedily took the first three
# digits of "2418.40" and then could not continue, yielding 241. Every gold
# price is four digits, so the parser was silently mangling essentially every
# signal it read. A price parser that is subtly wrong is far more dangerous than
# one that fails loudly -- it produces a complete, plausible, entirely fictional
# dataset, and every conclusion drawn from it is worthless.
NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,5})?|\d{2,7}(?:\.\d{1,5})?)"

SYMBOLS = {
    "xauusd": "XAUUSD", "xau": "XAUUSD", "gold": "XAUUSD", "gld": "XAUUSD",
    "btcusd": "BTCUSD", "btc": "BTCUSD", "bitcoin": "BTCUSD",
    "xagusd": "XAGUSD", "silver": "XAGUSD",
}

BUY_WORDS = r"\b(buy|long|bullish|buy\s*limit|buy\s*stop)\b"
SELL_WORDS = r"\b(sell|short|bearish|sell\s*limit|sell\s*stop)\b"


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").replace(" ", ""))
    except (ValueError, AttributeError):
        return None


def detect_symbol(text: str) -> Optional[str]:
    t = text.lower()
    for k, v in SYMBOLS.items():
        if re.search(rf"\b{re.escape(k)}\b", t):
            return v
    return None


def classify(text: str) -> MsgKind:
    t = text.lower()
    if re.search(r"\b(tp\s*\d?\s*(hit|reached|done|✅)|target\s*\d?\s*hit|"
                 r"sl\s*hit|stop\s*(loss\s*)?hit|closed?\s+in\s+(profit|loss))", t):
        return MsgKind.RESULT
    if re.search(r"\b(cancel|invalid|void|ignore\s+(this|the)\s+signal)\b", t):
        return MsgKind.CANCEL
    if re.search(r"\b(move\s+sl|sl\s+to\s+(be|breakeven|entry)|break\s*even|"
                 r"secure|partial|take\s+partial|close\s+half)\b", t):
        return MsgKind.UPDATE
    if re.search(BUY_WORDS, t) or re.search(SELL_WORDS, t):
        if re.search(r"\b(sl|s/l|stop)\b", t) or re.search(r"\btp\s*\d?\b", t):
            return MsgKind.SIGNAL
    return MsgKind.NOISE


def parse_signal(msg_id: str, t_ns: int, text: str,
                 default_symbol: str = "XAUUSD") -> Optional[ParsedSignal]:
    """Deterministic parse of the common signal dialects."""
    t = text.lower().replace("−", "-").replace("–", "-")
    kind = classify(text)
    if kind is not MsgKind.SIGNAL:
        return None

    sell = re.search(SELL_WORDS, t)
    buy = re.search(BUY_WORDS, t)
    if not (buy or sell):
        return None
    # If both appear, trust whichever comes first — later mentions are usually
    # commentary ("sell setup, do not buy here").
    direction = ("short" if sell and (not buy or sell.start() < buy.start())
                 else "long")

    sym = detect_symbol(text) or default_symbol
    notes: List[str] = []
    conf = 1.0

    # -- stop
    stop = None
    m = re.search(rf"\b(?:sl|s/?l|stop\s*loss|stop)\b\s*[:\-@=]?\s*{NUM}", t)
    if m:
        stop = _num(m.group(1))

    # -- targets (ordered by appearance, deduped)
    targets: List[float] = []
    # `(\d(?!\d))?` — the TP index must NOT be followed by another digit,
    # otherwise "tp 63000" parses the leading 6 as the index and the price as
    # 3000. Silent, and it corrupts every multi-digit target.
    for m in re.finditer(rf"\btp\s*(\d(?!\d))?\s*[:\-@=]?\s*{NUM}", t):
        v = _num(m.group(2))
        if v is not None and v not in targets:
            targets.append(v)
    if not targets:
        for m in re.finditer(rf"\btarget\s*(\d(?!\d))?\s*[:\-@=]?\s*{NUM}", t):
            v = _num(m.group(2))
            if v is not None and v not in targets:
                targets.append(v)

    # -- entry (ranged, explicit, or market)
    entry = entry_high = None
    is_market = bool(re.search(r"\b(now|market|cmp|current\s+price|instant)\b", t))
    m = re.search(rf"{NUM}\s*[-–/]\s*{NUM}", t)
    rng = None
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if a and b and abs(a - b) / max(a, 1) < 0.02:
            rng = (min(a, b), max(a, b))
    m = re.search(rf"\b(?:entry|enter|@|at)\b\s*[:\-@=]?\s*{NUM}", t)
    if m:
        entry = _num(m.group(1))
    elif rng:
        entry, entry_high = rng[0], rng[1]
        notes.append("ranged entry")
    else:
        # fall back to the first number that is neither the stop nor a target
        for m in re.finditer(NUM, t):
            v = _num(m.group(1))
            if v is None or v < 10:
                continue
            if v == stop or v in targets:
                continue
            entry = v
            conf = 0.75
            notes.append("entry inferred positionally")
            break

    if entry is None and not is_market:
        return ParsedSignal(msg_id, t_ns, sym, direction, None, None, stop,
                            targets, MsgKind.UNPARSED, text, 0.0,
                            notes=["no entry found"])

    # -- geometry sanity. A "signal" whose stop is on the profit side is either
    #    a parse failure or a broken signal; either way it must not silently
    #    enter the dataset as a real trade.
    if entry is not None and stop is not None:
        if direction == "long" and stop >= entry:
            conf = min(conf, 0.4)
            notes.append("stop on the wrong side for a long — parse suspect")
        if direction == "short" and stop <= entry:
            conf = min(conf, 0.4)
            notes.append("stop on the wrong side for a short — parse suspect")
    if entry is not None and targets:
        bad = [tp for tp in targets
               if (direction == "long" and tp <= entry)
               or (direction == "short" and tp >= entry)]
        if bad:
            targets = [tp for tp in targets if tp not in bad]
            notes.append(f"dropped {len(bad)} target(s) on the wrong side")

    return ParsedSignal(msg_id, t_ns, sym, direction, entry, entry_high, stop,
                        targets, MsgKind.SIGNAL, text, conf, is_market, notes)


def parse_update(msg_id: str, t_ns: int, text: str) -> Optional[ParsedUpdate]:
    """Follow-ups. These matter more than people expect.

    A channel that moves the stop to break-even after TP1 has a completely
    different risk profile from one that does not — and published win rates
    almost always assume the flattering version. Capturing updates is what makes
    the evaluation honest instead of generous.
    """
    t = text.lower()
    kind = classify(text)
    if kind is MsgKind.RESULT:
        if re.search(r"\bsl\b.*hit|stop.*hit|closed?\s+in\s+loss", t):
            return ParsedUpdate(msg_id, t_ns, "sl_hit", raw=text)
        m = re.search(r"\btp\s*(\d)?\b", t)
        return ParsedUpdate(msg_id, t_ns, "tp_hit",
                            float(m.group(1)) if m and m.group(1) else 1.0,
                            raw=text)
    if kind is MsgKind.CANCEL:
        return ParsedUpdate(msg_id, t_ns, "close", raw=text)
    if kind is MsgKind.UPDATE:
        if re.search(r"\b(be|breakeven|break\s*even|to\s+entry)\b", t):
            return ParsedUpdate(msg_id, t_ns, "sl_to_be", raw=text)
        m = re.search(rf"\bsl\b\s*(?:to|@|:)?\s*{NUM}", t)
        if m:
            return ParsedUpdate(msg_id, t_ns, "sl_move", _num(m.group(1)), raw=text)
        if re.search(r"\bpartial|close\s+half\b", t):
            return ParsedUpdate(msg_id, t_ns, "partial", raw=text)
    return None


LLM_PARSE_PROMPT = """\
Extract the trade signal from this message. Return ONLY JSON:
{{"is_signal": true|false, "symbol": "XAUUSD"|"BTCUSD"|other, "direction": "long"|"short",
 "entry": <float|null>, "entry_high": <float|null>, "stop": <float|null>,
 "targets": [<float>...], "is_market": true|false, "confidence": <0..1>}}

If it is not a trade signal, return {{"is_signal": false}}. Do NOT guess numbers
that are not present — return null. A wrong number is far worse than a missing
one, because this feeds a measurement of what the channel actually did.

MESSAGE:
{text}"""


class MessageParser:
    """Deterministic first, LLM only on the residue."""

    def __init__(self, default_symbol: str = "XAUUSD",
                 llm: Optional[Callable[[str], str]] = None,
                 min_confidence: float = 0.5):
        self.default_symbol = default_symbol
        self.llm = llm
        self.min_confidence = min_confidence
        self.stats: Dict[str, int] = {}

    def _note(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def parse(self, msg_id: str, t_ns: int, text: str
              ) -> Tuple[Optional[ParsedSignal], Optional[ParsedUpdate]]:
        if not text or not text.strip():
            self._note("empty")
            return None, None
        sig = parse_signal(msg_id, t_ns, text, self.default_symbol)
        if sig and sig.kind is MsgKind.SIGNAL and sig.confidence >= self.min_confidence:
            self._note("signal_regex")
            return sig, None
        upd = parse_update(msg_id, t_ns, text)
        if upd:
            self._note(f"update_{upd.action}")
            return None, upd
        if self.llm and classify(text) in (MsgKind.SIGNAL, MsgKind.UNPARSED):
            got = self._llm_parse(msg_id, t_ns, text)
            if got:
                self._note("signal_llm")
                return got, None
        self._note("noise_or_unparsed")
        return None, None

    def _llm_parse(self, msg_id: str, t_ns: int, text: str) -> Optional[ParsedSignal]:
        import json
        try:
            raw = self.llm(LLM_PARSE_PROMPT.format(text=text[:2000]))
            i, j = raw.find("{"), raw.rfind("}")
            if i < 0:
                return None
            d = json.loads(raw[i:j + 1])
            if not d.get("is_signal"):
                return None
            return ParsedSignal(
                msg_id, t_ns, str(d.get("symbol") or self.default_symbol),
                str(d["direction"]), d.get("entry"), d.get("entry_high"),
                d.get("stop"), [float(x) for x in (d.get("targets") or [])],
                MsgKind.SIGNAL, text, float(d.get("confidence", 0.6)),
                bool(d.get("is_market")), ["parsed by LLM fallback"])
        except Exception:
            return None
