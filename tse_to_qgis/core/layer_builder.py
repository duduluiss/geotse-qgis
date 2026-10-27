# -*- coding: utf-8 -*-
"""
Construtor de camadas vetoriais do QGIS (compatível com QGIS 4 / PyQt6 e QGIS 3).
Suporta:
  1. União 1:1 com camada municipal já existente no QGIS (código IBGE 7 ou 6 dígitos)
  2. Download automático da malha municipal via API v3 do IBGE (GeoJSON -> Camada Poligonal)
  3. Geração de camada de pontos para Locais de Votação (NR_LATITUDE / NR_LONGITUDE)
  4. Geração de tabela alfanumérica padronizada (sem geometria)
"""

import colorsys
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsFillSymbol,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSymbolLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

try:
    from qgis.PyQt.QtCore import QMetaType
    HAS_QMETATYPE = True
except ImportError:
    HAS_QMETATYPE = False

from .ibge_api import IbgeMalhasClient
from .municipality_mapper import MunicipalityMapper
from .tse_engine import TSE_PARTY_INFO, TseAggregationResult


def create_qgs_field(name: str, field_type: str) -> QgsField:
    """Cria QgsField compatível tanto com QGIS 4 (QMetaType.Type) quanto com QGIS 3 (QVariant)."""
    if HAS_QMETATYPE and hasattr(QMetaType, "Type"):
        if field_type == "int":
            return QgsField(name, QMetaType.Type.LongLong)
        elif field_type == "float":
            return QgsField(name, QMetaType.Type.Double)
        else:
            return QgsField(name, QMetaType.Type.QString)
    else:
        if field_type == "int":
            return QgsField(name, QVariant.LongLong)
        elif field_type == "float":
            return QgsField(name, QVariant.Double)
        else:
            return QgsField(name, QVariant.String)


