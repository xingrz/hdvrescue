"""Real-calendar arithmetic for AUX recording timecodes.

AUX timecodes feed two decisions: the order spans are chained in, and the
"does B follow A in time?" elapsed check that decides whether two spans belong to
one recording. Both require true calendar math — an approximation that treats a
month as a fixed number of days mis-orders spans across month boundaries and
breaks at year ends, which can mis-chain or even weld two recordings together. We
use the real proleptic-Gregorian calendar via :func:`calendar.timegm`.
"""

import calendar


def aux_epoch(aux):
    """Seconds since the Unix epoch (UTC, no timezone games) for an AUX hit.

    ``aux`` is the ``aux.first``/``aux.last`` shape ``{"date": (y,m,d),
    "time": (h,m,s) | None}`` (or a tuple ``((y,m,d), (h,m,s)|None)``). Returns
    ``None`` if there is no date. Missing time counts as 00:00:00.
    """
    date, time = _split(aux)
    if not date:
        return None
    y, mo, d = date
    h, mi, s = time if time else (0, 0, 0)
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def aux_elapsed(a, b):
    """Real-calendar seconds from AUX hit ``a`` to AUX hit ``b`` (``b - a``), or
    ``None`` if either lacks a date."""
    ea, eb = aux_epoch(a), aux_epoch(b)
    if ea is None or eb is None:
        return None
    return eb - ea


def same_date(a, b):
    """True iff both AUX hits carry a date and the dates are equal."""
    da, _ = _split(a)
    db, _ = _split(b)
    return da is not None and db is not None and da == db


def _split(aux):
    """Normalise either the dict form or the tuple form to ``(date, time)``."""
    if aux is None:
        return (None, None)
    if isinstance(aux, dict):
        return (aux.get("date"), aux.get("time"))
    # tuple/namedtuple: (date, time, ...)
    date = aux[0]
    time = aux[1] if len(aux) > 1 else None
    return (date, time)
