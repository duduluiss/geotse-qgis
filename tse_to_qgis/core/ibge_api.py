# -*- coding: utf-8 -*-
"""
Cliente para a API de Malhas v3 do IBGE (servicodados.ibge.gov.br/api/v3/malhas).
Realiza o download automático das geometrias municipais (GeoJSON) por UF ou Brasil
com descompressão GZIP transparente e cache em disco.
"""

import gzip
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional


VALID_BRAZIL_UFS = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO",
    "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR",
    "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
}


class IbgeMalhasClient:
    """Cliente com cache local para download de malhas municipais da API v3 do IBGE."""

    def __init__(self, cache_dir: Optional[str] = None):
        if cache_dir is None:
            cache_dir = os.path.join(tempfile.gettempdir(), "tse_geobrasil_ibge_cache")
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    @staticmethod
    def _fetch_json(url: str, timeout: int = 45) -> Dict:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "QGIS-TSE-GeoBrasil-Plugin/1.0",
                "Accept": "application/vnd.geo+json, application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(e.reason, ssl.SSLError):
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    raw = resp.read()
            else:
                raise
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

    def fetch_uf_geojson(self, uf: str, qualidade: str = "intermediaria") -> str:
        """
        Retorna o caminho de um arquivo GeoJSON no cache contendo os municípios da UF
        (ou de todo o Brasil se uf='BR').
        """
        uf_clean = uf.strip().upper()
        qual_clean = qualidade.strip().lower() if qualidade else "intermediaria"
        if qual_clean not in ("minima", "intermediaria", "maxima"):
            qual_clean = "intermediaria"

        cache_file = os.path.join(self.cache_dir, f"malha_mun_{uf_clean}_{qual_clean}.geojson")
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 500:
            return cache_file

        if uf_clean == "BR" or uf_clean not in VALID_BRAZIL_UFS:
            url = (
                f"https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
                f"?formato=application/vnd.geo+json&qualidade={qual_clean}&intrarregiao=municipio"
            )
        else:
            url = (
                f"https://servicodados.ibge.gov.br/api/v3/malhas/estados/{uf_clean}"
                f"?formato=application/vnd.geo+json&qualidade={qual_clean}&intrarregiao=municipio"
            )

        geojson_data = self._fetch_json(url)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(geojson_data, f, ensure_ascii=False)
        return cache_file

    def fetch_geojson_for_ufs(
        self,
        ufs: Iterable[str],
        qualidade: str = "intermediaria",
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> str:
        """
        Baixa a malha municipal do IBGE para uma ou mais UFs e retorna o caminho
        do arquivo GeoJSON consolidado.
        """
        valid_ufs = sorted({u.strip().upper() for u in ufs if u and u.strip().upper() in VALID_BRAZIL_UFS})
        if not valid_ufs or len(valid_ufs) > 6:
            if progress_callback:
                progress_callback(88, "Baixando malha municipal nacional (BR) na API do IBGE...")
            return self.fetch_uf_geojson("BR", qualidade)

        if len(valid_ufs) == 1:
            uf = valid_ufs[0]
            if progress_callback:
                progress_callback(88, f"Baixando malha municipal de {uf} na API do IBGE...")
            return self.fetch_uf_geojson(uf, qualidade)

        # Combina múltiplas UFs em um único GeoJSON
        combined_key = "_".join(valid_ufs)
        combined_path = os.path.join(self.cache_dir, f"malha_mun_{combined_key}_{qualidade}.geojson")
        if os.path.exists(combined_path) and os.path.getsize(combined_path) > 500:
            return combined_path

        all_features: List[Dict] = []
        for idx, uf in enumerate(valid_ufs):
            if progress_callback:
                progress_callback(
                    86 + int((idx / len(valid_ufs)) * 8),
                    f"Baixando malha municipal de {uf} na API do IBGE...",
                )
            uf_path = self.fetch_uf_geojson(uf, qualidade)
            with open(uf_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                all_features.extend(data.get("features", []))

        combined_geojson = {
            "type": "FeatureCollection",
            "features": all_features,
        }
        with open(combined_path, "w", encoding="utf-8") as f:
            json.dump(combined_geojson, f, ensure_ascii=False)
        return combined_path
