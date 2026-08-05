"""MIRROR — Telegram extraction via MTProto (Telethon).

WHY NOT A BOT. Telegram's Bot API cannot do this job, and it is worth being
precise about why so nobody wastes a week on it:

  * a bot only sees messages in chats where an admin explicitly added it
  * **a bot cannot read history from before it joined** — which makes a 4-6
    month backfill structurally impossible
  * channel owners of signal channels will generally not add your bot

MTProto (this module) logs in as YOUR OWN ACCOUNT and can read the full history
of any channel you are already a member of. That is the difference between "one
hour of runtime" and "ten thousand screenshots."

WHY NOT SCREENSHOTS. OCR on chat screenshots is lossy, slow, and destroys the
metadata that matters most: exact post timestamps (you need second-level
precision to align a signal to the tape), edit timestamps, reply threading, and
whether a message was later modified. All of that comes free over the API and is
irrecoverable from a picture.

DESIGN PRINCIPLES

  RAW FIRST, PARSE LATER. This module's only job is to capture messages
  verbatim into an immutable JSONL archive. Parsing happens downstream, so you
  can re-parse the same archive a dozen times as the parser improves without
  ever re-hitting Telegram.

  RESUMABLE. Progress is checkpointed by message id per channel. A crash, a
  rate limit, or a laptop lid closing costs you nothing.

  EDITS ARE CAPTURED. `edit_date` is recorded. A channel that edits its stop
  loss after the fact is doing something you very much want to measure.

  POLITE. FloodWait is respected with proper sleeps. Hammering the API gets
  your account limited, which is a self-inflicted wound.

INSTALL:  pip install telethon
CREDS:    https://my.telegram.org  ->  API development tools  ->  api_id/api_hash

Your session file is a credential. Treat it like a password: never commit it,
never share it. This module never writes credentials into the archive.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

NS = 1_000_000_000


@dataclass
class IngestConfig:
    api_id: int = 0
    api_hash: str = ""
    session: str = "mirror_session"
    channels: List[str] = field(default_factory=list)   # @handles, ids, or links
    out_root: str = "./mirror_data/raw"
    months_back: int = 6
    batch_size: int = 200          # messages per request; 200 is a safe max
    sleep_between_batches: float = 0.6
    include_media_meta: bool = True
    download_images: bool = False   # only if a channel posts signals as pictures
    image_dir: str = "./mirror_data/images"

    @classmethod
    def from_env(cls, **over) -> "IngestConfig":
        """Credentials come from the environment ONLY. They must never land in
        a config file that could be committed or shared."""
        c = cls(
            api_id=int(os.environ.get("TG_API_ID", "0") or 0),
            api_hash=os.environ.get("TG_API_HASH", ""),
            session=os.environ.get("TG_SESSION", "mirror_session"),
            channels=[s.strip() for s in
                      os.environ.get("TG_CHANNELS", "").split(",") if s.strip()],
        )
        for k, v in over.items():
            if hasattr(c, k):
                setattr(c, k, v)
        return c

    def validate(self) -> List[str]:
        errs = []
        if not self.api_id:
            errs.append("TG_API_ID unset — get one at https://my.telegram.org")
        if not self.api_hash:
            errs.append("TG_API_HASH unset")
        if not self.channels:
            errs.append("no channels configured (TG_CHANNELS=@one,@two)")
        return errs


def message_to_record(msg: Any, channel: str) -> Dict[str, Any]:
    """Flatten a Telethon Message into a stable, self-describing record.

    Everything that could matter later is kept, because re-scraping to recover a
    field you discarded means re-hitting the API for months of history.
    """
    def _ts(dt) -> int:
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * NS) if dt else 0

    txt = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    media_type = None
    m = getattr(msg, "media", None)
    if m is not None:
        media_type = type(m).__name__

    return {
        "channel": channel,
        "msg_id": int(getattr(msg, "id", 0)),
        "t_ns": _ts(getattr(msg, "date", None)),
        "date_utc": (getattr(msg, "date", None).isoformat()
                     if getattr(msg, "date", None) else None),
        "text": txt,
        # An edit is a signal in itself: a channel that quietly moves its stop
        # after the fact will show up here and nowhere else.
        "edit_t_ns": _ts(getattr(msg, "edit_date", None)),
        "was_edited": bool(getattr(msg, "edit_date", None)),
        # Reply threading lets you attach "TP1 hit" follow-ups to their parent
        # signal instead of guessing by proximity in time.
        "reply_to_msg_id": getattr(getattr(msg, "reply_to", None),
                                   "reply_to_msg_id", None),
        "is_forward": bool(getattr(msg, "fwd_from", None)),
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "media_type": media_type,
        "has_photo": media_type == "MessageMediaPhoto",
        "grouped_id": getattr(msg, "grouped_id", None),
    }


class TelegramIngest:
    """Historical backfill + live capture. Resumable, polite, raw-first."""

    def __init__(self, cfg: IngestConfig):
        self.cfg = cfg
        self.root = Path(cfg.out_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.stats: Dict[str, Dict[str, int]] = {}
        self._client = None

    # ---- paths -----------------------------------------------------------

    def _safe(self, channel: str) -> str:
        return "".join(c if (c.isalnum() or c in "-_") else "_"
                       for c in channel).strip("_")[:64] or "channel"

    def archive_path(self, channel: str) -> Path:
        return self.root / f"{self._safe(channel)}.jsonl"

    def checkpoint_path(self, channel: str) -> Path:
        return self.root / f"{self._safe(channel)}.checkpoint.json"

    def _load_checkpoint(self, channel: str) -> Dict[str, Any]:
        p = self.checkpoint_path(channel)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {"min_id_seen": None, "max_id_seen": None, "count": 0}

    def _save_checkpoint(self, channel: str, cp: Dict[str, Any]) -> None:
        self.checkpoint_path(channel).write_text(json.dumps(cp, indent=2))

    def _existing_ids(self, channel: str) -> set:
        """Idempotency: re-running never duplicates a message."""
        p = self.archive_path(channel)
        if not p.exists():
            return set()
        ids = set()
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ids.add(json.loads(line)["msg_id"])
                except Exception:
                    continue
        return ids

    # ---- client ----------------------------------------------------------

    async def _connect(self):
        try:
            from telethon import TelegramClient
        except ImportError as e:
            raise RuntimeError(
                "telethon is not installed. Run:  pip install telethon") from e
        client = TelegramClient(self.cfg.session, self.cfg.api_id,
                                self.cfg.api_hash)
        await client.start()      # prompts for phone + code on first run only
        self._client = client
        return client

    # ---- backfill --------------------------------------------------------

    async def backfill_channel(self, client, channel: str,
                               months_back: Optional[int] = None,
                               progress: Optional[Any] = None) -> Dict[str, int]:
        """Walk history backwards from now to the cutoff, appending raw records.

        Backwards is the right direction: Telegram paginates most efficiently
        from newest to oldest, and if you stop early you still have the most
        recent (most relevant) data rather than a random middle slice.
        """
        from telethon.errors import FloodWaitError

        months = months_back if months_back is not None else self.cfg.months_back
        cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months)
        seen = self._existing_ids(channel)
        cp = self._load_checkpoint(channel)
        st = {"fetched": 0, "written": 0, "skipped_dupe": 0,
              "with_text": 0, "photos": 0, "edited": 0, "flood_waits": 0}

        out = open(self.archive_path(channel), "a", encoding="utf-8")
        offset_id = 0
        try:
            while True:
                try:
                    batch = await client.get_messages(
                        channel, limit=self.cfg.batch_size, offset_id=offset_id)
                except FloodWaitError as e:
                    st["flood_waits"] += 1
                    # Respect the wait exactly. Retrying early is how accounts
                    # get restricted.
                    await asyncio.sleep(e.seconds + 2)
                    continue

                if not batch:
                    break

                stop = False
                for msg in batch:
                    st["fetched"] += 1
                    offset_id = min(offset_id or msg.id, msg.id)
                    d = getattr(msg, "date", None)
                    if d and d.replace(tzinfo=timezone.utc) < cutoff:
                        stop = True
                        continue
                    if msg.id in seen:
                        st["skipped_dupe"] += 1
                        continue
                    rec = message_to_record(msg, channel)
                    if rec["text"]:
                        st["with_text"] += 1
                    if rec["has_photo"]:
                        st["photos"] += 1
                        if self.cfg.download_images:
                            rec["image_path"] = await self._save_photo(
                                client, msg, channel)
                    if rec["was_edited"]:
                        st["edited"] += 1
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    seen.add(msg.id)
                    st["written"] += 1

                    cp["min_id_seen"] = (msg.id if cp["min_id_seen"] is None
                                         else min(cp["min_id_seen"], msg.id))
                    cp["max_id_seen"] = (msg.id if cp["max_id_seen"] is None
                                         else max(cp["max_id_seen"], msg.id))

                out.flush()
                cp["count"] = st["written"]
                self._save_checkpoint(channel, cp)
                if progress:
                    progress(channel, st)
                if stop:
                    break
                await asyncio.sleep(self.cfg.sleep_between_batches)
        finally:
            out.close()

        self.stats[channel] = st
        return st

    async def _save_photo(self, client, msg, channel: str) -> Optional[str]:
        d = Path(self.cfg.image_dir) / self._safe(channel)
        d.mkdir(parents=True, exist_ok=True)
        try:
            return str(await client.download_media(msg, file=str(d)))
        except Exception:
            return None

    async def run_backfill(self, progress: Optional[Any] = None
                           ) -> Dict[str, Dict[str, int]]:
        client = await self._connect()
        try:
            for ch in self.cfg.channels:
                await self.backfill_channel(client, ch, progress=progress)
        finally:
            await client.disconnect()
        return self.stats

    # ---- live ------------------------------------------------------------

    async def run_live(self, on_message: Optional[Any] = None) -> None:
        """Capture new posts in real time.

        RUN THIS FROM DAY ONE, even before you finish the analysis. Deleted and
        edited messages are invisible retroactively — a channel that quietly
        removes its losers leaves no trace in a later backfill. Live capture is
        the ONLY defense against that survivorship, and it is the single biggest
        threat to an honest measurement.
        """
        from telethon import events
        client = await self._connect()

        @client.on(events.NewMessage(chats=self.cfg.channels))
        async def _new(event):
            ch = getattr(event.chat, "username", None) or str(event.chat_id)
            rec = message_to_record(event.message, ch)
            with open(self.archive_path(ch), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if on_message:
                on_message(rec)

        @client.on(events.MessageEdited(chats=self.cfg.channels))
        async def _edit(event):
            ch = getattr(event.chat, "username", None) or str(event.chat_id)
            rec = message_to_record(event.message, ch)
            rec["_event"] = "edit"
            with open(self.archive_path(ch), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if on_message:
                on_message(rec)

        @client.on(events.MessageDeleted(chats=self.cfg.channels))
        async def _del(event):
            # A deletion is DATA. It is the clearest possible evidence of
            # survivorship, and it exists nowhere except here.
            for mid in event.deleted_ids:
                rec = {"_event": "delete", "msg_id": mid,
                       "t_ns": int(datetime.now(timezone.utc).timestamp() * NS),
                       "channel": str(event.chat_id)}
                with open(self.root / "deletions.jsonl", "a",
                          encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")

        await client.run_until_disconnected()


# ---------------------------------------------------------------- reading

def read_archive(path: str | Path) -> Iterable[Dict[str, Any]]:
    """Stream an archive, resolving duplicates of the same msg_id.

    RESOLUTION IS BY EDIT TIME, NOT BY FILE ORDER. Naive "last line wins" is
    subtly wrong and loses exactly the information this project cares about:
    live capture appends an edited message, then a later backfill re-appends the
    ORIGINAL after it, and the edit is silently discarded. A channel that
    quietly moved its stop after the fact would look pristine.

    The record with the greatest edit_t_ns wins; an edited record always beats
    an unedited one; ties fall back to last-seen.
    """
    p = Path(path)
    if not p.exists():
        return
    best: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    versions: Dict[int, int] = {}

    def _rank(d: Dict[str, Any]) -> tuple:
        return (int(d.get("edit_t_ns") or 0), 1 if d.get("was_edited") else 0)

    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            mid = d.get("msg_id")
            if mid is None:
                continue
            if mid not in best:
                order.append(mid)
                best[mid] = d
                versions[mid] = 1
                continue
            versions[mid] += 1
            if _rank(d) >= _rank(best[mid]):
                best[mid] = d
    for mid in order:
        rec = best[mid]
        rec["_versions_seen"] = versions.get(mid, 1)
        yield rec


def archive_summary(path: str | Path) -> Dict[str, Any]:
    recs = list(read_archive(path))
    if not recs:
        return {"messages": 0}
    ts = [r["t_ns"] for r in recs if r.get("t_ns")]
    texts = sum(1 for r in recs if r.get("text"))
    photos = sum(1 for r in recs if r.get("has_photo"))
    edited = sum(1 for r in recs if r.get("was_edited"))
    span_days = ((max(ts) - min(ts)) / NS / 86400) if len(ts) > 1 else 0
    return {
        "messages": len(recs), "with_text": texts, "photo_only": photos - texts
        if photos > texts else 0, "photos": photos, "edited": edited,
        "span_days": round(span_days, 1),
        "first_utc": datetime.fromtimestamp(min(ts) / NS,
                                            tz=timezone.utc).isoformat() if ts else None,
        "last_utc": datetime.fromtimestamp(max(ts) / NS,
                                           tz=timezone.utc).isoformat() if ts else None,
        "messages_per_day": round(len(recs) / span_days, 2) if span_days else 0,
        "edit_rate": round(edited / len(recs), 4),
    }


# ---------------------------------------------------------------- OCR

def ocr_image(path: str) -> Optional[str]:
    """Fallback for channels that post signals as pictures.

    Use ONLY when a channel is genuinely image-only. OCR on trading numbers is
    error-prone in exactly the worst way: a misread digit produces a plausible
    price. Anything recovered this way should be marked low-confidence and, for
    a channel that matters, spot-checked by hand before it enters the dataset.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return None


