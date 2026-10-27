# -*- coding: utf-8 -*-
"""
Algoritmo e Provedor do QGIS Processing para automação e processamento em lote
de arquivos do TSE no QGIS 4 / QGIS 3.
"""

import os
from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProcessingProvider,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QIcon

from ..core.ibge_api import IbgeMalhasClient
from ..core.layer_builder import TseLayerBuilder
from ..core.municipality_mapper import MunicipalityMapper
from ..core.tse_engine import TseEngine, TseProcessingConfig


class TseSpatializeAlgorithm(QgsProcessingAlgorithm):
    """Algoritmo da Caixa de Ferramentas para espacializar arquivos do TSE."""

    INPUT_FILE = "INPUT_FILE"
    FILTER_UF = "FILTER_UF"
    FILTER_CARGO = "FILTER_CARGO"
    FILTER_TURNO = "FILTER_TURNO"
    ENABLE_PIVOT = "ENABLE_PIVOT"
    PIVOT_COL = "PIVOT_COL"
    VALUE_COL = "VALUE_COL"
    TOP_N = "TOP_N"
    SPATIAL_MODE = "SPATIAL_MODE"
    EXISTING_LAYER = "EXISTING_LAYER"
    IBGE_FIELD = "IBGE_FIELD"
    OUTPUT = "OUTPUT"

    def name(self) -> str:
        return "espacializar_dados_tse"

    def displayName(self) -> str:
        return "Espacializar e Agregar Dados do TSE (5 dígitos -> IBGE 7 dígitos)"

    def group(self) -> str:
        return "Dados Eleitorais"

    def groupId(self) -> str:
        return "dados_eleitorais"

    def shortHelpString(self) -> str:
        return (
            "Lê qualquer arquivo de dados abertos do TSE (.csv, .txt ou .zip), converte "
            "automaticamente o código municipal de 5 dígitos do TSE (CD_MUNICIPIO / SG_UE) "
            "para o código IBGE de 7 dígitos (CD_MUN_IBGE), agrega zonas/seções e pivota "
            "candidatos/partidos/categorias em colunas.\n\n"
            "Permite baixar a malha municipal automaticamente da API v3 do IBGE, unir com "
            "uma camada existente no projeto ou gerar pontos de locais de votação."
        )

    def createInstance(self):
        return TseSpatializeAlgorithm()

    def initAlgorithm(self, config=None) -> None:
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_FILE,
                "Arquivo de Dados do TSE (.csv, .txt ou .zip)",
                behavior=QgsProcessingParameterFile.Behavior.File,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FILTER_UF,
                "Filtrar UF (ex.: SP, MG, AC ou vazio para TODAS)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FILTER_CARGO,
                "Filtrar Cargo (ex.: PREFEITO, PRESIDENTE ou vazio para TODOS)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FILTER_TURNO,
                "Filtrar Turno (1, 2 ou vazio para TODOS)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ENABLE_PIVOT,
                "Pivotar candidatos/partidos/categorias em colunas (1 linha por município)",
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PIVOT_COL,
                "Coluna para Pivotar (vazio = detecção automática, ex.: CANDIDATO_COMPOSTO, SG_PARTIDO)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.VALUE_COL,
                "Coluna de Valor para Somar (vazio = detecção automática)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOP_N,
                "Máximo de colunas pivotadas - Top N (0 = todas)",
                type=QgsProcessingParameterNumber.Type.Integer,
                defaultValue=15,
                minValue=0,
                maxValue=200,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.SPATIAL_MODE,
                "Modo de Espacialização",
                options=[
                    "Baixar Malha Municipal Automaticamente da API do IBGE",
                    "Unir com Camada Municipal Existente no QGIS",
                    "Gerar Pontos de Locais de Votação (NR_LATITUDE / NR_LONGITUDE)",
                    "Gerar Apenas Tabela Padronizada (Sem Geometria)",
                ],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.EXISTING_LAYER,
                "Camada Municipal Existente (usada no modo 2)",
                types=[QgsProcessing.SourceType.TypeVectorPolygon],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.IBGE_FIELD,
                "Campo do Código IBGE (7 dígitos) na Camada Existente",
                parentLayerParameterName=self.EXISTING_LAYER,
                defaultValue="CD_MUN",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                "Camada TSE Espacializada",
            )
        )

    def processAlgorithm(
        self,
        parameters: dict,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        file_path = self.parameterAsFile(parameters, self.INPUT_FILE, context)
        if not file_path or not os.path.exists(file_path):
            raise QgsProcessingException("Arquivo do TSE não encontrado.")

        spatial_mode = self.parameterAsEnum(parameters, self.SPATIAL_MODE, context)
        agg_level = "local_votacao" if spatial_mode == 2 else "municipio"

        cfg = TseProcessingConfig(
            file_path=file_path,
            filter_uf=self.parameterAsString(parameters, self.FILTER_UF, context),
            filter_cargo=self.parameterAsString(parameters, self.FILTER_CARGO, context),
            filter_turno=self.parameterAsString(parameters, self.FILTER_TURNO, context),
            aggregation_level=agg_level,
            enable_pivot=self.parameterAsBool(parameters, self.ENABLE_PIVOT, context),
            pivot_col=self.parameterAsString(parameters, self.PIVOT_COL, context),
            value_col=self.parameterAsString(parameters, self.VALUE_COL, context),
            top_n_pivots=self.parameterAsInt(parameters, self.TOP_N, context),
        )

        engine = TseEngine()

        def cb(pct: int, msg: str) -> None:
            feedback.setProgress(pct)
            feedback.pushInfo(msg)

        result = engine.process(cfg, progress_callback=cb)
        fields = TseLayerBuilder.build_fields(result.field_specs)

        if spatial_mode == 0:
            client = IbgeMalhasClient()
            geojson_path = client.fetch_geojson_for_ufs(result.detected_ufs, progress_callback=cb)
            source_layer = QgsVectorLayer(geojson_path, "ibge_api", "ogr")
            ibge_field = "codarea"
            wkb_type = QgsWkbTypes.Type.MultiPolygon
            crs = source_layer.crs()
        elif spatial_mode == 1:
            source_layer = self.parameterAsVectorLayer(parameters, self.EXISTING_LAYER, context)
            if source_layer is None:
                raise QgsProcessingException("Selecione uma camada municipal existente para o modo 2.")
            ibge_field = self.parameterAsString(parameters, self.IBGE_FIELD, context) or "CD_MUN"
            wkb_type = source_layer.wkbType()
            crs = source_layer.crs()
        elif spatial_mode == 2:
            source_layer = None
            ibge_field = ""
            wkb_type = QgsWkbTypes.Type.Point
            crs = context.project().crs()
        else:
            source_layer = None
            ibge_field = ""
            wkb_type = QgsWkbTypes.Type.NoGeometry
            crs = context.project().crs()

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, wkb_type, crs
        )
        if sink is None:
            raise QgsProcessingException("Erro ao criar a camada de saída (FeatureSink).")

        if spatial_mode in (0, 1) and source_layer is not None:
            by_ibge7 = {str(r.get("CD_MUN_IBGE", "")): r for r in result.records}
            mapper = MunicipalityMapper.instance()
            for src_feat in source_layer.getFeatures():
                code7 = mapper.format_ibge_code(src_feat[ibge_field])
                rec = by_ibge7.get(code7)
                if rec is None:
                    continue
                feat = QgsFeature(fields)
                feat.setGeometry(src_feat.geometry())
                feat.setAttributes([rec.get(name) for name, _ in result.field_specs])
                sink.addFeature(feat)
        elif spatial_mode == 2:
            for rec in result.records:
                lat = rec.get("LATITUDE")
                lon = rec.get("LONGITUDE")
                if lat is None or lon is None:
                    continue
                feat = QgsFeature(fields)
                feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(lon), float(lat))))
                feat.setAttributes([rec.get(name) for name, _ in result.field_specs])
                sink.addFeature(feat)
        else:
            for rec in result.records:
                feat = QgsFeature(fields)
                feat.setAttributes([rec.get(name) for name, _ in result.field_specs])
                sink.addFeature(feat)

        feedback.setProgress(100)
        return {self.OUTPUT: dest_id}


class TseProcessingProvider(QgsProcessingProvider):
    """Provedor registrado na Caixa de Ferramentas de Processamento do QGIS."""

    def id(self) -> str:
        return "geotse"

    def name(self) -> str:
        return "GeoTSE - Espacializador Eleitoral"

    def icon(self) -> QIcon:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return QIcon(os.path.join(base_dir, "icon.svg"))

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(TseSpatializeAlgorithm())