class TseLayerBuilder:
    """Constrói e estiliza camadas espaciais do QGIS a partir do resultado agregado do TSE."""

    @staticmethod
    def build_fields(field_specs: List[Tuple[str, str]]) -> QgsFields:
        fields = QgsFields()
        for name, ftype in field_specs:
            fields.append(create_qgs_field(name, ftype))
        return fields

    @classmethod
    def build_layer_from_ibge_api(
        cls,
        result: TseAggregationResult,
        layer_name: str = "TSE_Municipios_IBGE",
        qualidade: str = "intermediaria",
        keep_unmatched_polygons: bool = False,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> QgsVectorLayer:
        """
        Baixa a malha municipal da API v3 do IBGE para as UFs presentes no resultado
        e une (1:1) com os dados agregados do TSE via CD_MUN_IBGE (7 dígitos).
        """
        client = IbgeMalhasClient()
        geojson_path = client.fetch_geojson_for_ufs(
            result.detected_ufs,
            qualidade=qualidade,
            progress_callback=progress_callback,
        )

        ibge_layer = QgsVectorLayer(geojson_path, "ibge_malha_temp", "ogr")
        if not ibge_layer.isValid():
            raise RuntimeError(f"Falha ao carregar o GeoJSON baixado da API do IBGE: {geojson_path}")

        return cls.build_layer_from_existing_layer(
            result=result,
            source_layer=ibge_layer,
            ibge_field="codarea",
            layer_name=layer_name,
            keep_unmatched_polygons=keep_unmatched_polygons,
        )

    @classmethod
    def build_layer_from_existing_layer(
        cls,
        result: TseAggregationResult,
        source_layer: QgsVectorLayer,
        ibge_field: str,
        layer_name: str = "TSE_Espacializado",
        keep_unmatched_polygons: bool = False,
    ) -> QgsVectorLayer:
        """
        Une os registros agregados do TSE a uma camada de municípios existente no QGIS
        utilizando o código IBGE de 7 dígitos (com fallback automático para 6 dígitos).
        """
        crs_authid = source_layer.crs().authid() or "EPSG:4674"
        geom_type_str = QgsWkbTypes.displayString(source_layer.wkbType())
        if not geom_type_str or "Polygon" in geom_type_str:
            uri_geom = "MultiPolygon"
        elif "Point" in geom_type_str:
            uri_geom = "Point"
        else:
            uri_geom = "MultiPolygon"

        mem_layer = QgsVectorLayer(f"{uri_geom}?crs={crs_authid}", layer_name, "memory")
        provider = mem_layer.dataProvider()

        fields = cls.build_fields(result.field_specs)
        provider.addAttributes(fields.toList())
        mem_layer.updateFields()
        out_fields = mem_layer.fields()

        # Indexa os registros do TSE por código IBGE de 7 e de 6 dígitos
        by_ibge7: Dict[str, Dict[str, Any]] = {}
        by_ibge6: Dict[str, Dict[str, Any]] = {}
        for rec in result.records:
            code7 = str(rec.get("CD_MUN_IBGE", "")).strip()
            if code7:
                by_ibge7[code7] = rec
                if len(code7) >= 6:
                    by_ibge6[code7[:6]] = rec

        mapper = MunicipalityMapper.instance()
        out_features: List[QgsFeature] = []

        for src_feat in source_layer.getFeatures():
            raw_code = src_feat[ibge_field]
            clean_code = mapper.format_ibge_code(raw_code)
            rec = by_ibge7.get(clean_code)
            if rec is None and len(clean_code) >= 6:
                # Suporte a malhas antigas que usam código IBGE de 6 dígitos (sem dígito verificador)
                raw_str = str(raw_code).strip()
                rec = by_ibge6.get(raw_str[:6])

            if rec is None and not keep_unmatched_polygons:
                continue

            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(src_feat.geometry())

            if rec is not None:
                attrs = [rec.get(name) for name, _ in result.field_specs]
            else:
                info = mapper.by_ibge.get(clean_code, {})
                empty_rec = {
                    "CD_MUN_IBGE": clean_code,
                    "CD_MUN_TSE": info.get("tse", ""),
                    "SG_UF": info.get("uf", ""),
                    "NM_MUN": info.get("nome", ""),
                }
                attrs = [empty_rec.get(name) for name, _ in result.field_specs]

            new_feat.setAttributes(attrs)
            out_features.append(new_feat)

        provider.addFeatures(out_features)
        mem_layer.updateExtents()
        return mem_layer

    @classmethod
    def build_points_layer(
        cls,
        result: TseAggregationResult,
        layer_name: str = "TSE_Locais_Votacao",
        crs_authid: str = "EPSG:4674",
    ) -> QgsVectorLayer:
        """
        Constrói uma camada de pontos (Locais de Votação) a partir das colunas LATITUDE e LONGITUDE.
        """
        mem_layer = QgsVectorLayer(f"Point?crs={crs_authid}", layer_name, "memory")
        provider = mem_layer.dataProvider()

        fields = cls.build_fields(result.field_specs)
        provider.addAttributes(fields.toList())
        mem_layer.updateFields()
        out_fields = mem_layer.fields()

        out_features: List[QgsFeature] = []
        for rec in result.records:
            lat = rec.get("LATITUDE")
            lon = rec.get("LONGITUDE")
            if lat is None or lon is None:
                continue
            feat = QgsFeature(out_fields)
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(lon), float(lat))))
            feat.setAttributes([rec.get(name) for name, _ in result.field_specs])
            out_features.append(feat)

        provider.addFeatures(out_features)
        mem_layer.updateExtents()
        return mem_layer

    @classmethod
    def build_table_layer(
        cls,
        result: TseAggregationResult,
        layer_name: str = "TSE_Tabela_IBGE",
    ) -> QgsVectorLayer:
        """Constrói uma camada tabular (sem geometria) com o código IBGE de 7 dígitos."""
        mem_layer = QgsVectorLayer("None", layer_name, "memory")
        provider = mem_layer.dataProvider()

        fields = cls.build_fields(result.field_specs)
        provider.addAttributes(fields.toList())
        mem_layer.updateFields()
        out_fields = mem_layer.fields()

        out_features: List[QgsFeature] = []
        for rec in result.records:
            feat = QgsFeature(out_fields)
            feat.setAttributes([rec.get(name) for name, _ in result.field_specs])
            out_features.append(feat)

        provider.addFeatures(out_features)
        return mem_layer

    @staticmethod
    def save_to_geopackage(
        layer: QgsVectorLayer,
        output_path: str,
        layer_name: str,
        append_to_existing_gpkg: bool = False,
    ) -> QgsVectorLayer:
        """Salva uma camada em memória em um arquivo GeoPackage (.gpkg) e recarrega."""
        from .tse_engine import make_safe_slug

        safe_table = make_safe_slug(layer_name, max_len=50).lower() or "tse_camada"
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = safe_table

        if append_to_existing_gpkg and os.path.exists(output_path):
            if hasattr(QgsVectorFileWriter, "ActionOnExistingFile"):
                options.actionOnExistingFile = (
                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                )
            else:
                options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        transform_context = QgsProject.instance().transformContext()

        if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
            err, err_msg, new_file, new_layer = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, output_path, transform_context, options
            )
        else:
            err, err_msg = QgsVectorFileWriter.writeAsVectorFormatV2(
                layer, output_path, transform_context, options
            )

        if err != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(f"Erro ao salvar GeoPackage ({output_path}): {err_msg}")

        uri = f"{output_path}|layername={safe_table}"
        saved_layer = QgsVectorLayer(uri, layer_name, "ogr")
        return saved_layer if saved_layer.isValid() else layer

    @staticmethod
    def _hsl_to_hex(hue_deg: int, sat_pct: int, light_pct: int) -> str:
        """Converte (Hue 0..360, Sat 0..100, Lightness 0..100) em string hexadecimal #RRGGBB."""
        h = (hue_deg % 360) / 360.0
        l = max(0.0, min(1.0, light_pct / 100.0))
        s = max(0.0, min(1.0, sat_pct / 100.0))
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        return QColor(int(round(r * 255)), int(round(g * 255)), int(round(b * 255))).name()

    @classmethod
    def _resolve_candidate_hsl(
        cls,
        candidate_key: str,
        fallback_index: int,
        party_occurrence: Dict[str, int],
    ) -> Tuple[int, int, int, str]:
        """
        Determina a cor (Hue 0-360, Sat 0-100, Lightness 0-100, Hex) de um candidato ou partido
        a partir do número TSE (2 primeiros dígitos) ou sigla partidária, aplicando leve
        deslocamento de matiz quando houver mais de um candidato do mesmo partido na camada.
        """
        key_clean = str(candidate_key or "").strip().upper()
        prefix2 = ""
        if len(key_clean) >= 2 and key_clean[:2].isdigit():
            prefix2 = key_clean[:2]

        matched_info = None
        party_id = ""
        if prefix2 and prefix2 in TSE_PARTY_INFO:
            matched_info = TSE_PARTY_INFO[prefix2]
            party_id = prefix2
        else:
            # Tenta casar por sigla partidária (ex.: "PT", "22_PL", "MDB")
            tokens = [t for t in key_clean.replace("-", "_").split("_") if t]
            for code2, info in TSE_PARTY_INFO.items():
                if info[0].upper() in tokens or key_clean == info[0].upper():
                    matched_info = info
                    party_id = code2
                    break

        if matched_info is not None:
            _, base_h, base_s, base_l = matched_info
            occ = party_occurrence.get(party_id, 0)
            party_occurrence[party_id] = occ + 1
            if occ == 0:
                h, s, l = base_h, base_s, base_l
            else:
                # Desloca levemente o matiz (+16 / -16 graus) para diferenciar candidatos do mesmo partido
                shift = ((occ + 1) // 2) * 16 * (1 if occ % 2 == 1 else -1)
                h = (base_h + shift) % 360
                s = max(55, min(92, base_s - (occ * 4)))
                l = max(36, min(58, base_l + (4 if occ % 2 == 1 else -4)))
            return h, s, l, cls._hsl_to_hex(h, s, l)

        # Fallback: distribuição áurea de matizes
        hue_deg = int(round(((fallback_index * 0.618033988749895) % 1.0) * 360)) % 360
        sat_pct = 72
        light_pct = 48
        return hue_deg, sat_pct, light_pct, cls._hsl_to_hex(hue_deg, sat_pct, light_pct)

    @staticmethod
    def _attach_margin_data_defined_color(
        symbol,
        hue_deg: int,
        sat_pct: int,
        base_light_pct: int,
        base_hex: str,
        max_margin: float = 35.0,
    ) -> None:
        """
        Aplica Data-Defined FillColor escalando a luminosidade HSL pela margem de vitória
        (DIFF_1_2_PERC):
          - Margem ~ 0 p.p. (vitória apertada) -> tom claro pastel (Lightness = 82%)
          - Margem >= 35 p.p. (vitória esmagadora) -> tom escuro intenso (Lightness = 26%)
          - Na legenda do QGIS (onde DIFF_1_2_PERC é NULL) -> exibe a cor base vibrante do partido.
        """
        if symbol.symbolLayerCount() == 0:
            return
        prop_enum = getattr(
            getattr(QgsSymbolLayer, "Property", QgsSymbolLayer),
            "FillColor",
            getattr(QgsSymbolLayer, "PropertyFillColor", None),
        )
        if prop_enum is None:
            return

        expr = (
            f'CASE WHEN "DIFF_1_2_PERC" IS NULL THEN \'{base_hex}\' '
            f"ELSE color_hsl({int(hue_deg)}, {int(sat_pct)}, "
            f'clamp(26, to_int(scale_linear("DIFF_1_2_PERC", 0, {max_margin}, 82, 26)), 82)) END'
        )
        symbol.symbolLayer(0).setDataDefinedProperty(
            prop_enum, QgsProperty.fromExpression(expr)
        )

    @classmethod
    def apply_winner_symbology(
        cls,
        layer: QgsVectorLayer,
        field_name: str = "VENCEDOR",
        result: Optional[TseAggregationResult] = None,
        graduated_margin: bool = True,
    ) -> None:
        """
        Aplica simbologia categorizada baseada na coluna VENCEDOR:
          - graduated_margin=False ("Dado Bruto"): cor sólida oficial do partido/candidato.
          - graduated_margin=True ("Graduado por Margem"): mesma cor-base por candidato, mas
            variando de claro (vitória apertada) a escuro (vitória folgada) via Data-Defined Color
            sobre a coluna DIFF_1_2_PERC.
          - Inclui categoria residual ("Outros candidatos vencedores") para que nenhum município
            ou local de votação suma do mapa em eleições proporcionais fragmentadas.
        """
        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            return

        has_diff_field = layer.fields().indexFromName("DIFF_1_2_PERC") >= 0
        use_graduated = bool(graduated_margin and has_diff_field)
        unit_lbl = (
            "locais"
            if (result and result.aggregation_level == "local_votacao")
            else "mun."
        )

        if result is not None and result.pivot_summary:
            items = [
                (
                    p_key,
                    f"{p_key} ({int(round(tot)):,} votos | 1º em {wins} {unit_lbl})".replace(",", "."),
                )
                for p_key, tot, wins in result.pivot_summary
            ]
        else:
            unique_vals = sorted(
                str(v) for v in layer.uniqueValues(idx) if v is not None and str(v).strip()
            )
            items = [(val, val) for val in unique_vals]

        if not items:
            return

        # Ajusta escala de margem máxima conforme o tipo de eleição (proporcional tem margens menores)
        max_margin = 35.0
        if result is not None and result.records and has_diff_field:
            diffs = sorted(
                float(r.get("DIFF_1_2_PERC") or 0.0)
                for r in result.records
                if r.get("DIFF_1_2_PERC") is not None
            )
            if diffs:
                p90 = diffs[min(len(diffs) - 1, int(len(diffs) * 0.90))]
                max_margin = max(10.0, min(45.0, round(p90, 1)))

        categories: List[QgsRendererCategory] = []
        is_point = layer.geometryType() == QgsWkbTypes.GeometryType.PointGeometry
        party_occurrence: Dict[str, int] = {}

        for i, (val, label) in enumerate(items):
            hue_deg, sat_pct, light_pct, color_hex = cls._resolve_candidate_hsl(
                val, i, party_occurrence
            )

            if is_point:
                symbol = QgsMarkerSymbol.createSimple(
                    {
                        "name": "circle",
                        "color": color_hex,
                        "size": "3.0",
                        "outline_color": "#222222",
                        "outline_width": "0.25",
                    }
                )
            else:
                symbol = QgsFillSymbol.createSimple(
                    {
                        "color": color_hex,
                        "outline_color": "#444444",
                        "outline_width": "0.18",
                    }
                )

            if use_graduated:
                cls._attach_margin_data_defined_color(
                    symbol, hue_deg, sat_pct, light_pct, color_hex, max_margin=max_margin
                )

            categories.append(QgsRendererCategory(val, symbol, label))

        # Categoria catch-all ("Outros candidatos vencedores") para não ocultar feições fora do Top N
        other_wins = result.other_winners_count if result is not None else 0
        known_keys = {k for k, _ in items}
        actual_other_vals = [
            str(v)
            for v in layer.uniqueValues(idx)
            if v is not None and str(v).strip() and str(v) not in known_keys
        ]
        if other_wins > 0 or actual_other_vals:
            other_hex = "#8c96a6"
            other_lbl = (
                f"Outros candidatos vencedores ({other_wins} {unit_lbl})"
                if other_wins > 0
                else "Outros candidatos vencedores"
            )
            if is_point:
                other_sym = QgsMarkerSymbol.createSimple(
                    {
                        "name": "circle",
                        "color": other_hex,
                        "size": "2.6",
                        "outline_color": "#333333",
                        "outline_width": "0.2",
                    }
                )
            else:
                other_sym = QgsFillSymbol.createSimple(
                    {
                        "color": other_hex,
                        "outline_color": "#555555",
                        "outline_width": "0.18",
                    }
                )
            if use_graduated:
                cls._attach_margin_data_defined_color(
                    other_sym, 215, 16, 55, other_hex, max_margin=max_margin
                )
            categories.append(QgsRendererCategory("", other_sym, other_lbl))

        renderer = QgsCategorizedSymbolRenderer(field_name, categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    @classmethod
    def apply_candidate_graduated_symbology(
        cls,
        layer: QgsVectorLayer,
        result: Optional[TseAggregationResult] = None,
        field_name: str = "",
    ) -> None:
        """
        Aplica simbologia graduada (5 faixas coropléticas / pontos graduados) sobre o percentual
        de votos do candidato principal (1º colocado estadual ou candidato filtrado pelo usuário).
        """
        target_field = field_name
        cand_label = ""
        if result is not None:
            if not target_field:
                target_field = result.primary_pct_field
            cand_label = result.primary_candidate_label

        if not target_field or layer.fields().indexFromName(target_field) < 0:
            # Fallback para PERC_VENCEDOR se não houver coluna PCT_ específica
            if layer.fields().indexFromName("PERC_VENCEDOR") >= 0:
                target_field = "PERC_VENCEDOR"
            else:
                return

        idx = layer.fields().indexFromName(target_field)
        vals: List[float] = []
        for feat in layer.getFeatures():
            v = feat[idx]
            if v is not None:
                try:
                    vals.append(float(v))
                except (ValueError, TypeError):
                    pass

        if not vals:
            return

        vals.sort()
        min_v = vals[0]
        max_v = vals[-1]
        if max_v <= min_v:
            max_v = min_v + 1.0

        # Escolhe a família de cor oficial do candidato/partido
        hue_deg, sat_pct, _, _ = cls._resolve_candidate_hsl(cand_label or target_field, 0, {})
        is_point = layer.geometryType() == QgsWkbTypes.GeometryType.PointGeometry
        is_pct = target_field.startswith(("PCT_", "PERC_"))
        suffix = "%" if is_pct else ""

        # Calcula 5 quebras por quantis suavizados
        num_classes = 5
        breaks = [min_v]
        for c_idx in range(1, num_classes):
            q_pos = int(round((c_idx / num_classes) * (len(vals) - 1)))
            b_val = round(vals[q_pos], 1 if is_pct else 0)
            if b_val <= breaks[-1]:
                b_val = round(breaks[-1] + (max_v - min_v) / num_classes, 2)
            breaks.append(min(max_v, b_val))
        breaks.append(max_v)

        light_steps = [86, 72, 57, 43, 28]
        ranges: List[QgsRendererRange] = []
        prefix_lbl = f"{cand_label}: " if cand_label else ""

        for i in range(num_classes):
            low = breaks[i]
            high = breaks[i + 1]
            if high < low:
                high = low
            c_hex = cls._hsl_to_hex(hue_deg, sat_pct, light_steps[i])
            if is_point:
                sz = str(round(2.4 + i * 0.45, 2))
                sym = QgsMarkerSymbol.createSimple(
                    {
                        "name": "circle",
                        "color": c_hex,
                        "size": sz,
                        "outline_color": "#222222",
                        "outline_width": "0.22",
                    }
                )
            else:
                sym = QgsFillSymbol.createSimple(
                    {
                        "color": c_hex,
                        "outline_color": "#444444",
                        "outline_width": "0.18",
                    }
                )
            lbl = f"{prefix_lbl}{low:.1f}{suffix} a {high:.1f}{suffix}".replace(".", ",")
            ranges.append(QgsRendererRange(low, high, sym, lbl))

        renderer = QgsGraduatedSymbolRenderer(target_field, ranges)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
