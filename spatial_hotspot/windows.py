"""Time-window parsing, bucketing and late-event semantics.

Semantics (documented for API consumers, see ``docs/SEMANTICS.md``):

* Every window is **half-open** ``[start, end)`` on **event time**
  (the timestamp carried by the event), never on ingest/wall-clock time.
* Windows are never closed.  There is no watermark after which an event is
  refused: a late event with an old timestamp is accepted, deduplicated by
  ``event_id``, and retroactively updates the time buckets covering its
  timestamp.  Query results therefore change when the underlying data
  changes; cached results that overlap the late event's time are
  invalidated and must be recomputed.
* ``start=None, end=None`` means "all available event time"; ``end=None``
  with a start means ``[start, +inf)``.
* Time buckets are fixed-width and anchored at the POSIX epoch, so bucket
  keys are stable regardless of ingest order.
"""

import math

DEFAULT_BUCKET_WIDTH_S = 3600.0


def parse_window(
    start: float | None,
    end: float | None,
) -> tuple[float | None, float | None]:
    """Validate an ``[start, end)`` event-time window.

    ``None`` bounds are passed through as unbounded.  Raises ``ValueError``
    when bounds are finite but inverted.
    """

    if start is not None and end is not None and start > end:
        raise ValueError(f"window start {start} is after end {end}")
    return start, end


def in_window(
    ts: float,
    start: float | None,
    end: float | None,
) -> bool:
    """Whether event time ``ts`` falls in the half-open window."""

    if start is not None and ts < start:
        return False
    if end is not None and ts >= end:
        return False
    return True


def time_bucket(ts: float, bucket_width_s: float = DEFAULT_BUCKET_WIDTH_S) -> int:
    """Epoch-anchored fixed-width bucket index containing ``ts``."""

    return math.floor(ts / bucket_width_s)


def bucket_bounds(
    bucket: int,
    bucket_width_s: float = DEFAULT_BUCKET_WIDTH_S,
) -> tuple[float, float]:
    """``[lo, hi)`` event-time interval of a bucket."""

    return bucket * bucket_width_s, (bucket + 1) * bucket_width_s


def bucket_range(
    start: float | None,
    end: float | None,
    min_bucket: int,
    max_bucket: int,
    bucket_width_s: float = DEFAULT_BUCKET_WIDTH_S,
) -> range:
    """Bucket indices overlapping ``[start, end)`` within a known range."""

    lo = min_bucket if start is None else max(min_bucket, time_bucket(start, bucket_width_s))
    if end is None:
        hi = max_bucket
    else:
        hi = min(
            max_bucket,
            math.ceil(end / bucket_width_s) - 1,
        )
    if hi < lo:
        return range(0)
    return range(lo, hi + 1)
