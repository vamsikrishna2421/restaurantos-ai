"""Realtime fan-out (ARCHITECTURE §9): ``bus`` (P10a: commit-hook queues, lanes, spawn, cursors) and ``hub`` (P10b:
WebSocket fan-out, backlog, presence). Each module is imported on its own through the registry."""

from __future__ import annotations