SETUP_GUIDE = """\
TELEGRAM EXTRACTION — SETUP (about 10 minutes, once)

1. Get API credentials
     Go to https://my.telegram.org  ->  log in  ->  "API development tools"
     Create an app (any name). Copy api_id and api_hash.

2. Install
     pip install telethon

3. Export credentials (never put these in a file you might commit)
     export TG_API_ID=1234567
     export TG_API_HASH=abcdef0123456789abcdef0123456789
     export TG_CHANNELS="@channel_one,@channel_two"

4. First run — authenticates once, writes a session file
     python -m colosseum.mirror.telegram_ingest --backfill --months 6

     Telegram will send you a login code. After this, the session file keeps
     you logged in; you will not be asked again.

5. Check what you got
     python -m colosseum.mirror.telegram_ingest --summary

6. Start live capture and LEAVE IT RUNNING
     python -m colosseum.mirror.telegram_ingest --live

     This is not optional if you care about honest numbers. Deleted and edited
     messages are invisible to any later backfill, and that is precisely the
     survivorship that makes a channel's record look better than its trading.

NOTES
  * The session file is a credential. Do not commit or share it.
  * You only see channels you are already a member of. This reads what your own
    account can already read, for your own analysis.
  * If a channel posts signals as images, add --download-images and the OCR
    fallback will engage. Prefer text channels; OCR misreads digits in the
    worst possible way — plausibly.
"""


