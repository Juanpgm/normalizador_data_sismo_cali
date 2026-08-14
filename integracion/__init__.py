"""
Integración EDAN ↔ Visitas — Emergencia sismo Cali (agosto 2026).

Standalone, importable package that reproduces the address/coordinate
standardization and the multi-tier entity-resolution cascade originally
prototyped in EDA.ipynb, and adds a cadastral SPATIAL BRIDGE tier plus
progress metrics and tests.

The notebooks are the source of truth for the algorithms; this package does
not modify them. Every function here is a faithful port with identical
behaviour, reorganized into testable modules.
"""

__version__ = "1.0.0"
