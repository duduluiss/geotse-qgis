# -*- coding: utf-8 -*-
"""
Tradutor oficial de códigos municipais TSE (5 dígitos) <-> IBGE (7 dígitos).
Possui fallback automático por (SG_UF, NM_MUNICIPIO normalizado) para cobrir
100% dos 5.571 municípios brasileiros (incluindo emancipações recentes).
"""

import json
import os
import unicodedata
from typing import Dict, Optional, Tuple


def normalize_text(text: str) -> str:
    """Remove acentos, converte para maiúsculas e padroniza espaços."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_str = nfkd.encode("ASCII", "ignore").decode("ASCII")
    return " ".join(ascii_str.upper().strip().split())


class MunicipalityMapper:
    """Mapeador em memória entre códigos TSE (5 dígitos) e IBGE (7 dígitos)."""

    _instance = None

    def __init__(self, json_path: Optional[str] = None):
        if json_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            json_path = os.path.join(base_dir, "data", "tse_ibge_municipios.json")

        self.by_tse: Dict[str, Dict[str, str]] = {}
        self.by_uf_name: Dict[str, Dict[str, str]] = {}
        self.by_ibge: Dict[str, Dict[str, str]] = {}
        self._load(json_path)

    @classmethod
    def instance(cls) -> "MunicipalityMapper":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self, json_path: str) -> None:
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"Tabela de municípios TSE-IBGE não encontrada em: {json_path}")

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.by_tse = data.get("by_tse", {})
        self.by_uf_name = data.get("by_uf_name", {})

        for tse_code, info in self.by_tse.items():
            ibge_code = info.get("ibge", "")
            if ibge_code:
                self.by_ibge[ibge_code] = {
                    "tse": tse_code,
                    "ibge": ibge_code,
                    "uf": info.get("uf", ""),
                    "nome": info.get("nome", ""),
                }

    @staticmethod
    def format_tse_code(raw_code) -> str:
        """Padroniza o código TSE para 5 dígitos com zero à esquerda (ex.: '01120')."""
        if raw_code is None:
            return ""
        s = str(raw_code).strip().strip('"').strip("'")
        if not s:
            return ""
        if s.endswith(".0"):
            s = s[:-2]
        if s.isdigit():
            return s.zfill(5)
        return s

    @staticmethod
    def format_ibge_code(raw_code) -> str:
        """Padroniza o código IBGE para 7 dígitos (ex.: '1200013')."""
        if raw_code is None:
            return ""
        s = str(raw_code).strip().strip('"').strip("'")
        if not s:
            return ""
        if s.endswith(".0"):
            s = s[:-2]
        if s.isdigit():
            return s.zfill(7)
        return s

    def resolve(
        self,
        cd_municipio_tse=None,
        sg_uf: Optional[str] = None,
        nm_municipio: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """
        Retorna a tupla (cd_ibge_7, cd_tse_5, sg_uf, nm_municipio_oficial).
        Se não encontrar correspondência (ex.: Exterior 'ZZ' ou Voto em Trânsito),
        retorna ('', cd_tse_5, sg_uf_limpo, nm_municipio_limpo).
        """
        tse_code = self.format_tse_code(cd_municipio_tse)
        uf_clean = str(sg_uf or "").strip().upper()
        nm_clean = str(nm_municipio or "").strip()

        # Caso 1: Código já tem 7 dígitos e é um código IBGE válido
        if len(tse_code) == 7 and tse_code in self.by_ibge:
            info = self.by_ibge[tse_code]
            return (
                info["ibge"],
                info.get("tse", tse_code),
                info.get("uf", uf_clean),
                info.get("nome", nm_clean),
            )

        # Caso 2: Busca direta pelo código TSE de 5 dígitos
        if tse_code and tse_code in self.by_tse:
            info = self.by_tse[tse_code]
            return (
                info["ibge"],
                tse_code,
                info.get("uf") or uf_clean,
                info.get("nome") or nm_clean,
            )

        # Caso 3: Fallback por (SG_UF, NM_MUNICIPIO normalizado)
        if uf_clean and nm_clean:
            key = f"{uf_clean}:{normalize_text(nm_clean)}"
            if key in self.by_uf_name:
                info = self.by_uf_name[key]
                return (
                    info["ibge"],
                    tse_code or info.get("tse", ""),
                    info.get("uf") or uf_clean,
                    info.get("nome") or nm_clean,
                )

        # Caso especial: Distrito Federal / Brasília (qualquer zona do DF se UF='DF')
        if uf_clean == "DF":
            return ("5300108", tse_code or "97012", "DF", "Brasília")

        return ("", tse_code, uf_clean, nm_clean)
