"""Telegram delivery: journal-first, send-second.

The ordering is the whole design. A signal is written to the immutable ledger
and journal BEFORE any attempt to deliver it. A Telegram outage, rate limit, or
network partition can therefore never cost the engine a record -- it can only
delay a notification. The retry queue drains later; the memory was never at risk.

Delivery is also decoupled from reasoning: the engine never blocks on the
messenger. If sending is slow, signals queue; they do not stall the tape.

Your Taken/Skipped taps come back as an extra feedback channel: did the human
agree, and were they right to? Over time that is a genuinely useful second
opinion the learner can compare itself against.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

from ..core.clock import ns_to_dt

API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class DeliveryStats:
    sent: int = 0
    failed: int = 0
    queued: int = 0
    retries: int = 0
    rate_limited: int = 0
    last_error: str = ""


def _fmt_price(p: float) -> str:
    return f"{p:,.2f}"


def signal_card(rec: Dict[str, object]) -> str:
    """The five-second read: what, where, why, and what kills it."""
    d = str(rec.get("direction", "")).upper()
    arrow = "🟢 LONG" if d == "LONG" else "🔴 SHORT"
    conv = float(rec.get("conviction", 0))
    bar = "█" * int(conv * 10) + "░" * (10 - int(conv * 10))
    entry = float(rec.get("entry", 0))
    stop = float(rec.get("stop", 0))
    risk = abs(entry - stop)
    tgts = rec.get("targets", []) or []
    tlines = []
    for t in tgts:
        px = float(t.get("price", 0))
        r = abs(px - entry) / risk if risk else 0
        tlines.append(f"  {t.get('label','TP')}  <code>{_fmt_price(px)}</code>  "
                      f"({r:.1f}R) — {t.get('reason','')}")
    t_str = ns_to_dt(int(rec.get("t_ns", 0))).strftime("%H:%M:%S UTC")
    conf = rec.get("confluence_with") or []
    conf_line = (f"\n🤝 <b>Confluence:</b> {', '.join(map(str, conf))}"
                 if conf else "")
    explore = ("\n🧪 <i>Exploration trade — deliberately perturbed parameters, "
               "excluded from headline stats</i>" if rec.get("is_exploration") else "")

    return f"""<b>{arrow}  XAU/USD</b>
<code>{bar}</code> {conv:.0%} · seat {rec.get('seat_id','?')} ({rec.get('lens','')})
<i>{t_str} · {rec.get('liquidity_session','')}</i>

📍 <b>Entry</b>  <code>{_fmt_price(entry)}</code>
🛑 <b>Stop</b>   <code>{_fmt_price(stop)}</code>  ({risk:.2f} risk)
   <i>{rec.get('stop_reason','')}</i>

🎯 <b>Targets</b>
{chr(10).join(tlines)}

💭 <b>Thesis</b>
{rec.get('thesis','')}

⚙️ <b>Mechanism</b>
{rec.get('mechanism','')}

🗺 <b>Expected path</b>
{rec.get('predicted_path','')}

❌ <b>Invalidation</b> (fires before the stop)
{rec.get('invalidation','')}

⚖️ <b>Counter-case</b>
{rec.get('counter_case','')}

net edge after cost: <code>{rec.get('edge_after_cost','n/a')}</code> · horizon ~{rec.get('horizon_min','?')}m{conf_line}{explore}
<code>{rec.get('signal_id','')}</code>"""


def outcome_card(sid: str, o: Dict[str, object]) -> str:
    res = str(o.get("resolution", ""))
    icon = {"tp1": "✅", "tp2": "✅✅", "stop": "❌",
            "timeout": "⏱", "never_filled": "⚪"}.get(res, "•")
    verdict = str(o.get("mechanism_verdict", ""))
    vicon = {"confirmed": "✅", "partial": "🟡", "false": "🔴"}.get(verdict, "•")
    return f"""{icon} <b>RESOLVED</b> · <code>{sid}</code>

