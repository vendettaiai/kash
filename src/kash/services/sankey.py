"""A three-column Sankey diagram for the dashboard: money in → total → money out.

Pure geometry for an SVG (no JavaScript needed to draw it). Every band's width
is proportional to its amount; both sides always add up to the same total, so
the caller balances them (a surplus is "Saved", a deficit "From savings").
"""

from dataclasses import dataclass, field
from decimal import Decimal

WIDTH = 330.0  # about the width of a phone card, so text keeps its size
NODE = 8.0  # node bar width
GAP = 10.0  # space between nodes in a column
ROW = 30.0  # height per node, so labels have room
MIN_HEIGHT = 150.0
LABEL_SPACING = 14.0  # smallest distance between two labels in a column


@dataclass(frozen=True)
class Flow:
    name: str
    amount: Decimal
    color: str
    href: str = ""


@dataclass
class Node:
    x: float
    y: float
    h: float
    color: str
    flow: Flow
    label_y: float = 0.0


@dataclass(frozen=True)
class Link:
    d: str
    color: str
    flow: Flow


@dataclass
class Sankey:
    width: float
    height: float
    total: Decimal
    sources: list[Node] = field(default_factory=list)
    middle: Node | None = None
    targets: list[Node] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


def _band(x0: float, a0: float, a1: float, x1: float, b0: float, b1: float) -> str:
    """A filled band from (x0, a0..a1) to (x1, b0..b1) with curved edges."""
    xm = (x0 + x1) / 2
    return (
        f"M{x0:.1f},{a0:.1f} C{xm:.1f},{a0:.1f} {xm:.1f},{b0:.1f} {x1:.1f},{b0:.1f} "
        f"L{x1:.1f},{b1:.1f} C{xm:.1f},{b1:.1f} {xm:.1f},{a1:.1f} {x0:.1f},{a1:.1f} Z"
    )


def _column(flows: list[Flow], x: float, scale: float, height: float) -> list[Node]:
    heights = [max(float(f.amount) * scale, 1.5) for f in flows]
    used = sum(heights) + GAP * (len(flows) - 1)
    y = (height - used) / 2
    nodes = []
    for f, h in zip(flows, heights, strict=True):
        nodes.append(Node(x, y, h, f.color, f))
        y += h + GAP
    _spread_labels(nodes, height)
    return nodes


def _spread_labels(nodes: list[Node], height: float) -> None:
    """Labels at their node's middle, pushed apart so none overlap."""
    last = -LABEL_SPACING
    for n in nodes:
        n.label_y = max(n.y + n.h / 2, last + LABEL_SPACING)
        last = n.label_y
    overflow = last - (height - 4)
    if overflow > 0:  # shift the whole column up, keeping the spacing
        for n in nodes:
            n.label_y -= overflow


def build(sources: list[Flow], targets: list[Flow], width: float = WIDTH) -> Sankey | None:
    """The diagram, or None when there is nothing to show. ``width`` is in SVG
    units; pick it close to the card's pixel width so the text stays readable."""
    sources = [f for f in sources if f.amount > 0]
    targets = [f for f in targets if f.amount > 0]
    total = sum((f.amount for f in sources), Decimal("0"))
    if not sources or not targets or total <= 0:
        return None
    rows = max(len(sources), len(targets))
    height = max(MIN_HEIGHT, rows * ROW)
    usable = height - GAP * (rows - 1)
    scale = usable / float(total)

    chart = Sankey(width=width, height=height, total=total)
    chart.sources = _column(sources, 0.0, scale, height)
    chart.targets = _column(targets, width - NODE, scale, height)
    mid_h = float(total) * scale
    chart.middle = Node(
        (width - NODE) / 2, (height - mid_h) / 2, mid_h, "#64748b", Flow("Total", total, "")
    )

    # Bands stack along the middle node in the same order as the nodes.
    m = chart.middle
    y = m.y
    for n in chart.sources:
        h = float(n.flow.amount) * scale
        chart.links.append(Link(_band(n.x + NODE, n.y, n.y + h, m.x, y, y + h), n.color, n.flow))
        y += h
    y = m.y
    for n in chart.targets:
        h = float(n.flow.amount) * scale
        chart.links.append(Link(_band(m.x + NODE, y, y + h, n.x, n.y, n.y + h), n.color, n.flow))
        y += h
    return chart
