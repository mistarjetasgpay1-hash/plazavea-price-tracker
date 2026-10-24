"""Historial de ofertas notificadas (TTL + bajada de precio) y candado de instancia."""

from .store import StateStore, exclusive_lock

__all__ = ["StateStore", "exclusive_lock"]
