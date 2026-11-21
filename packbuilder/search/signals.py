"""The traffic lights of the map, kept in the search pack for the app's guidance (docs/next-round.md
item 36).

The routing engine tells the app about a traffic light only when the signal sits on the very line of
the route. A light that OpenStreetMap has on the cross street's side of a junction, or a few metres
past the point where the route turns off, gets no icon. So the pack carries every light and the app
joins them to the junctions of its route by distance (RouteLights.kt).

Table (build.py):
  signal(cell, lat, lon)
      every highway=traffic_signals node. cell = (floor(lat*100)+9000)*40000 + floor(lon*100)+18000:
      a square of 0.01 degree, so the lights along a route are read cell by cell.

Plain functions; pyosmium is not needed here.
"""
from __future__ import annotations

import math


def cell(lat: float, lon: float) -> int:
    return (math.floor(lat * 100) + 9000) * 40000 + math.floor(lon * 100) + 18000


def is_signal(tags) -> bool:
    return tags.get("highway") == "traffic_signals"
