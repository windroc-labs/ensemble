"""A usage reading's one-line summary."""


def summary(usage):
    if usage is None:
        return "unknown"
    return " ".join(
        f"{k}={'none' if w is None else f'{w.used_percent:g}%'}"
        for k, w in (("five_hour", usage.five_hour), ("weekly", usage.weekly))
    )