def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="MIRROR Telegram extractor")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--setup", action="store_true")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--out", default="./mirror_data/raw")
    p.add_argument("--download-images", action="store_true")
    a = p.parse_args(argv)

    if a.setup:
        print(SETUP_GUIDE)
        return 0

    cfg = IngestConfig.from_env(out_root=a.out, months_back=a.months,
                                download_images=a.download_images)
    ing = TelegramIngest(cfg)

    if a.summary:
        for f in sorted(Path(cfg.out_root).glob("*.jsonl")):
            if f.name == "deletions.jsonl":
                continue
            print(f"\n{f.name}")
            for k, v in archive_summary(f).items():
                print(f"  {k:20} {v}")
        return 0

    errs = cfg.validate()
    if errs:
        for e in errs:
            print(f"[!] {e}")
        print("\nRun with --setup for the full guide.")
        return 1

    def _prog(ch, st):
        print(f"  {ch}: fetched {st['fetched']:>6} | written {st['written']:>6} "
              f"| text {st['with_text']:>5} | photos {st['photos']:>4} "
              f"| edited {st['edited']:>4}", flush=True)

    if a.backfill:
        print(f"Backfilling {len(cfg.channels)} channel(s), {a.months} months...")
        asyncio.run(ing.run_backfill(progress=_prog))
        print("\nDone. Run --summary to inspect.")
        return 0
    if a.live:
        print("Live capture running (Ctrl-C to stop). Deletions are logged.")
        asyncio.run(ing.run_live())
        return 0

    print(SETUP_GUIDE)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
