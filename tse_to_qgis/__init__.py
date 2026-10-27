# -*- coding: utf-8 -*-
"""
GeoTSE - Espacializador Eleitoral para QGIS 4 / QGIS 3
Autor: Luís Eduardo Araújo de Souza
"""

import importlib
import sys


def _reload_submodules() -> None:
    """Garante que o QGIS recarregue todos os submódulos atualizados da memória."""
    order = [
        "tse_to_qgis.core.municipality_mapper",
        "tse_to_qgis.core.ibge_api",
        "tse_to_qgis.core.tse_locais_client",
        "tse_to_qgis.core.tse_engine",
        "tse_to_qgis.core.layer_builder",
        "tse_to_qgis.processing.tse_provider",
        "tse_to_qgis.gui.main_dialog",
        "tse_to_qgis.plugin",
    ]
    for mod_name in order:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception:
                pass


def classFactory(iface):
    """Inicializa o plugin no QGIS recarregando todos os submódulos."""
    _reload_submodules()
    from .plugin import TseGeoBrasilPlugin
    return TseGeoBrasilPlugin(iface)
