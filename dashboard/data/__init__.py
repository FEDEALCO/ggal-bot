"""
dashboard/data/
================
Capa de datos del dashboard, SEPARADA de la UI (ver dashboard/app.py).

Regla de arquitectura de este paquete (mandato explicito del usuario,
2026-09-30 - ver REPORT.md Fase 1): todo lo que este aca es una funcion
PURA (recibe datos, devuelve datos - nunca importa streamlit, nunca
dibuja nada) y TESTEADA (ver ggal_bot/validation/test_dashboard_data_*.py).

FUENTE UNICA DE VERDAD para PnL/reconstruccion de trades y posiciones:
ggal_bot/backtest/ (reconstruct.py, costs.py, metrics.py, attribution.py).
Este paquete NUNCA reimplementa esa matematica - solo carga los CSV/JSON
crudos y los convierte al formato que esas funciones ya testeadas esperan.
"""
