# -*- coding: utf-8 -*-
"""
Classe principal do plugin QGIS 4 / QGIS 3: GeoTSE - Espacializador Eleitoral.
Autor: Luís Eduardo Araújo de Souza
"""

import os
from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from . import _reload_submodules
from .gui.main_dialog import TseGeoBrasilDialog
from .processing.tse_provider import TseProcessingProvider


class TseGeoBrasilPlugin:
    """Gerencia o ciclo de vida do plugin no QGIS (barra de ferramentas, menu e Processing)."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.action = None
        self.provider = None
        self.dialog = None
        self.menu_name = "&GeoTSE - Espacializador Eleitoral"

    def initProcessing(self) -> None:
        """Registra o provedor na Caixa de Ferramentas de Processamento."""
        self.provider = TseProcessingProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self) -> None:
        """Adiciona o botão na barra de ferramentas, item no menu Vetor e provedor Processing."""
        self.initProcessing()

        icon_path = os.path.join(self.plugin_dir, "icon.svg")
        self.action = QAction(
            QIcon(icon_path),
            "GeoTSE - Espacializador Eleitoral...",
            self.iface.mainWindow(),
        )
        self.action.setToolTip(
            "Importar, converter código TSE (5 dígitos -> IBGE 7 dígitos), pivotar linhas e espacializar dados do TSE"
        )
        self.action.triggered.connect(self.run)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu(self.menu_name, self.action)

    def unload(self) -> None:
        """Remove ícones, menus e o provedor Processing ao desativar o plugin."""
        if self.action is not None:
            self.iface.removePluginVectorMenu(self.menu_name, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    def run(self) -> None:
        """Abre a janela principal do plugin recarregando os submódulos atualizados."""
        _reload_submodules()
        from .gui.main_dialog import TseGeoBrasilDialog as FreshDialog
        self.dialog = FreshDialog(self.iface)
        self.dialog.exec()
