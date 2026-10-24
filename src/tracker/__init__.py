"""Monitor de ofertas de Plaza Vea con alertas por Telegram.

Paquetes:
  core/        configuración, modelos, HTTP, filtros, orquestador (pipeline) y modo continuo
  scrapers/    extracción de datos (VTEX · Plaza Vea)
  promotions/  procesador de promociones: Tarjeta Oh!, combos, NxM, precio por unidad
  telegram/    cliente de la API de Telegram y plantillas de mensajes
  state/       historial con TTL, regla de bajada de precio, migración y candado
"""

__version__ = "3.1.0"
