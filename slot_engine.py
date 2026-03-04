from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import List
from dateutil import parser as dtparser
import logging
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Interval:
    start: datetime  # timezone-aware
    end: datetime    # timezone-aware

    def valid(self) -> bool:
        return self.start < self.end

def parse_hhmm(s: str) -> time:
    # expects "HH:MM"
    hh, mm = s.strip().split(":")
    return time(hour=int(hh), minute=int(mm))

def parse_date_ymd(s: str) -> date:
    # expects "YYYY-MM-DD"
    return date.fromisoformat(s.strip())

def to_local_dt(d: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=tz)

def parse_any_iso(dt_str: str) -> datetime:
    # Parses RFC3339 / ISO 8601, returns aware datetime
    dt = dtparser.isoparse(dt_str)
    if dt.tzinfo is None:
        raise ValueError(f"Datetime must include timezone offset or Z: {dt_str}")
    return dt

def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    xs = [i for i in intervals if i.valid()]
    if not xs:
        return []
    xs.sort(key=lambda i: i.start)
    merged = [xs[0]]
    for cur in xs[1:]:
        last = merged[-1]
        if cur.start <= last.end:
            merged[-1] = Interval(last.start, max(last.end, cur.end))
        else:
            merged.append(cur)
    return merged

def subtract_busy(free: List[Interval], busy: List[Interval]) -> List[Interval]:
    # free - busy
    if not free:
        return []
    if not busy:
        return free

    busy_m = merge_intervals(busy)
    out: List[Interval] = []
    for f in free:
        segments = [f]
        for b in busy_m:
            new_segments = []
            for seg in segments:
                # no overlap
                if b.end <= seg.start or b.start >= seg.end:
                    new_segments.append(seg)
                    continue
                # overlap exists: cut seg into left/right
                if b.start > seg.start:
                    new_segments.append(Interval(seg.start, b.start))
                if b.end < seg.end:
                    new_segments.append(Interval(b.end, seg.end))
            segments = new_segments
            if not segments:
                break
        out.extend([s for s in segments if s.valid()])
    return merge_intervals(out)

def generate_slots_from_intervals(
    intervals: List[Interval],
    duration_min: int,
    buffer_min: int
) -> List[Interval]:
    logger.info(f"Generating slots from {len(intervals)} intervals")
    logger.info(f"Slot duration: {duration_min} min | Buffer: {buffer_min} min")
    slots: List[Interval] = []
    step = timedelta(minutes=duration_min + buffer_min)
    dur = timedelta(minutes=duration_min)

    for inter in intervals:
        t = inter.start
        while True:
            end = t + dur
            if end > inter.end:
                break
            slots.append(Interval(t, end))
            t = t + step
    logger.info(f"Generated {len(slots)} slots")
    return slots