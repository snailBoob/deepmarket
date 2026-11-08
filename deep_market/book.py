"""Order book for one exchange: integer price ticks -> size in base units."""

BID = 0
ASK = 1


class Book:
    __slots__ = ("sides",)

    def __init__(self):
        self.sides = ({}, {})  # (bids, asks)

    @property
    def bids(self):
        return self.sides[BID]

    @property
    def asks(self):
        return self.sides[ASK]

    def apply(self, side, tick, size):
        """Set one level; size 0 removes it. Returns the previous size (0.0 if absent)."""
        levels = self.sides[side]
        if size > 0.0:
            old = levels.get(tick, 0.0)
            levels[tick] = size
        else:
            old = levels.pop(tick, 0.0)
        return old

    def reset(self, bids, asks):
        """Replace the whole book. Returns the changes [(side, tick, new_size)] that turn
        the old book into the new one, so a change log stays continuous across resyncs."""
        changes = []
        for side, new in ((BID, bids), (ASK, asks)):
            old = self.sides[side]
            for tick in old.keys() - new.keys():
                changes.append((side, tick, 0.0))
            for tick, size in new.items():
                if old.get(tick) != size:
                    changes.append((side, tick, size))
        self.sides = (dict(bids), dict(asks))
        return changes

    def best_bid(self):
        return max(self.sides[BID]) if self.sides[BID] else None

    def best_ask(self):
        return min(self.sides[ASK]) if self.sides[ASK] else None

    def copy(self):
        b = Book()
        b.sides = (dict(self.sides[BID]), dict(self.sides[ASK]))
        return b