Result: <b>{res}</b> · Net R: <b>{o.get('realized_r')}</b>
MFE {o.get('mfe_r')}R · MAE {o.get('mae_r')}R · {int(o.get('time_to_outcome_s',0))//60}m

Path taken: <code>{' → '.join(map(str, o.get('path_realized', [])))}</code>
Path match: <b>{o.get('path_match')}</b>

{vicon} <b>Mechanism was {verdict}</b>
<i>Graded separately from profit — a win on a false mechanism is punished, not
celebrated. Being right for the wrong reason teaches the engine nothing.</i>

Lessons: {', '.join(map(str, o.get('lesson_tags', []))) or '—'}"""


class TelegramPublisher:
    def __init__(self, token: str, chat_id: str, *,
                 enabled: bool = True, max_queue: int = 500,
                 transport: Optional[Callable[[str, dict], dict]] = None,
                 min_interval_s: float = 1.0,
                 now: Optional[Callable[[], float]] = None):
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)
        self.stats = DeliveryStats()
        self.queue: Deque[dict] = deque(maxlen=max_queue)
        self._transport = transport or self._http
        self.min_interval_s = min_interval_s
        self._now = now or time.monotonic
        self._last_send = 0.0
        self.human_tags: Dict[str, str] = {}   # signal_id -> taken|skipped

    # ---- transport -------------------------------------------------------

    def _http(self, method: str, payload: dict) -> dict:
        url = API.format(token=self.token, method=method)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def _send(self, payload: dict) -> bool:
        if not self.enabled:
            return False
        gap = self._now() - self._last_send
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)   # respect Telegram limits
        try:
            r = self._transport("sendMessage", payload)
            self._last_send = self._now()
            if r.get("ok"):
                self.stats.sent += 1
                return True
            desc = str(r.get("description", ""))
            if "Too Many Requests" in desc or r.get("error_code") == 429:
                self.stats.rate_limited += 1
            self.stats.failed += 1
            self.stats.last_error = desc
            return False
        except Exception as e:
            self.stats.failed += 1
            self.stats.last_error = f"{type(e).__name__}: {e}"
            return False

    # ---- public API ------------------------------------------------------

    def publish_signal(self, rec: Dict[str, object]) -> bool:
        sid = str(rec.get("signal_id", ""))
        payload = {
            "chat_id": self.chat_id, "text": signal_card(rec),
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Taken", "callback_data": f"taken:{sid}"},
                {"text": "⏭ Skipped", "callback_data": f"skipped:{sid}"},
            ]]},
        }
        return self._enqueue_or_send(payload)

    def publish_outcome(self, sid: str, outcome: Dict[str, object]) -> bool:
        return self._enqueue_or_send({
            "chat_id": self.chat_id, "text": outcome_card(sid, outcome),
            "parse_mode": "HTML", "disable_web_page_preview": True})

    def publish_text(self, text: str) -> bool:
        return self._enqueue_or_send({
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True})

    def _enqueue_or_send(self, payload: dict) -> bool:
        if self._send(payload):
            return True
        self.queue.append(payload)      # never lost, only delayed
        self.stats.queued = len(self.queue)
        return False

    def drain(self, max_items: int = 20) -> int:
        """Called on the engine's housekeeping tick. Bounded so draining a
        backlog can never starve the tape."""
        sent = 0
        for _ in range(min(max_items, len(self.queue))):
            payload = self.queue.popleft()
            if self._send(payload):
                sent += 1
                self.stats.retries += 1
            else:
                self.queue.appendleft(payload)   # still down; stop trying
                break
        self.stats.queued = len(self.queue)
        return sent

    # ---- inbound feedback ------------------------------------------------

    def poll_callbacks(self, offset: int = 0) -> List[Dict[str, str]]:
        """Read Taken/Skipped taps. Each becomes an extra label the learner can
        compare itself against: where did the human disagree, and who was right?"""
        if not self.enabled:
            return []
        out: List[Dict[str, str]] = []
        try:
            r = self._transport("getUpdates",
                                {"offset": offset, "timeout": 0,
                                 "allowed_updates": ["callback_query"]})
        except Exception as e:
            self.stats.last_error = f"poll: {e}"
            return []
        for upd in r.get("result", []) or []:
            cq = upd.get("callback_query")
            if not cq:
                continue
            data = str(cq.get("data", ""))
            if ":" not in data:
                continue
            action, sid = data.split(":", 1)
            self.human_tags[sid] = action
            out.append({"signal_id": sid, "action": action,
                        "update_id": str(upd.get("update_id", ""))})
        return out

    def health(self) -> Dict[str, object]:
        return {"enabled": self.enabled, "sent": self.stats.sent,
                "failed": self.stats.failed, "queued": len(self.queue),
                "rate_limited": self.stats.rate_limited,
                "last_error": self.stats.last_error,
                "human_tags": len(self.human_tags)}
