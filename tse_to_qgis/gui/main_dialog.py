# -*- coding: utf-8 -*-
"""
Janela principal do plugin GeoTSE - Espacializador Eleitoral.
Autor: Luís Eduardo Araújo de Souza
Layout compacto otimizado para telas de notebook (1366x768 e Full HD com escala 125%/150%),
compatível com QGIS 4 (PyQt6 / Qt6) e QGIS 3 (PyQt5 / Qt5).
"""

import os
from typing import Dict, List, Optional, Set

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.ibge_api import IbgeMalhasClient
from ..core.layer_builder import TseLayerBuilder
from ..core.tse_engine import (
    TseEngine,
    TseFileMetadata,
    TseProcessingConfig,
)


class TseGeoBrasilDialog(QDialog):
    """Diálogo compacto e responsivo para importar, agregar e espacializar tabelas do TSE no QGIS."""

    MODE_IBGE_API = "ibge_api"
    MODE_EXISTING_LAYER = "existing_layer"
    MODE_POINTS_LOCAL = "points_local"
    MODE_TABLE_ONLY = "table_only"

    def __init__(self, iface, parent: Optional[QWidget] = None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.engine = TseEngine()
        self.current_meta: Optional[TseFileMetadata] = None
        self._layer_map: Dict[str, QgsVectorLayer] = {}

        self.setWindowTitle("GeoTSE - Espacializador Eleitoral")
        self._setup_ui()
        self._populate_project_layers()
        self._adjust_to_notebook_screen()

    def _adjust_to_notebook_screen(self) -> None:
        """Ajusta as dimensões da janela para caber perfeitamente em telas de notebook."""
        self.setMinimumSize(580, 390)
        target_w, target_h = 660, 490
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(680, int(avail.width() * 0.85))
            target_h = min(500, int(avail.height() * 0.82))
        self.resize(target_w, target_h)

    def _setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(6)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, stretch=1)

        # ==================================================================
        # ABA 1: Configuração Principal (dentro de QScrollArea para segurança)
        # ==================================================================
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        if hasattr(QFrame, "Shape"):
            scroll.setFrameShape(QFrame.Shape.NoFrame)
        else:
            scroll.setFrameShape(QFrame.NoFrame)

        main_page = QWidget()
        page_layout = QVBoxLayout(main_page)
        page_layout.setContentsMargins(4, 4, 4, 4)
        page_layout.setSpacing(6)

        # ------------------------------------------------------------------
        # 1. Arquivo do TSE (.csv, .txt ou .zip)
        # ------------------------------------------------------------------
        grp_file = QGroupBox("1. Arquivo de Dados Abertos do TSE (.csv, .txt ou .zip)")
        grid_file = QGridLayout(grp_file)
        grid_file.setContentsMargins(8, 6, 8, 6)
        grid_file.setVerticalSpacing(4)
        grid_file.setHorizontalSpacing(6)

        self.txt_file_path = QLineEdit()
        self.txt_file_path.setPlaceholderText(
            "Selecione qualquer arquivo do TSE (votacao_candidato, detalhe, perfil, locais...)"
        )
        self.btn_browse = QPushButton("Procurar...")
        self.btn_browse.clicked.connect(self._on_browse_file)

        grid_file.addWidget(QLabel("Arquivo TSE:"), 0, 0)
        grid_file.addWidget(self.txt_file_path, 0, 1)
        grid_file.addWidget(self.btn_browse, 0, 2)

        self.lbl_zip_member = QLabel("Tabela (ZIP):")
        self.cmb_zip_member = QComboBox()
        self.cmb_zip_member.setEnabled(False)
        self.cmb_zip_member.currentIndexChanged.connect(self._on_zip_member_changed)
        grid_file.addWidget(self.lbl_zip_member, 1, 0)
        grid_file.addWidget(self.cmb_zip_member, 1, 1, 1, 2)
        self.lbl_zip_member.setVisible(False)
        self.cmb_zip_member.setVisible(False)

        self.lbl_profile_detected = QLabel(
            "<i>Selecione um arquivo para detectar o layout automaticamente.</i>"
        )
        self.lbl_profile_detected.setWordWrap(True)
        grid_file.addWidget(QLabel("Layout:"), 2, 0)
        grid_file.addWidget(self.lbl_profile_detected, 2, 1, 1, 2)

        page_layout.addWidget(grp_file)

        # ------------------------------------------------------------------
        # 2. Chave Municipal (TSE 5 dígitos -> IBGE 7 dígitos) e Filtros
        # ------------------------------------------------------------------
        grp_filters = QGroupBox("2. Chave Municipal (TSE 5 díg. -> IBGE 7 díg.) e Recorte Eleitoral")
        grid_filters = QGridLayout(grp_filters)
        grid_filters.setContentsMargins(8, 6, 8, 6)
        grid_filters.setVerticalSpacing(4)
        grid_filters.setHorizontalSpacing(6)

        self.cmb_mun_code = QComboBox()
        self.cmb_filter_uf = QComboBox()
        self.cmb_filter_uf.addItem("TODAS", "TODAS")
        self.cmb_filter_municipio = QComboBox()
        self.cmb_filter_municipio.addItem("TODOS (Estado Inteiro)", "TODOS")
        self.cmb_filter_municipio.setToolTip(
            "Filtra um município específico (ideal para analisar Locais de Votação de uma cidade)."
        )
        self.cmb_filter_cargo = QComboBox()
        self.cmb_filter_cargo.addItem("TODOS (Separar em Camadas por Cargo)", "TODOS")
        self.cmb_filter_turno = QComboBox()
        self.cmb_filter_turno.addItem("TODOS", "TODOS")

        row_f1 = QHBoxLayout()
        row_f1.setSpacing(6)
        row_f1.addWidget(QLabel("Coluna TSE:"))
        row_f1.addWidget(self.cmb_mun_code, stretch=2)
        row_f1.addWidget(QLabel("UF:"))
        row_f1.addWidget(self.cmb_filter_uf, stretch=1)
        row_f1.addWidget(QLabel("Município:"))
        row_f1.addWidget(self.cmb_filter_municipio, stretch=3)
        grid_filters.addLayout(row_f1, 0, 0)

        row_f2 = QHBoxLayout()
        row_f2.setSpacing(6)
        row_f2.addWidget(QLabel("Cargo:"))
        row_f2.addWidget(self.cmb_filter_cargo, stretch=3)
        row_f2.addWidget(QLabel("Turno:"))
        row_f2.addWidget(self.cmb_filter_turno, stretch=1)
        grid_filters.addLayout(row_f2, 1, 0)

        page_layout.addWidget(grp_filters)

        # ------------------------------------------------------------------
        # 3. Tratamento das Linhas (Agregação e Pivot em Colunas)
        # ------------------------------------------------------------------
        grp_pivot = QGroupBox("3. Tratamento das Linhas (Agregação por Município e Pivot em Colunas)")
        vbox_pivot = QVBoxLayout(grp_pivot)
        vbox_pivot.setContentsMargins(8, 6, 8, 6)
        vbox_pivot.setSpacing(4)

        row_agg = QHBoxLayout()
        row_agg.setSpacing(6)
        row_agg.addWidget(QLabel("Agregação:"))
        self.cmb_agg_level = QComboBox()
        self.cmb_agg_level.addItem(
            "Por Município (1 linha = 1 município com CD_MUN_IBGE 7 dígitos)", "municipio"
        )
        self.cmb_agg_level.addItem(
            "Por Local de Votação (1 ponto = 1 local com Latitude/Longitude)", "local_votacao"
        )
        self.cmb_agg_level.currentIndexChanged.connect(self._on_agg_level_changed)
        row_agg.addWidget(self.cmb_agg_level, stretch=1)

        self.chk_enable_pivot = QCheckBox("Pivotar linhas em colunas")
        self.chk_enable_pivot.setChecked(True)
        self.chk_enable_pivot.toggled.connect(self._on_pivot_toggled)
        row_agg.addWidget(self.chk_enable_pivot)
        vbox_pivot.addLayout(row_agg)

        row_cols = QHBoxLayout()
        row_cols.setSpacing(6)
        row_cols.addWidget(QLabel("Virar Colunas:"))
        self.cmb_pivot_col = QComboBox()
        row_cols.addWidget(self.cmb_pivot_col, stretch=1)
        row_cols.addWidget(QLabel("Somar Métrica:"))
        self.cmb_value_col = QComboBox()
        row_cols.addWidget(self.cmb_value_col, stretch=1)
        vbox_pivot.addLayout(row_cols)

        row_opts = QHBoxLayout()
        row_opts.setSpacing(6)
        row_opts.addWidget(QLabel("Top N:"))
        self.spn_top_n = QSpinBox()
        self.spn_top_n.setRange(0, 200)
        self.spn_top_n.setValue(15)
        self.spn_top_n.setToolTip("Quantidade máxima de colunas criadas (0 = todas).")
        row_opts.addWidget(self.spn_top_n)

        row_opts.addWidget(QLabel("Filtrar:"))
        self.txt_specific_pivots = QLineEdit()
        self.txt_specific_pivots.setPlaceholderText("Opcional: ex. 13, 22 ou PT, PL, MDB")
        row_opts.addWidget(self.txt_specific_pivots, stretch=1)

        self.chk_percentages = QCheckBox("Calcular %")
        self.chk_percentages.setChecked(True)
        self.chk_winner = QCheckBox("VENCEDOR e Margem")
        self.chk_winner.setChecked(True)
        row_opts.addWidget(self.chk_percentages)
        row_opts.addWidget(self.chk_winner)
        vbox_pivot.addLayout(row_opts)

        page_layout.addWidget(grp_pivot)

        # ------------------------------------------------------------------
        # 4. Modo de Espacialização (Compactado em ComboBox + Linha Dinâmica)
        # ------------------------------------------------------------------
        grp_spatial = QGroupBox("4. Modo de Espacialização e Saída")
        vbox_spatial = QVBoxLayout(grp_spatial)
        vbox_spatial.setContentsMargins(8, 6, 8, 6)
        vbox_spatial.setSpacing(4)

        row_mode = QHBoxLayout()
        row_mode.setSpacing(6)
        row_mode.addWidget(QLabel("Geometria:"))
        self.cmb_spatial_mode = QComboBox()
        self.cmb_spatial_mode.addItem(
            "Baixar malha municipal automaticamente da API oficial do IBGE (GeoJSON 7 dígitos)",
            self.MODE_IBGE_API,
        )
        self.cmb_spatial_mode.addItem(
            "Unir (Join 1:1) com camada de municípios já aberta no projeto do QGIS",
            self.MODE_EXISTING_LAYER,
        )
        self.cmb_spatial_mode.addItem(
            "Gerar camada de Pontos dos Locais de Votação (usa NR_LATITUDE / NR_LONGITUDE)",
            self.MODE_POINTS_LOCAL,
        )
        self.cmb_spatial_mode.addItem(
            "Gerar apenas Tabela Padronizada (sem geometria, com CD_MUN_IBGE de 7 dígitos)",
            self.MODE_TABLE_ONLY,
        )
        self.cmb_spatial_mode.currentIndexChanged.connect(self._update_spatial_controls)
        row_mode.addWidget(self.cmb_spatial_mode, stretch=1)
        vbox_spatial.addLayout(row_mode)

        self.wid_ibge_api = QWidget()
        row_api = QHBoxLayout(self.wid_ibge_api)
        row_api.setContentsMargins(0, 0, 0, 0)
        row_api.setSpacing(6)
        row_api.addWidget(QLabel("Qualidade da malha IBGE:"))
        self.cmb_ibge_quality = QComboBox()
        self.cmb_ibge_quality.addItem("Intermediária (Recomendada - Rápida)", "intermediaria")
        self.cmb_ibge_quality.addItem("Máxima (Alta definição cartográfica)", "maxima")
        self.cmb_ibge_quality.addItem("Mínima (Simplificada)", "minima")
        row_api.addWidget(self.cmb_ibge_quality, stretch=1)
        vbox_spatial.addWidget(self.wid_ibge_api)

        self.wid_existing_layer = QWidget()
        row_layer = QHBoxLayout(self.wid_existing_layer)
        row_layer.setContentsMargins(0, 0, 0, 0)
        row_layer.setSpacing(6)
        row_layer.addWidget(QLabel("Camada QGIS:"))
        self.cmb_existing_layer = QComboBox()
        self.cmb_existing_layer.currentIndexChanged.connect(self._on_existing_layer_changed)
        row_layer.addWidget(self.cmb_existing_layer, stretch=2)
        row_layer.addWidget(QLabel("Campo Código IBGE:"))
        self.cmb_ibge_field = QComboBox()
        row_layer.addWidget(self.cmb_ibge_field, stretch=1)
        self.btn_refresh_layers = QPushButton("Atualizar")
        self.btn_refresh_layers.clicked.connect(self._populate_project_layers)
        row_layer.addWidget(self.btn_refresh_layers)
        vbox_spatial.addWidget(self.wid_existing_layer)

        row_style = QHBoxLayout()
        row_style.setSpacing(6)
        row_style.addWidget(QLabel("Simbologia:"))
        self.cmb_symbology_mode = QComboBox()
        self.cmb_symbology_mode.addItem(
            "Vencedor Graduado por Margem (Claro = Disputado -> Escuro = Vitória Folgada)",
            "winner_graduated",
        )
        self.cmb_symbology_mode.addItem(
            "Vencedor Bruto (Cor Sólida Oficial do Partido / Candidato)",
            "winner_solid",
        )
        self.cmb_symbology_mode.addItem(
            "Graduado por % de Votos (1º Candidato ou Candidato Filtrado)",
            "candidate_graduated",
        )
        self.cmb_symbology_mode.addItem(
            "Sem Simbologia Automática",
            "none",
        )
        row_style.addWidget(self.cmb_symbology_mode, stretch=1)
        self.chk_keep_unmatched = QCheckBox("Manter sem votos")
        row_style.addWidget(self.chk_keep_unmatched)
        vbox_spatial.addLayout(row_style)

        row_out = QHBoxLayout()
        row_out.setSpacing(6)
        row_out.addWidget(QLabel("GeoPackage (.gpkg):"))
        self.txt_output_gpkg = QLineEdit()
        self.txt_output_gpkg.setPlaceholderText("Em branco = Camada Temporária em Memória")
        self.btn_browse_out = QPushButton("Salvar...")
        self.btn_browse_out.clicked.connect(self._on_browse_output)
        row_out.addWidget(self.txt_output_gpkg, stretch=1)
        row_out.addWidget(self.btn_browse_out)
        vbox_spatial.addLayout(row_out)

        page_layout.addWidget(grp_spatial)
        page_layout.addStretch(1)

        scroll.setWidget(main_page)
        self.tabs.addTab(scroll, "1. Configuração e Espacialização")

        # ==================================================================
        # ABA 2: Log Detalhado de Processamento
        # ==================================================================
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(6, 6, 6, 6)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        log_layout.addWidget(self.txt_log)
        self.tabs.addTab(log_page, "2. Log de Execução")

        # ==================================================================
        # RODAPÉ FIXO (Sempre visível em qualquer resolução de notebook)
        # ==================================================================
        footer_row = QHBoxLayout()
        footer_row.setSpacing(8)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumWidth(180)
        footer_row.addWidget(self.progress_bar)

        self.lbl_status = QLabel("Pronto.")
        footer_row.addWidget(self.lbl_status, stretch=1)

        self.btn_run = QPushButton("Processar e Gerar Camada no QGIS")
        self.btn_run.setMinimumHeight(30)
        self.btn_run.setStyleSheet("font-weight: bold; padding: 4px 12px;")
        self.btn_run.clicked.connect(self._on_run_clicked)

        self.btn_close = QPushButton("Fechar")
        self.btn_close.setMinimumHeight(30)
        self.btn_close.clicked.connect(self.reject)

        footer_row.addWidget(self.btn_run)
        footer_row.addWidget(self.btn_close)
        root_layout.addLayout(footer_row)

        self._update_spatial_controls()

    def _log(self, msg: str) -> None:
        self.lbl_status.setText(msg[:85] + ("..." if len(msg) > 85 else ""))
        self.txt_log.append(msg)
        QCoreApplication.processEvents()

    def _populate_project_layers(self) -> None:
        self.cmb_existing_layer.clear()
        self._layer_map.clear()
        project_layers = QgsProject.instance().mapLayers().values()
        for lyr in project_layers:
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                self._layer_map[lyr.id()] = lyr
                self.cmb_existing_layer.addItem(lyr.name(), lyr.id())

        self._on_existing_layer_changed()

    def _on_existing_layer_changed(self) -> None:
        self.cmb_ibge_field.clear()
        layer_id = self.cmb_existing_layer.currentData()
        if not layer_id or layer_id not in self._layer_map:
            return
        lyr = self._layer_map[layer_id]
        fields = [f.name() for f in lyr.fields()]
        self.cmb_ibge_field.addItems(fields)

        preferred = [
            "CD_MUN", "CD_MUN_IBGE", "CD_GEOCMU", "CD_GEOCODM",
            "CODAREA", "COD_IBGE", "CD_IBGE", "IBGE",
        ]
        upper_map = {f.upper(): idx for idx, f in enumerate(fields)}
        for cand in preferred:
            if cand in upper_map:
                self.cmb_ibge_field.setCurrentIndex(upper_map[cand])
                break

    def _update_spatial_controls(self) -> None:
        mode = self.cmb_spatial_mode.currentData()
        self.wid_ibge_api.setVisible(mode == self.MODE_IBGE_API)
        self.wid_existing_layer.setVisible(mode == self.MODE_EXISTING_LAYER)
        self.chk_keep_unmatched.setVisible(mode in (self.MODE_IBGE_API, self.MODE_EXISTING_LAYER))
        self.cmb_symbology_mode.setEnabled(mode != self.MODE_TABLE_ONLY)
        if mode == self.MODE_POINTS_LOCAL and self.cmb_agg_level.currentData() != "local_votacao":
            self.cmb_agg_level.blockSignals(True)
            self.cmb_agg_level.setCurrentIndex(1)
            self.cmb_agg_level.blockSignals(False)

    def _set_spatial_mode(self, mode_key: str) -> None:
        idx = self.cmb_spatial_mode.findData(mode_key)
        if idx >= 0:
            self.cmb_spatial_mode.setCurrentIndex(idx)

    def _on_browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Selecionar arquivo de dados abertos do TSE",
            "",
            "Arquivos do TSE (*.csv *.txt *.zip);;Todos os arquivos (*.*)",
        )
        if not path:
            return
        self.txt_file_path.setText(path)
        self._inspect_selected_file(path, None)

    def _on_browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Salvar camada resultante em GeoPackage",
            "",
            "GeoPackage (*.gpkg)",
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.txt_output_gpkg.setText(path)

    def _on_zip_member_changed(self) -> None:
        if not self.cmb_zip_member.isEnabled():
            return
        path = self.txt_file_path.text().strip()
        member = self.cmb_zip_member.currentText()
        if path and member:
            self._inspect_selected_file(path, member, reload_zip_list=False)

    def _inspect_selected_file(
        self, file_path: str, zip_member: Optional[str], reload_zip_list: bool = True
    ) -> None:
        try:
            self._log(f"Inspecionando: {os.path.basename(file_path)}...")
            meta = self.engine.inspect_file(file_path, zip_member)
            self.current_meta = meta

            if reload_zip_list:
                self.cmb_zip_member.blockSignals(True)
                self.cmb_zip_member.clear()
                if meta.zip_csv_members:
                    self.cmb_zip_member.addItems(meta.zip_csv_members)
                    self.cmb_zip_member.setEnabled(True)
                    self.lbl_zip_member.setVisible(True)
                    self.cmb_zip_member.setVisible(True)
                    if meta.zip_member in meta.zip_csv_members:
                        self.cmb_zip_member.setCurrentText(meta.zip_member)
                else:
                    self.cmb_zip_member.setEnabled(False)
                    self.lbl_zip_member.setVisible(False)
                    self.cmb_zip_member.setVisible(False)
                self.cmb_zip_member.blockSignals(False)

            self.lbl_profile_detected.setText(
                f"<b>{meta.profile_description}</b> "
                f"(<code>{meta.encoding}</code> | sep: <code>{meta.delimiter}</code>)"
            )

            self.cmb_mun_code.clear()
            self.cmb_mun_code.addItems(meta.headers)
            if meta.mun_code_col in meta.headers:
                self.cmb_mun_code.setCurrentText(meta.mun_code_col)

            self.cmb_filter_uf.clear()
            self.cmb_filter_uf.addItem("TODAS", "TODAS")
            for u in meta.available_ufs:
                self.cmb_filter_uf.addItem(u, u)
            if len(meta.available_ufs) == 1:
                self.cmb_filter_uf.setCurrentIndex(1)

            self.cmb_filter_municipio.clear()
            self.cmb_filter_municipio.addItem("TODOS (Estado Inteiro)", "TODOS")
            for m in meta.available_municipios:
                self.cmb_filter_municipio.addItem(m, m)

            self.cmb_filter_cargo.clear()
            self.cmb_filter_cargo.addItem("TODOS (Separar em Camadas por Cargo)", "TODOS")
            for c in meta.available_cargos:
                self.cmb_filter_cargo.addItem(c, c)
            if len(meta.available_cargos) == 1:
                self.cmb_filter_cargo.setCurrentIndex(1)

            self.cmb_filter_turno.clear()
            self.cmb_filter_turno.addItem("TODOS", "TODOS")
            for t in meta.available_turnos:
                self.cmb_filter_turno.addItem(t, t)
            if len(meta.available_turnos) == 1:
                self.cmb_filter_turno.setCurrentIndex(1)

            self.cmb_pivot_col.clear()
            for col in meta.candidate_pivot_cols:
                if col == "CANDIDATO_COMPOSTO":
                    self.cmb_pivot_col.addItem(
                        "CANDIDATO_COMPOSTO (Nº + Nome de Urna | Separa Legenda)",
                        "CANDIDATO_COMPOSTO",
                    )
                elif col == "PARTIDO_COMPOSTO":
                    self.cmb_pivot_col.addItem(
                        "PARTIDO_COMPOSTO (Agrupar por Partido / Legenda - Ideal p/ Deputados)",
                        "PARTIDO_COMPOSTO",
                    )
                elif col == "AUTO_CARGO":
                    self.cmb_pivot_col.addItem(
                        "AUTO_CARGO (Majoritário = Candidato | Proporcional = Partido)",
                        "AUTO_CARGO",
                    )
                else:
                    self.cmb_pivot_col.addItem(col, col)
            if meta.default_pivot_col:
                idx = self.cmb_pivot_col.findData(meta.default_pivot_col)
                if idx >= 0:
                    self.cmb_pivot_col.setCurrentIndex(idx)

            self.cmb_value_col.clear()
            for col in meta.candidate_value_cols:
                if col == "__COUNT__":
                    self.cmb_value_col.addItem("__COUNT__ (Contagem de registros)", "__COUNT__")
                else:
                    self.cmb_value_col.addItem(col, col)
            if meta.default_value_col:
                idx = self.cmb_value_col.findData(meta.default_value_col)
                if idx >= 0:
                    self.cmb_value_col.setCurrentIndex(idx)

            current_mode = self.cmb_spatial_mode.currentData()
            if meta.profile == "detalhe_votacao":
                self.chk_enable_pivot.setChecked(False)
                self.cmb_agg_level.setCurrentIndex(0)
                if current_mode == self.MODE_POINTS_LOCAL:
                    self._set_spatial_mode(self.MODE_IBGE_API)
            elif meta.profile == "locais_votacao" and meta.has_coordinates:
                self.cmb_agg_level.setCurrentIndex(1)
                self._set_spatial_mode(self.MODE_POINTS_LOCAL)
                self.chk_enable_pivot.setChecked(False)
            else:
                self.cmb_agg_level.setCurrentIndex(0)
                self.chk_enable_pivot.setChecked(bool(meta.default_pivot_col))
                if current_mode == self.MODE_POINTS_LOCAL:
                    self._set_spatial_mode(self.MODE_IBGE_API)

            cargos_txt = ", ".join(meta.available_cargos) if meta.available_cargos else "N/A"
            self._log(
                f"Layout '{meta.profile}' detectado. Cargos encontrados: {cargos_txt}."
            )
        except Exception as e:
            QMessageBox.warning(self, "Erro ao inspecionar arquivo", str(e))

    def _on_agg_level_changed(self) -> None:
        agg_level = self.cmb_agg_level.currentData()
        if agg_level == "local_votacao":
            self._set_spatial_mode(self.MODE_POINTS_LOCAL)
            if self.current_meta and self.current_meta.profile == "locais_votacao":
                self.chk_enable_pivot.setChecked(False)
            else:
                self.chk_enable_pivot.setChecked(True)
        elif self.cmb_spatial_mode.currentData() == self.MODE_POINTS_LOCAL:
            self._set_spatial_mode(self.MODE_IBGE_API)

    def _on_pivot_toggled(self, checked: bool) -> None:
        self.cmb_pivot_col.setEnabled(checked)
        self.spn_top_n.setEnabled(checked)
        self.txt_specific_pivots.setEnabled(checked)
        self.chk_percentages.setEnabled(checked)
        self.chk_winner.setEnabled(checked)

    def _on_run_clicked(self) -> None:
        file_path = self.txt_file_path.text().strip()
        if not file_path or not os.path.exists(file_path):
            QMessageBox.warning(
                self, "Arquivo inválido", "Selecione um arquivo válido do TSE (.csv, .txt ou .zip)."
            )
            return

        spatial_mode = self.cmb_spatial_mode.currentData()
        if spatial_mode == self.MODE_EXISTING_LAYER:
            layer_id = self.cmb_existing_layer.currentData()
            if not layer_id or layer_id not in self._layer_map:
                QMessageBox.warning(
                    self,
                    "Camada não selecionada",
                    "Selecione uma camada vetorial aberta no projeto ou escolha baixar a malha na API do IBGE.",
                )
                return

        zip_member = self.cmb_zip_member.currentText() if self.cmb_zip_member.isEnabled() else None
        specific_list = [
            s.strip() for s in self.txt_specific_pivots.text().split(",") if s.strip()
        ]

        agg_level = self.cmb_agg_level.currentData() or "municipio"
        if spatial_mode == self.MODE_POINTS_LOCAL:
            agg_level = "local_votacao"

        selected_uf = self.cmb_filter_uf.currentData() or self.cmb_filter_uf.currentText()
        selected_mun = self.cmb_filter_municipio.currentData() or self.cmb_filter_municipio.currentText()
        selected_cargo = self.cmb_filter_cargo.currentData() or self.cmb_filter_cargo.currentText()
        selected_turno = self.cmb_filter_turno.currentData() or self.cmb_filter_turno.currentText()
        symbology_mode = self.cmb_symbology_mode.currentData() or "winner_graduated"

        config = TseProcessingConfig(
            file_path=file_path,
            zip_member=zip_member,
            profile="auto",
            mun_code_col=self.cmb_mun_code.currentText(),
            filter_uf=selected_uf,
            filter_municipio=selected_mun,
            filter_cargo=selected_cargo,
            filter_turno=selected_turno,
            aggregation_level=agg_level,
            enable_pivot=self.chk_enable_pivot.isChecked(),
            pivot_col=self.cmb_pivot_col.currentData() or self.cmb_pivot_col.currentText(),
            value_col=self.cmb_value_col.currentData() or self.cmb_value_col.currentText(),
            top_n_pivots=self.spn_top_n.value(),
            specific_pivot_values=specific_list,
            include_percentages=self.chk_percentages.isChecked(),
            include_winner=self.chk_winner.isChecked(),
            split_layers_by_cargo=True,
        )

        self.btn_run.setEnabled(False)
        self.progress_bar.setValue(5)
        self._log("Processando e convertendo código TSE (5 dígitos) -> IBGE (7 dígitos)...")

        try:
            def prog_cb(pct: int, msg: str) -> None:
                self.progress_bar.setValue(pct)
                self._log(msg)

            grouped_results = self.engine.process_by_groups(config, progress_callback=prog_cb)
            grouped_results = [(g_title, res) for g_title, res in grouped_results if res.records]

            if not grouped_results:
                QMessageBox.information(
                    self,
                    "Sem registros para o filtro selecionado",
                    "Nenhum registro municipal correspondeu aos filtros informados (UF / Cargo / Turno).",
                )
                self.progress_bar.setValue(0)
                return

            base_name = os.path.splitext(os.path.basename(zip_member or file_path))[0]
            all_detected_ufs: Set[str] = set()
            for _, res in grouped_results:
                all_detected_ufs.update(res.detected_ufs)

            # Se o modo for API do IBGE, baixa a malha uma única vez para reutilizar em todos os cargos
            ibge_api_source_layer: Optional[QgsVectorLayer] = None
            if spatial_mode == self.MODE_IBGE_API:
                qual = self.cmb_ibge_quality.currentData() or "intermediaria"
                self._log("Baixando malha municipal na API v3 do IBGE...")
                client = IbgeMalhasClient()
                geojson_path = client.fetch_geojson_for_ufs(
                    all_detected_ufs, qualidade=qual, progress_callback=prog_cb
                )
                ibge_api_source_layer = QgsVectorLayer(geojson_path, "ibge_malha_temp", "ogr")
                if not ibge_api_source_layer.isValid():
                    raise RuntimeError(f"Falha ao carregar o GeoJSON da API do IBGE: {geojson_path}")

            # Se houver múltiplos grupos (ex.: Cargo = TODOS), cria um Grupo de Camadas no QGIS
            project = QgsProject.instance()
            layer_tree_root = project.layerTreeRoot()
            uf_label = (
                ", ".join(sorted(all_detected_ufs))
                if len(all_detected_ufs) <= 3
                else "BR"
            )
            if selected_mun and selected_mun not in ("TODOS", "TODAS"):
                scope_label = f"{uf_label} - {selected_mun}"
            else:
                scope_label = uf_label

            use_layer_group = len(grouped_results) > 1
            qgis_group = None
            if use_layer_group:
                group_name = f"GeoTSE - {base_name} ({scope_label})"
                qgis_group = layer_tree_root.insertGroup(0, group_name)
                self._log(f"Criando grupo de camadas '{group_name}' com {len(grouped_results)} camadas...")

            gpkg_path = self.txt_output_gpkg.text().strip()
            created_layer_names: List[str] = []
            total_features_created = 0
            last_valid_layer: Optional[QgsVectorLayer] = None

            for idx, (group_title, result) in enumerate(grouped_results):
                pct = 85 + int(((idx + 1) / len(grouped_results)) * 14)
                self.progress_bar.setValue(min(99, pct))

                if use_layer_group:
                    layer_name = f"{group_title} ({scope_label})"
                else:
                    suffix = f" - {group_title}" if group_title and group_title != "Geral" else ""
                    mun_suffix = f" ({selected_mun})" if (selected_mun and selected_mun not in ("TODOS", "TODAS")) else ""
                    layer_name = f"GeoTSE - {base_name}{suffix}{mun_suffix}"

                self._log(f"Construindo camada: {layer_name}...")

                if spatial_mode == self.MODE_POINTS_LOCAL:
                    out_layer = TseLayerBuilder.build_points_layer(result, layer_name=layer_name)
                elif spatial_mode == self.MODE_TABLE_ONLY:
                    out_layer = TseLayerBuilder.build_table_layer(result, layer_name=layer_name)
                elif spatial_mode == self.MODE_EXISTING_LAYER:
                    src_layer = self._layer_map[self.cmb_existing_layer.currentData()]
                    ibge_field = self.cmb_ibge_field.currentText()
                    out_layer = TseLayerBuilder.build_layer_from_existing_layer(
                        result=result,
                        source_layer=src_layer,
                        ibge_field=ibge_field,
                        layer_name=layer_name,
                        keep_unmatched_polygons=self.chk_keep_unmatched.isChecked(),
                    )
                else:
                    out_layer = TseLayerBuilder.build_layer_from_existing_layer(
                        result=result,
                        source_layer=ibge_api_source_layer,
                        ibge_field="codarea",
                        layer_name=layer_name,
                        keep_unmatched_polygons=self.chk_keep_unmatched.isChecked(),
                    )

                if gpkg_path:
                    out_layer = TseLayerBuilder.save_to_geopackage(
                        out_layer,
                        gpkg_path,
                        layer_name,
                        append_to_existing_gpkg=(idx > 0 or os.path.exists(gpkg_path)),
                    )

                if spatial_mode != self.MODE_TABLE_ONLY and symbology_mode != "none":
                    if symbology_mode == "candidate_graduated" or len(specific_list) == 1:
                        TseLayerBuilder.apply_candidate_graduated_symbology(
                            out_layer, result=result
                        )
                    elif symbology_mode == "winner_solid":
                        TseLayerBuilder.apply_winner_symbology(
                            out_layer, "VENCEDOR", result=result, graduated_margin=False
                        )
                    else:
                        TseLayerBuilder.apply_winner_symbology(
                            out_layer, "VENCEDOR", result=result, graduated_margin=True
                        )

                if qgis_group is not None:
                    project.addMapLayer(out_layer, False)
                    qgis_group.addLayer(out_layer)
                else:
                    project.addMapLayer(out_layer)

                if out_layer.isValid() and out_layer.featureCount() > 0:
                    last_valid_layer = out_layer

                created_layer_names.append(layer_name)
                total_features_created += out_layer.featureCount()

            if last_valid_layer is not None and spatial_mode != self.MODE_TABLE_ONLY:
                try:
                    canvas = self.iface.mapCanvas()
                    if canvas is not None:
                        ext = last_valid_layer.extent()
                        if not ext.isEmpty():
                            ext.scale(1.08)
                            canvas.setExtent(ext)
                            canvas.refresh()
                except Exception:
                    pass

            self.progress_bar.setValue(100)
            total_read = grouped_results[0][1].total_rows_read
            summary = (
                f"Concluído! Linhas lidas: {total_read:,} | "
                f"Camadas geradas: {len(created_layer_names)} ({', '.join(created_layer_names)}) | "
                f"Total de feições: {total_features_created:,}."
            )
            self._log(summary)
            QMessageBox.information(self, "Camadas Geradas com Sucesso", summary)

        except Exception as e:
            self._log(f"ERRO: {e}")
            QMessageBox.critical(self, "Erro durante o processamento", str(e))
        finally:
            self.btn_run.setEnabled(True)
