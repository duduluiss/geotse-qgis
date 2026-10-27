# -*- coding: utf-8 -*-
"""
Cliente inteligente com HTTP Range (Partial Content 206), fallback SSL para OSGeo4W/QGIS
e cache local para buscar automaticamente as coordenadas geográficas (NR_LATITUDE e NR_LONGITUDE),
bairro e CEP dos Locais de Votação diretamente na base oficial do TSE (cdn.tse.jus.br)
quando o usuário carrega arquivos como 'votacao_secao' ou 'bweb'.
"""

import csv
import io
import json
import os
import ssl
import tempfile
import urllib.request
import zipfile
from typing import Callable, Dict, Iterable, Optional

from .municipality_mapper import MunicipalityMapper


def _urlopen_safe(req: urllib.request.Request, timeout: int = 25):
    """Abre requisição HTTP com fallback para contexto SSL não verificado no OSGeo4W/QGIS."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except Exception:
        ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _clean_coord(val: str) -> Optional[float]:
    if not val:
        return None
    s = str(val).strip().strip('"').replace(",", ".")
    if not s or s in ("-1", "-1.0", "-3", "#NULO", "#NULO#", "#NE", "#NE#"):
        return None
    try:
        f = float(s)
        if f == -1.0 or f == 0.0 or abs(f) > 180.0:
            return None
        return f
    except ValueError:
        return None


class _HttpRangeFile(io.RawIOBase):
    """
    Wrapper HTTP Range (Status 206) que permite ao zipfile.ZipFile do Python
    abrir um arquivo .zip de 170 MB no servidor do TSE e baixar APENAS o CSV
    da UF desejada (~1 a 3 MB) em poucos segundos.
    """

    def __init__(self, url: str, timeout: int = 25):
        self.url = url
        self.timeout = timeout
        self.pos = 0
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0 (QGIS-GeoTSE-Plugin/1.0)"},
        )
        with _urlopen_safe(req, timeout=timeout) as r:
            self.size = int(r.headers["Content-Length"])
        self._tail_cache: Optional[bytes] = None
        self._tail_start = max(0, self.size - 131072)

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        return self.pos

    def readinto(self, b) -> int:
        n = len(b)
        if n == 0 or self.pos >= self.size:
            return 0
        end = min(self.size - 1, self.pos + n - 1)
        if self.pos >= self._tail_start:
            if self._tail_cache is None:
                req = urllib.request.Request(
                    self.url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (QGIS-GeoTSE-Plugin/1.0)",
                        "Range": f"bytes={self._tail_start}-{self.size - 1}",
                    },
                )
                with _urlopen_safe(req, timeout=self.timeout) as r:
                    self._tail_cache = r.read()
            rel = self.pos - self._tail_start
            chunk = self._tail_cache[rel : rel + (end - self.pos + 1)]
        else:
            req = urllib.request.Request(
                self.url,
                headers={
                    "User-Agent": "Mozilla/5.0 (QGIS-GeoTSE-Plugin/1.0)",
                    "Range": f"bytes={self.pos}-{end}",
                },
            )
            with _urlopen_safe(req, timeout=self.timeout) as r:
                chunk = r.read()
        b[: len(chunk)] = chunk
        self.pos += len(chunk)
        return len(chunk)


class TseLocaisCoordClient:
    """
    Obtém e armazena em cache local as coordenadas (LATITUDE, LONGITUDE) de todos
    os locais de votação (CD_MUNICIPIO_TSE + NR_ZONA + NR_LOCAL_VOTACAO) por UF e Ano.
    """

    def __init__(self, cache_dir: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.embedded_dir = os.path.join(base_dir, "data", "locais_cache")
        os.makedirs(self.embedded_dir, exist_ok=True)

        if cache_dir is None:
            cache_dir = os.path.join(tempfile.gettempdir(), "geotse_locais_cache")
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.mapper = MunicipalityMapper.instance()

    def _get_uf_coords(
        self,
        uf: str,
        ano: str = "2026",
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Dict[str, object]]:
        uf_clean = (uf or "").strip().upper()
        ano_clean = (ano or "2026").strip()
        if not ano_clean.isdigit():
            ano_clean = "2026"

        # 1. Verifica cache embutido na pasta do plugin ou na pasta temp
        for folder in (self.embedded_dir, self.cache_dir):
            for y_try in (ano_clean, "2026", "2024"):
                cand_file = os.path.join(folder, f"locais_{y_try}_{uf_clean}.json")
                if os.path.exists(cand_file) and os.path.getsize(cand_file) > 200:
                    try:
                        with open(cand_file, "r", encoding="utf-8") as f:
                            return json.load(f)
                    except Exception:
                        pass

        candidate_years = [ano_clean]
        for y in ["2026", "2024", "2022", "2020"]:
            if y not in candidate_years:
                candidate_years.append(y)

        coords_map: Dict[str, Dict[str, object]] = {}
        mun_lat_sum: Dict[str, float] = {}
        mun_lon_sum: Dict[str, float] = {}
        mun_count: Dict[str, int] = {}

        for y in candidate_years:
            url = (
                f"https://cdn.tse.jus.br/estatistica/sead/odsele/"
                f"eleitorado_locais_votacao/eleitorado_local_votacao_{y}.zip"
            )
            try:
                if progress_callback:
                    progress_callback(
                        82,
                        f"Buscando coordenadas dos Locais de Votação ({uf_clean}/{y}) no TSE...",
                    )
                range_stream = io.BufferedReader(_HttpRangeFile(url), buffer_size=524288)
                with zipfile.ZipFile(range_stream) as zf:
                    target_members = [
                        m
                        for m in zf.namelist()
                        if m.lower().endswith(".csv") and f"_{uf_clean.lower()}" in m.lower()
                    ]
                    if not target_members:
                        target_members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
                    if not target_members:
                        continue

                    member = target_members[0]
                    with zf.open(member, "r") as raw_f:
                        text_f = io.TextIOWrapper(raw_f, encoding="iso-8859-1", errors="replace")
                        reader = csv.reader(text_f, delimiter=";", quotechar='"')
                        headers = [h.strip().strip('"').upper() for h in next(reader)]
                        col_idx = {h: i for i, h in enumerate(headers)}

                        idx_mun = col_idx.get("CD_MUNICIPIO", -1)
                        idx_zona = col_idx.get("NR_ZONA", -1)
                        idx_local = col_idx.get("NR_LOCAL_VOTACAO", -1)
                        idx_nm_local = col_idx.get("NM_LOCAL_VOTACAO", -1)
                        idx_end = col_idx.get("DS_ENDERECO", -1)
                        idx_bairro = col_idx.get("NM_BAIRRO", -1)
                        idx_cep = col_idx.get("NR_CEP", -1)
                        idx_lat = col_idx.get("NR_LATITUDE", -1)
                        idx_lon = col_idx.get("NR_LONGITUDE", -1)

                        for row in reader:
                            if idx_mun < 0 or idx_local < 0 or idx_lat < 0 or idx_lon < 0:
                                break
                            cd_tse = self.mapper.format_tse_code(row[idx_mun])
                            zona = (
                                str(int(row[idx_zona]))
                                if row[idx_zona].strip().isdigit()
                                else row[idx_zona].strip()
                            )
                            local = (
                                str(int(row[idx_local]))
                                if row[idx_local].strip().isdigit()
                                else row[idx_local].strip()
                            )
                            lat = _clean_coord(row[idx_lat])
                            lon = _clean_coord(row[idx_lon])

                            key = f"{cd_tse}:{zona}:{local}"
                            if key not in coords_map or (
                                coords_map[key].get("lat") is None and lat is not None
                            ):
                                coords_map[key] = {
                                    "lat": lat,
                                    "lon": lon,
                                    "nome": row[idx_nm_local].strip() if 0 <= idx_nm_local < len(row) else "",
                                    "endereco": row[idx_end].strip() if 0 <= idx_end < len(row) else "",
                                    "bairro": row[idx_bairro].strip() if 0 <= idx_bairro < len(row) else "",
                                    "cep": row[idx_cep].strip() if 0 <= idx_cep < len(row) else "",
                                }
                            if lat is not None and lon is not None and cd_tse:
                                mun_lat_sum[cd_tse] = mun_lat_sum.get(cd_tse, 0.0) + lat
                                mun_lon_sum[cd_tse] = mun_lon_sum.get(cd_tse, 0.0) + lon
                                mun_count[cd_tse] = mun_count.get(cd_tse, 0) + 1
                if coords_map:
                    break
            except Exception:
                continue

        for cd_tse, cnt in mun_count.items():
            if cnt > 0:
                coords_map[f"MUN:{cd_tse}"] = {
                    "lat": round(mun_lat_sum[cd_tse] / cnt, 6),
                    "lon": round(mun_lon_sum[cd_tse] / cnt, 6),
                }

        if coords_map:
            for folder in (self.embedded_dir, self.cache_dir):
                try:
                    out_f = os.path.join(folder, f"locais_{ano_clean}_{uf_clean}.json")
                    with open(out_f, "w", encoding="utf-8") as f:
                        json.dump(coords_map, f, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    pass

        return coords_map

    def load_coords_for_ufs(
        self,
        ufs: Iterable[str],
        ano: str = "2026",
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Dict[str, object]]:
        merged: Dict[str, Dict[str, object]] = {}
        for uf in sorted({u.strip().upper() for u in ufs if u and u.strip()}):
            if uf == "ZZ":
                continue
            uf_map = self._get_uf_coords(uf, ano=ano, progress_callback=progress_callback)
            merged.update(uf_map)
        return merged
