"""Activity rules: who met the monthly requirements and who didn't."""
from __future__ import annotations

from dataclasses import dataclass, field

from .utils import jalali_month_range, month_weeks, now_ts, short_jdate


@dataclass
class WeekResult:
    index: int
    label: str
    valid: int
    pending: int
    finished: bool


@dataclass
class MemberResult:
    discord_id: int
    kind: str
    skipped_new: bool = False
    weeks: list[WeekResult] = field(default_factory=list)
    videos_valid: int = 0
    videos_pending: int = 0
    stream_fail: bool = False
    video_fail: bool = False
    month_finished: bool = True

    @property
    def failed(self) -> bool:
        return self.stream_fail or self.video_fail

    @property
    def pending(self) -> int:
        return self.videos_pending + sum(w.pending for w in self.weeks)


def evaluate_member(member: dict, streams: list[dict], videos: list[dict], jy: int, jm: int, rules,
                    now: int | None = None) -> MemberResult:
    """`rules` needs: min_streams_per_week, min_stream_minutes, min_videos_per_month, min_video_minutes."""
    now = now or now_ts()
    m_start, m_end = jalali_month_range(jy, jm)
    res = MemberResult(member["discord_id"], member["kind"], month_finished=now >= m_end)
    if member["added_at"] > m_start:
        res.skipped_new = True
        return res

    if member["kind"] in ("streamer", "both"):
        min_len = rules.min_stream_minutes * 60
        for i, (ws, we, sat, fri) in enumerate(month_weeks(jy, jm)):
            finished = now >= we
            valid = pending = 0
            for s in streams:
                if s["status"] != "ended" or not s["started_at"] or not (ws <= s["started_at"] < we):
                    continue
                if (s["ended_at"] or 0) - s["started_at"] < min_len:
                    continue
                if s["server_related"] == 1:
                    valid += 1
                elif s["server_related"] is None:
                    pending += 1
            label = f"Week {i + 1} ({short_jdate(sat)} - {short_jdate(fri)})"
            res.weeks.append(WeekResult(i, label, valid, pending, finished))
            if finished and valid < rules.min_streams_per_week:
                res.stream_fail = True

    if member["kind"] in ("creator", "both"):
        min_len = rules.min_video_minutes * 60
        for v in videos:
            if not (m_start <= v["published_at"] < m_end):
                continue
            if v["duration_sec"] is not None and v["duration_sec"] < min_len:
                continue
            if v["server_related"] == 1:
                res.videos_valid += 1
            elif v["server_related"] is None:
                res.videos_pending += 1
        if res.month_finished and res.videos_valid < rules.min_videos_per_month:
            res.video_fail = True
    return res


def _mark(ok: bool, finished: bool) -> str:
    if ok:
        return "OK"
    return "In progress" if not finished else "Missed"


def member_lines(r: MemberResult, rules, only_failures: bool = True) -> list[str]:
    """Detail lines (stream weeks / videos) for one member."""
    out = []
    if r.weeks:
        weeks = r.weeks if not only_failures else [
            w for w in r.weeks if w.finished and w.valid < rules.min_streams_per_week]
        for w in weeks:
            mark = _mark(w.valid >= rules.min_streams_per_week, w.finished)
            extra = f", {w.pending} awaiting confirmation" if w.pending else ""
            out.append(f"> {w.label}: **{w.valid}/{rules.min_streams_per_week}** streams - {mark}{extra}")
    if r.kind in ("creator", "both") and (not only_failures or r.video_fail):
        mark = _mark(r.videos_valid >= rules.min_videos_per_month, r.month_finished)
        extra = f", {r.videos_pending} awaiting confirmation" if r.videos_pending else ""
        out.append(f"> Videos this month: **{r.videos_valid}/{rules.min_videos_per_month}** - {mark}{extra}")
    return out
