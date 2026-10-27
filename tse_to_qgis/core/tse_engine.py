# -*- coding: utf-8 -*-
"""
Motor de leitura, detecção automática de layout, filtragem e agregação (pivot)
para TODOS os arquivos de dados abertos do TSE (.csv, .txt e .zip),
incluindo Eleições Gerais (Presidente, Governador, Senador, Deputados) e
Eleições Municipais (Prefeito, Vice-Prefeito e Vereador).

Inclui:
  - Separação automática de Votos de Legenda (2 dígitos) em cargos proporcionais
  - Agrupamento nominal por candidato (CANDIDATO_COMPOSTO) ou por Partido/Legenda (PARTIDO_COMPOSTO)
  - Filtro por UF, Município, Cargo e Turno
  - Busca automática de coordenadas de Locais de Votação no CDN do TSE
"""

import csv
import io
import os
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .municipality_mapper import MunicipalityMapper, normalize_text
from .tse_locais_client import TseLocaisCoordClient


# Valores nulos padrão utilizados pelo TSE nos arquivos CSV
TSE_NULL_VALUES = {"#NULO", "#NULO#", "#NE", "#NE#", "-1", "-3", ""}

# Códigos de votáveis especiais no TSE (Branco, Nulo, Anulado)
TSE_BLANK_CODES = {"95"}
TSE_NULL_VOTE_CODES = {"96", "97", "98"}

# Cargos proporcionais onde votáveis de 2 dígitos representam Voto de Legenda
PROPORTIONAL_CARGOS = {
    "DEPUTADO FEDERAL",
    "DEPUTADO ESTADUAL",
    "DEPUTADO DISTRITAL",
    "VEREADOR",
}

# Mapa oficial de códigos de cargo do TSE (CD_CARGO -> DS_CARGO)
TSE_CARGO_CODES = {
    "1": "PRESIDENTE",
    "2": "VICE-PRESIDENTE",
    "3": "GOVERNADOR",
    "4": "VICE-GOVERNADOR",
    "5": "SENADOR",
    "6": "DEPUTADO FEDERAL",
    "7": "DEPUTADO ESTADUAL",
    "8": "DEPUTADO DISTRITAL",
    "9": "1º SUPLENTE",
    "10": "2º SUPLENTE",
    "11": "PREFEITO",
    "12": "VICE-PREFEITO",
    "13": "VEREADOR",
}

# Ordem hierárquica dos cargos para organização das camadas no QGIS
CARGO_PRIORITY = {
    "PRESIDENTE": 1,
    "GOVERNADOR": 2,
    "SENADOR": 3,
    "DEPUTADO FEDERAL": 4,
    "DEPUTADO ESTADUAL": 5,
    "DEPUTADO DISTRITAL": 6,
    "PREFEITO": 7,
    "VEREADOR": 8,
}

# Dicionário de Partidos pelo número TSE (2 dígitos) -> (Sigla, Hue 0-360, Sat 0-100, Lightness 0-100)
TSE_PARTY_INFO: Dict[str, Tuple[str, int, int, int]] = {
    "10": ("REPUBLICANOS", 232, 76, 48),
    "11": ("PP", 202, 78, 48),
    "12": ("PDT", 342, 78, 48),
    "13": ("PT", 0, 84, 46),          # Vermelho
    "14": ("PTB", 38, 80, 46),
    "15": ("MDB", 138, 74, 40),       # Verde
    "16": ("PSTU", 355, 85, 38),
    "18": ("REDE", 166, 75, 42),
    "19": ("PODE", 88, 68, 44),
    "20": ("PODE", 88, 68, 44),
    "21": ("PCB", 358, 80, 40),
    "22": ("PL", 216, 86, 45),        # Azul
    "23": ("CIDADANIA", 320, 74, 48),
    "25": ("PRD", 46, 82, 46),
    "27": ("DC", 210, 65, 46),
    "28": ("PRTB", 150, 68, 42),
    "29": ("PCO", 352, 85, 36),
    "30": ("NOVO", 24, 92, 50),       # Laranja
    "33": ("MOBILIZA", 265, 65, 48),
    "35": ("PMB", 310, 65, 48),
    "36": ("AGIR", 190, 70, 45),
    "40": ("PSB", 30, 88, 48),        # Laranja/Amarelo
    "43": ("PV", 122, 72, 38),        # Verde Floresta
    "44": ("UNIAO", 195, 84, 45),     # Azul Ciano
    "45": ("PSDB", 224, 80, 46),      # Azul Tucano
    "50": ("PSOL", 282, 74, 46),      # Roxo
    "51": ("PATRIOTA", 145, 70, 40),
    "55": ("PSD", 182, 78, 40),       # Turquesa
    "65": ("PCdoB", 348, 82, 44),
    "70": ("AVANTE", 15, 82, 48),
    "77": ("SOLIDARIEDADE", 34, 86, 48),
    "80": ("UP", 356, 78, 34),
    "90": ("PROS", 208, 70, 46),
}


def resolve_party_label(nr_code: str, sg_partido: str = "") -> str:
    """Retorna rótulo padronizado do partido (ex.: '22_PL', '13_PT', '15_MDB')."""
    nr_clean = str(nr_code or "").strip()
    sg_clean = str(sg_partido or "").strip().upper()
    prefix2 = nr_clean[:2] if len(nr_clean) >= 2 and nr_clean[:2].isdigit() else ""
    if prefix2:
        if not sg_clean or sg_clean in TSE_NULL_VALUES:
            info = TSE_PARTY_INFO.get(prefix2)
            sg_clean = info[0] if info else f"PART_{prefix2}"
        return f"{prefix2}_{sg_clean}"
    if sg_clean and sg_clean not in TSE_NULL_VALUES:
        return sg_clean
    return "OUTROS"


def normalize_cargo_label(raw_cargo: str) -> str:
    """Padroniza o nome do cargo (traduzindo CD_CARGO numérico caso DS_CARGO não exista)."""
    val = str(raw_cargo or "").strip().strip('"')
    if not val or val.upper() in TSE_NULL_VALUES:
        return ""
    if val.isdigit() and val in TSE_CARGO_CODES:
        return TSE_CARGO_CODES[val]
    return val.upper()


def cargo_sort_key(group_tuple: Tuple[str, str]) -> Tuple[int, str, str]:
    """Ordena grupos de (cargo, turno) na hierarquia eleitoral."""
    cargo, turno = group_tuple
    prio = CARGO_PRIORITY.get(cargo.upper(), 99)
    return (prio, cargo.upper(), str(turno))


def format_group_title(cargo: str, turno: str) -> str:
    """Formata o nome do subgrupo/camada a partir de Cargo e Turno."""
    c_str = cargo.strip() if cargo else ""
    t_str = turno.strip() if turno else ""
    if c_str and t_str:
        return f"{c_str} - {t_str}º Turno"
    elif c_str:
        return c_str
    elif t_str:
        return f"{t_str}º Turno"
    return "Geral"


def parse_tse_float(val: Any) -> float:
    """Converte strings numéricas do TSE (ex.: '1234,56' ou '1.234,56') em float."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().strip('"').strip("'")
    if not s or s.upper() in TSE_NULL_VALUES:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_tse_coord(val: Any) -> Optional[float]:
    """Converte latitude/longitude do TSE (onde -1 significa sem coordenada)."""
    if val is None:
        return None
    s = str(val).strip().strip('"').strip("'")
    if not s or s.upper() in TSE_NULL_VALUES:
        return None
    s = s.replace(",", ".")
    try:
        num = float(s)
        if num == -1.0 or num == 0.0 or abs(num) > 180.0:
            return None
        return num
    except ValueError:
        return None


def make_safe_slug(text: str, max_len: int = 28) -> str:
    """Gera sufixo seguro para nomes de colunas (ASCII, sem espaços ou símbolos)."""
    norm = normalize_text(text)
    slug = re.sub(r"[^A-Z0-9]+", "_", norm).strip("_")
    if not slug:
        slug = "OUTROS"
    return slug[:max_len].strip("_")


@dataclass
class TseFileMetadata:
    """Metadados extraídos na inspeção rápida de um arquivo do TSE."""
    file_path: str
    zip_member: Optional[str] = None
    zip_csv_members: List[str] = field(default_factory=list)
    encoding: str = "iso-8859-1"
    delimiter: str = ";"
    headers: List[str] = field(default_factory=list)
    profile: str = "generico_tse"
    profile_description: str = "Tabela Genérica do TSE"
    mun_code_col: str = ""
    mun_name_col: str = ""
    uf_col: str = ""
    cargo_col: str = ""
    turno_col: str = ""
    default_pivot_col: str = ""
    candidate_pivot_cols: List[str] = field(default_factory=list)
    default_value_col: str = ""
    candidate_value_cols: List[str] = field(default_factory=list)
    has_coordinates: bool = False
    lat_col: str = ""
    lon_col: str = ""
    available_ufs: List[str] = field(default_factory=list)
    available_municipios: List[str] = field(default_factory=list)
    available_cargos: List[str] = field(default_factory=list)
    available_turnos: List[str] = field(default_factory=list)


@dataclass
class TseProcessingConfig:
    """Configuração de execução para o motor de agregação do TSE."""
    file_path: str
    zip_member: Optional[str] = None
    profile: str = "auto"
    mun_code_col: str = ""
    mun_name_col: str = ""
    uf_col: str = ""
    filter_uf: str = ""          # Vazio ou "TODAS" = sem filtro de UF
    filter_municipio: str = ""   # Vazio ou "TODOS" = todos os municípios
    filter_cargo: str = ""       # Vazio ou "TODOS" = separa em camadas por cargo!
    filter_turno: str = ""       # Vazio ou "TODOS" = separa por turno se houver >1!
    aggregation_level: str = "municipio"  # "municipio" ou "local_votacao"
    enable_pivot: bool = True
    pivot_col: str = ""          # CANDIDATO_COMPOSTO, PARTIDO_COMPOSTO, AUTO_CARGO, SG_PARTIDO, etc.
    value_col: str = ""          # Coluna numérica para somar, ou "__COUNT__" para contagem de linhas
    top_n_pivots: int = 15       # Máximo de colunas pivotadas (0 = todas)
    specific_pivot_values: List[str] = field(default_factory=list)  # Filtro opcional de candidatos/partidos
    include_percentages: bool = True
    include_winner: bool = True
    exclude_exterior_zz: bool = True
    split_layers_by_cargo: bool = True
    separate_party_legend_votes: bool = True  # Separa votos de legenda (2 dígitos) em cargos proporcionais


@dataclass
class TseAggregationResult:
    """Resultado consolidado pronto para construir a camada espacial no QGIS."""
    aggregation_level: str  # "municipio" ou "local_votacao"
    profile: str
    field_specs: List[Tuple[str, str]]  # Lista de (nome_coluna, tipo: 'str' | 'int' | 'float')
    records: List[Dict[str, Any]]
    total_rows_read: int = 0
    matched_rows: int = 0
    unmatched_tse_codes: Set[str] = field(default_factory=set)
    detected_ufs: Set[str] = field(default_factory=set)
    group_label: str = ""
    # Lista de (chave_candidato, total_votos_estado, qtd_vitorias_1_lugar)
    pivot_summary: List[Tuple[str, float, int]] = field(default_factory=list)
    # Quantidade de municípios/locais vencidos por candidatos fora do Top N
    other_winners_count: int = 0
    # Primeiro campo de percentual (PCT_...) para modo de simbologia graduada de candidato único
    primary_pct_field: str = ""
    primary_candidate_label: str = ""


class _MunicipalityBucket:
    """Acumulador interno por (Cargo, Turno) para leitura única do CSV."""
    def __init__(self):
        self.mun_meta: Dict[str, Tuple[str, str, str, str]] = {}
        self.mun_total_geral: Dict[str, float] = defaultdict(float)
        self.mun_total_validos: Dict[str, float] = defaultdict(float)
        self.mun_brancos: Dict[str, float] = defaultdict(float)
        self.mun_nulos: Dict[str, float] = defaultdict(float)
        self.mun_legenda: Dict[str, float] = defaultdict(float)
        self.mun_pivot_values: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.global_pivot_totals: Dict[str, float] = defaultdict(float)
        self.matched_rows: int = 0
        self.detected_ufs: Set[str] = set()


class _DetalheBucket:
    """Acumulador interno por (Cargo, Turno) para arquivos detalhe_votacao."""
    def __init__(self):
        self.mun_meta: Dict[str, Tuple[str, str, str, str]] = {}
        self.mun_sums: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.mun_zones: Dict[str, Set[str]] = defaultdict(set)
        self.matched_rows: int = 0
        self.detected_ufs: Set[str] = set()


class _LocalVotacaoBucket:
    """Acumulador interno por (Cargo, Turno) para votação por Local de Votação."""
    def __init__(self):
        # Chave: (cd_ibge, nr_zona, nr_local)
        self.local_meta: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.local_secoes: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)
        self.local_total_geral: Dict[Tuple[str, str, str], float] = defaultdict(float)
        self.local_total_validos: Dict[Tuple[str, str, str], float] = defaultdict(float)
        self.local_brancos: Dict[Tuple[str, str, str], float] = defaultdict(float)
        self.local_nulos: Dict[Tuple[str, str, str], float] = defaultdict(float)
        self.local_legenda: Dict[Tuple[str, str, str], float] = defaultdict(float)
        self.local_pivot_values: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.global_pivot_totals: Dict[str, float] = defaultdict(float)
        self.matched_rows: int = 0
        self.detected_ufs: Set[str] = set()


class TseEngine:
    """Motor de inspeção e processamento em streaming de arquivos do TSE."""

    def __init__(self, mapper: Optional[MunicipalityMapper] = None):
        self.mapper = mapper or MunicipalityMapper.instance()

    @staticmethod
    def list_zip_csv_members(zip_path: str) -> List[str]:
        """Lista todos os arquivos .csv ou .txt dentro de um pacote .zip do TSE."""
        members = []
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                lower = name.lower()
                if (lower.endswith(".csv") or lower.endswith(".txt")) and not lower.endswith("/"):
                    members.append(name)
        members.sort(key=lambda x: (0 if "_br" not in x.lower() else 1, x.lower()))
        return members

    @staticmethod
    def _open_text_stream(
        file_path: str,
        zip_member: Optional[str] = None,
        encoding: str = "iso-8859-1",
    ):
        """Abre um stream de texto a partir de um arquivo CSV/TXT ou membro de um ZIP."""
        if file_path.lower().endswith(".zip"):
            zf = zipfile.ZipFile(file_path, "r")
            if not zip_member:
                members = TseEngine.list_zip_csv_members(file_path)
                if not members:
                    zf.close()
                    raise ValueError("O arquivo ZIP informado não contém nenhum arquivo .csv ou .txt.")
                zip_member = members[0]
            raw_stream = zf.open(zip_member, "r")
            text_stream = io.TextIOWrapper(raw_stream, encoding=encoding, errors="replace", newline="")
            return text_stream, zf
        else:
            f = open(file_path, "r", encoding=encoding, errors="replace", newline="")
            return f, None

    @staticmethod
    def _detect_encoding_and_delimiter(
        file_path: str, zip_member: Optional[str] = None
    ) -> Tuple[str, str]:
        """Detecta automaticamente codificação (UTF-8 vs Latin-1) e separador (; vs ,)."""
        raw_bytes = b""
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, "r") as zf:
                if not zip_member:
                    members = TseEngine.list_zip_csv_members(file_path)
                    if not members:
                        raise ValueError("Nenhum CSV encontrado no arquivo ZIP.")
                    zip_member = members[0]
                with zf.open(zip_member, "r") as member_f:
                    raw_bytes = member_f.read(8192)
        else:
            with open(file_path, "rb") as f:
                raw_bytes = f.read(8192)

        encoding = "iso-8859-1"
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            try:
                raw_bytes.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                encoding = "iso-8859-1"

        first_line = raw_bytes.splitlines()[0].decode(encoding, errors="replace") if raw_bytes else ""
        if first_line.count(";") >= first_line.count(","):
            delimiter = ";"
        else:
            delimiter = ","
        return encoding, delimiter

    @staticmethod
    def _detect_profile_and_columns(headers: List[str]) -> Dict[str, Any]:
        """
        Analisa o cabeçalho do CSV e identifica a família do arquivo TSE e suas colunas-chave.
        """
        h_set = set(headers)

        mun_code_candidates = [
            "CD_MUNICIPIO",
            "SG_UE",
            "COD_MUNICIPIO_TSE",
            "CD_MUN_TSE",
            "COD_MUN_TSE",
            "CD_MUNICIPIO_TSE",
            "CD_MUN_IBGE",
            "CD_MUN",
        ]
        mun_code_col = next((c for c in mun_code_candidates if c in h_set), "")

        mun_name_candidates = ["NM_MUNICIPIO", "NM_UE", "NOME_MUNICIPIO", "NM_MUN"]
        mun_name_col = next((c for c in mun_name_candidates if c in h_set), "")

        uf_candidates = ["SG_UF", "UF"]
        uf_col = next((c for c in uf_candidates if c in h_set), "")

        cargo_col = "DS_CARGO" if "DS_CARGO" in h_set else ("CD_CARGO" if "CD_CARGO" in h_set else "")
        turno_col = "NR_TURNO" if "NR_TURNO" in h_set else ""

        lat_col = next((c for c in ["NR_LATITUDE", "LATITUDE", "LAT"] if c in h_set), "")
        lon_col = next((c for c in ["NR_LONGITUDE", "LONGITUDE", "LON", "LONG"] if c in h_set), "")
        has_coordinates = bool(lat_col and lon_col)

        profile = "generico_tse"
        profile_desc = "Tabela Genérica do TSE"
        default_pivot_col = ""
        default_value_col = ""

        if ("NR_CANDIDATO" in h_set or "NR_VOTAVEL" in h_set) and any(
            c in h_set for c in ["QT_VOTOS_NOMINAIS_VALIDOS", "QT_VOTOS_NOMINAIS", "QT_VOTOS"]
        ):
            profile = "votacao_candidato"
            profile_desc = "Votação Nominal por Candidato / Votável (MunZona ou Seção)"
            default_pivot_col = "CANDIDATO_COMPOSTO"
            for vc in ["QT_VOTOS_NOMINAIS_VALIDOS", "QT_VOTOS_NOMINAIS", "QT_VOTOS"]:
                if vc in h_set:
                    default_value_col = vc
                    break

        elif "SG_PARTIDO" in h_set and any(
            c in h_set
            for c in [
                "QT_TOTAL_VOTOS_LEG_VALIDOS",
                "QT_VOTOS_LEGENDA",
                "QT_VOTOS_NOMINAIS_VALIDOS",
            ]
        ):
            profile = "votacao_partido"
            profile_desc = "Votação por Partido / Coligação (MunZona)"
            default_pivot_col = "PARTIDO_COMPOSTO"
            for vc in [
                "QT_VOTOS_NOMINAIS_VALIDOS",
                "QT_TOTAL_VOTOS_LEG_VALIDOS",
                "QT_VOTOS_LEGENDA",
                "QT_VOTOS_NOMINAIS",
            ]:
                if vc in h_set:
                    default_value_col = vc
                    break

        elif "QT_APTOS" in h_set and "QT_COMPARECIMENTO" in h_set and "QT_ABSTENCOES" in h_set:
            if any(
                c in h_set
                for c in ["DS_GENERO", "DS_FAIXA_ETARIA", "DS_GRAU_ESCOLARIDADE", "DS_ESTADO_CIVIL"]
            ):
                profile = "perfil_eleitorado"
                profile_desc = "Perfil de Comparecimento e Abstenção do Eleitorado"
                default_pivot_col = next(
                    (
                        c
                        for c in [
                            "DS_GENERO",
                            "DS_FAIXA_ETARIA",
                            "DS_GRAU_ESCOLARIDADE",
                            "DS_RACA_COR",
                            "DS_ESTADO_CIVIL",
                        ]
                        if c in h_set
                    ),
                    "",
                )
                default_value_col = "QT_APTOS"
            else:
                profile = "detalhe_votacao"
                profile_desc = "Detalhe da Apuração (Comparecimento, Abstenção, Brancos e Nulos)"
                default_pivot_col = ""
                default_value_col = "QT_APTOS"

        elif has_coordinates and ("NR_LOCAL_VOTACAO" in h_set or "NM_LOCAL_VOTACAO" in h_set or "LOCAL" in h_set):
            profile = "locais_votacao"
            profile_desc = "Locais de Votação / Eleitorado por Local (Com Coordenadas)"
            default_pivot_col = ""
            default_value_col = next(
                (c for c in ["QT_ELEITOR_SECAO", "QT_ELEITOR", "QT_APTOS", "ELEITORES"] if c in h_set),
                "__COUNT__",
            )

        elif "QT_ELEITORES_PERFIL" in h_set:
            profile = "perfil_eleitorado"
            profile_desc = "Perfil do Eleitorado (Gênero, Faixa Etária, Escolaridade, Raça/Cor)"
            default_pivot_col = next(
                (
                    c
                    for c in [
                        "DS_GENERO",
                        "DS_GRAU_ESCOLARIDADE",
                        "DS_FAIXA_ETARIA",
                        "DS_RACA_COR",
                        "DS_ESTADO_CIVIL",
                    ]
                    if c in h_set
                ),
                "",
            )
            default_value_col = "QT_ELEITORES_PERFIL"

        elif any(
            c in h_set
            for c in ["VR_BEM_CANDIDATO", "VR_RECEITA", "VR_DESPESA_CONTRATADA", "SQ_CANDIDATO", "QT_VAGA"]
        ):
            profile = "candidatos_contas"
            if "VR_BEM_CANDIDATO" in h_set:
                profile_desc = "Bens de Candidatos Declarados"
                default_value_col = "VR_BEM_CANDIDATO"
                default_pivot_col = "DS_TIPO_BEM_CANDIDATO" if "DS_TIPO_BEM_CANDIDATO" in h_set else ""
            elif "VR_RECEITA" in h_set:
                profile_desc = "Prestação de Contas - Receitas de Campanha"
                default_value_col = "VR_RECEITA"
                default_pivot_col = next(
                    (c for c in ["SG_PARTIDO", "DS_ORIGEM_RECEITA", "DS_ESPECIE_RECEITA"] if c in h_set),
                    "",
                )
            elif "VR_DESPESA_CONTRATADA" in h_set:
                profile_desc = "Prestação de Contas - Despesas Contratadas"
                default_value_col = "VR_DESPESA_CONTRATADA"
                default_pivot_col = next(
                    (c for c in ["SG_PARTIDO", "DS_ORIGEM_DESPESA"] if c in h_set),
                    "",
                )
            elif "QT_VAGA" in h_set:
                profile_desc = "Vagas em Disputa por Município"
                default_value_col = "QT_VAGA"
                default_pivot_col = "DS_CARGO" if "DS_CARGO" in h_set else ""
            else:
                profile_desc = "Consulta de Candidaturas (Candidatos)"
                default_value_col = "__COUNT__"
                default_pivot_col = next(
                    (
                        c
                        for c in [
                            "SG_PARTIDO",
                            "DS_GENERO",
                            "DS_COR_RACA",
                            "DS_GRAU_INSTRUCAO",
                            "DS_SITUACAO_CANDIDATURA",
                        ]
                        if c in h_set
                    ),
                    "",
                )

        candidate_pivot_cols = []
        if profile in ("votacao_candidato", "votacao_partido"):
            candidate_pivot_cols.extend(["CANDIDATO_COMPOSTO", "PARTIDO_COMPOSTO", "AUTO_CARGO"])
        preferred_pivots = [
            "SG_PARTIDO",
            "NM_URNA_CANDIDATO",
            "NM_VOTAVEL",
            "NR_CANDIDATO",
            "NR_VOTAVEL",
            "NM_CANDIDATO",
            "DS_GENERO",
            "DS_FAIXA_ETARIA",
            "DS_GRAU_ESCOLARIDADE",
            "DS_GRAU_INSTRUCAO",
            "DS_COR_RACA",
            "DS_RACA_COR",
            "DS_ESTADO_CIVIL",
            "DS_SITUACAO_CANDIDATURA",
            "DS_ORIGEM_RECEITA",
            "DS_TIPO_BEM_CANDIDATO",
            "DS_CARGO",
        ]
        for col in preferred_pivots:
            if col in h_set and col not in candidate_pivot_cols:
                candidate_pivot_cols.append(col)
        for col in headers:
            if col not in candidate_pivot_cols and not col.startswith(("DT_", "HH_", "CD_", "SQ_")):
                candidate_pivot_cols.append(col)

        candidate_value_cols = []
        for col in headers:
            if col.startswith(("QT_", "VR_")) or any(
                k in col for k in ["VOTOS", "ELEITOR", "APTOS", "VALOR", "RECEITA", "DESPESA"]
            ):
                candidate_value_cols.append(col)
        candidate_value_cols.append("__COUNT__")
        if not default_value_col:
            default_value_col = candidate_value_cols[0]

        return {
            "profile": profile,
            "profile_description": profile_desc,
            "mun_code_col": mun_code_col,
            "mun_name_col": mun_name_col,
            "uf_col": uf_col,
            "cargo_col": cargo_col,
            "turno_col": turno_col,
            "default_pivot_col": default_pivot_col,
            "candidate_pivot_cols": candidate_pivot_cols,
            "default_value_col": default_value_col,
            "candidate_value_cols": candidate_value_cols,
            "has_coordinates": has_coordinates,
            "lat_col": lat_col,
            "lon_col": lon_col,
        }

    def inspect_file(
        self,
        file_path: str,
        zip_member: Optional[str] = None,
        max_sample_rows: int = 25000,
    ) -> TseFileMetadata:
        """
        Inspeciona o arquivo TSE para detectar colunas, tipo de tabela, UFs,
        municípios, cargos e turnos.
        """
        zip_members = []
        if file_path.lower().endswith(".zip"):
            zip_members = self.list_zip_csv_members(file_path)
            if not zip_member and zip_members:
                zip_member = zip_members[0]

        encoding, delimiter = self._detect_encoding_and_delimiter(file_path, zip_member)
        stream, zf = self._open_text_stream(file_path, zip_member, encoding)
        try:
            reader = csv.reader(stream, delimiter=delimiter, quotechar='"')
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise ValueError("O arquivo selecionado está vazio.")

            headers = [h.strip().strip('"').upper() for h in raw_headers]
            info = self._detect_profile_and_columns(headers)

            col_idx = {h: idx for idx, h in enumerate(headers)}
            uf_idx = col_idx.get(info["uf_col"], -1)
            mun_name_idx = col_idx.get(info["mun_name_col"], -1)
            cargo_idx = col_idx.get(info["cargo_col"], -1)
            turno_idx = col_idx.get(info["turno_col"], -1)

            ufs: Set[str] = set()
            municipios: Set[str] = set()
            cargos: Set[str] = set()
            turnos: Set[str] = set()

            for i, row in enumerate(reader):
                if i >= max_sample_rows:
                    break
                if 0 <= uf_idx < len(row):
                    val = row[uf_idx].strip()
                    if val and val.upper() not in TSE_NULL_VALUES:
                        ufs.add(val.upper())
                if 0 <= mun_name_idx < len(row):
                    m_val = row[mun_name_idx].strip()
                    if m_val and m_val.upper() not in TSE_NULL_VALUES:
                        municipios.add(m_val.upper())
                if 0 <= cargo_idx < len(row):
                    c_val = normalize_cargo_label(row[cargo_idx])
                    if c_val:
                        cargos.add(c_val)
                if 0 <= turno_idx < len(row):
                    val = row[turno_idx].strip()
                    if val and val.upper() not in TSE_NULL_VALUES:
                        turnos.add(val)

            # Complementa com todos os municípios oficiais das UFs detectadas (se <= 3 UFs)
            if 1 <= len(ufs) <= 3:
                for m_info in self.mapper.by_tse.values():
                    if m_info.get("uf") in ufs and m_info.get("nome"):
                        municipios.add(m_info["nome"].upper())

            # Remove duplicatas normalizadas mantendo a grafia oficial com acentos quando disponível
            norm_mun_map: Dict[str, str] = {}
            for m_str in sorted(municipios):
                n_key = normalize_text(m_str)
                if n_key:
                    norm_mun_map[n_key] = m_str
            sorted_municipios = [norm_mun_map[k] for k in sorted(norm_mun_map.keys())]
            sorted_cargos = sorted(cargos, key=lambda c: (CARGO_PRIORITY.get(c.upper(), 99), c))

            return TseFileMetadata(
                file_path=file_path,
                zip_member=zip_member,
                zip_csv_members=zip_members,
                encoding=encoding,
                delimiter=delimiter,
                headers=headers,
                profile=info["profile"],
                profile_description=info["profile_description"],
                mun_code_col=info["mun_code_col"],
                mun_name_col=info["mun_name_col"],
                uf_col=info["uf_col"],
                cargo_col=info["cargo_col"],
                turno_col=info["turno_col"],
                default_pivot_col=info["default_pivot_col"],
                candidate_pivot_cols=info["candidate_pivot_cols"],
                default_value_col=info["default_value_col"],
                candidate_value_cols=info["candidate_value_cols"],
                has_coordinates=info["has_coordinates"],
                lat_col=info["lat_col"],
                lon_col=info["lon_col"],
                available_ufs=sorted(ufs),
                available_municipios=sorted_municipios,
                available_cargos=sorted_cargos,
                available_turnos=sorted(turnos),
            )
        finally:
            stream.close()
            if zf is not None:
                zf.close()

    def process(
        self,
        config: TseProcessingConfig,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> TseAggregationResult:
        grouped = self.process_by_groups(config, progress_callback)
        if not grouped:
            return TseAggregationResult(
                aggregation_level=config.aggregation_level,
                profile=config.profile,
                field_specs=[],
                records=[],
            )
        return grouped[0][1]

    def process_by_groups(
        self,
        config: TseProcessingConfig,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> List[Tuple[str, TseAggregationResult]]:
        meta = self.inspect_file(config.file_path, config.zip_member, max_sample_rows=500)
        profile = config.profile if config.profile and config.profile != "auto" else meta.profile

        mun_code_col = config.mun_code_col or meta.mun_code_col
        mun_name_col = config.mun_name_col or meta.mun_name_col
        uf_col = config.uf_col or meta.uf_col

        if config.aggregation_level == "local_votacao":
            if profile == "locais_votacao" or (meta.has_coordinates and profile != "votacao_candidato"):
                res = self._process_locais_votacao(config, meta, progress_callback)
                res.group_label = "Locais de Votação"
                return [("Locais de Votação", res)]
            return self._process_votacao_por_local_grouped(
                config, meta, profile, mun_code_col, mun_name_col, uf_col, progress_callback
            )

        if profile == "detalhe_votacao" and not config.enable_pivot:
            return self._process_detalhe_votacao_grouped(config, meta, progress_callback)

        return self._process_municipality_aggregation_grouped(
            config, meta, profile, mun_code_col, mun_name_col, uf_col, progress_callback
        )

    @staticmethod
    def _extract_pivot_key(
        row: List[str],
        effective_pivot: str,
        nr_code: str,
        nm_urna_idx: int,
        sg_part_idx: int,
        pivot_idx: int,
    ) -> str:
        """Extrai o identificador de coluna/categoria (candidato ou partido)."""
        if effective_pivot == "PARTIDO_COMPOSTO":
            sg_p = row[sg_part_idx].strip() if 0 <= sg_part_idx < len(row) else ""
            return resolve_party_label(nr_code, sg_p)
        elif effective_pivot == "CANDIDATO_COMPOSTO":
            nm_u = row[nm_urna_idx].strip() if 0 <= nm_urna_idx < len(row) else ""
            sg_p = row[sg_part_idx].strip() if 0 <= sg_part_idx < len(row) else ""
            if nr_code and nm_u:
                return f"{nr_code}_{nm_u}"
            elif nr_code and sg_p:
                return f"{nr_code}_{sg_p}"
            return nr_code or nm_u or "OUTROS"
        elif 0 <= pivot_idx < len(row):
            p_key = row[pivot_idx].strip()
            if not p_key or p_key.upper() in TSE_NULL_VALUES:
                return "NAO_INFORMADO"
            return p_key
        return "TOTAL"

    def _process_detalhe_votacao_grouped(
        self,
        config: TseProcessingConfig,
        meta: TseFileMetadata,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> List[Tuple[str, TseAggregationResult]]:
        stream, zf = self._open_text_stream(config.file_path, meta.zip_member, meta.encoding)
        try:
            reader = csv.reader(stream, delimiter=meta.delimiter, quotechar='"')
            headers = [h.strip().strip('"').upper() for h in next(reader)]
            col_idx = {h: i for i, h in enumerate(headers)}

            mun_code_idx = col_idx.get(config.mun_code_col or meta.mun_code_col, -1)
            mun_name_idx = col_idx.get(config.mun_name_col or meta.mun_name_col, -1)
            uf_idx = col_idx.get(config.uf_col or meta.uf_col, -1)
            cargo_idx = col_idx.get(meta.cargo_col, -1)
            turno_idx = col_idx.get(meta.turno_col, -1)
            zona_idx = col_idx.get("NR_ZONA", -1)

            sum_cols = [
                c
                for c in [
                    "QT_APTOS",
                    "QT_COMPARECIMENTO",
                    "QT_ABSTENCOES",
                    "QT_VOTOS_VALIDOS",
                    "QT_VOTOS_NOMINAIS",
                    "QT_VOTOS_LEGENDA",
                    "QT_VOTOS_BRANCOS",
                    "QT_VOTOS_NULOS",
                ]
                if c in col_idx
            ]
            sum_indices = [(c, col_idx[c]) for c in sum_cols]

            filter_uf = (config.filter_uf or "").strip().upper()
            if filter_uf in ("TODAS", "TODOS", "ALL"):
                filter_uf = ""
            filter_mun_norm = normalize_text(config.filter_municipio)
            if filter_mun_norm in ("TODOS", "TODAS", "ALL"):
                filter_mun_norm = ""
            filter_cargo = normalize_cargo_label(config.filter_cargo)
            if filter_cargo in ("TODAS", "TODOS", "ALL"):
                filter_cargo = ""
            filter_turno = (config.filter_turno or "").strip()
            if filter_turno.upper() in ("TODAS", "TODOS", "ALL"):
                filter_turno = ""

            buckets: Dict[Tuple[str, str], _DetalheBucket] = defaultdict(_DetalheBucket)
            total_rows = 0
            unmatched_codes: Set[str] = set()

            for row in reader:
                total_rows += 1
                if progress_callback and total_rows % 50000 == 0:
                    progress_callback(min(85, total_rows // 10000), f"Lendo linha {total_rows:,}...")

                uf_val = row[uf_idx].strip().upper() if 0 <= uf_idx < len(row) else ""
                if config.exclude_exterior_zz and uf_val == "ZZ":
                    continue
                if filter_uf and uf_val != filter_uf:
                    continue

                row_cargo = normalize_cargo_label(row[cargo_idx]) if 0 <= cargo_idx < len(row) else ""
                if filter_cargo and row_cargo != filter_cargo:
                    continue

                row_turno = row[turno_idx].strip() if 0 <= turno_idx < len(row) else ""
                if filter_turno and row_turno != filter_turno:
                    continue

                raw_tse = row[mun_code_idx] if 0 <= mun_code_idx < len(row) else ""
                raw_name = row[mun_name_idx] if 0 <= mun_name_idx < len(row) else ""
                cd_ibge, cd_tse, uf_res, nm_res = self.mapper.resolve(raw_tse, uf_val, raw_name)
                if not cd_ibge:
                    if cd_tse:
                        unmatched_codes.add(cd_tse)
                    continue
                if filter_mun_norm and normalize_text(nm_res) != filter_mun_norm and normalize_text(raw_name) != filter_mun_norm:
                    continue

                g_key = (
                    row_cargo if config.split_layers_by_cargo else (filter_cargo or ""),
                    row_turno if config.split_layers_by_cargo else (filter_turno or ""),
                )
                b = buckets[g_key]
                b.matched_rows += 1
                if uf_res:
                    b.detected_ufs.add(uf_res)
                b.mun_meta[cd_ibge] = (cd_ibge, cd_tse, uf_res, nm_res)

                if 0 <= zona_idx < len(row):
                    b.mun_zones[cd_ibge].add(row[zona_idx].strip())

                sums = b.mun_sums[cd_ibge]
                for col_name, idx in sum_indices:
                    if idx < len(row):
                        sums[col_name] += int(parse_tse_float(row[idx]))

            field_specs: List[Tuple[str, str]] = [
                ("CD_MUN_IBGE", "str"),
                ("CD_MUN_TSE", "str"),
                ("SG_UF", "str"),
                ("NM_MUN", "str"),
                ("QT_ZONAS", "int"),
            ]
            for c in sum_cols:
                field_specs.append((c, "int"))
            if "QT_APTOS" in sum_cols and "QT_COMPARECIMENTO" in sum_cols:
                field_specs.append(("PCT_COMPARECIMENTO", "float"))
            if "QT_APTOS" in sum_cols and "QT_ABSTENCOES" in sum_cols:
                field_specs.append(("PCT_ABSTENCAO", "float"))
            if "QT_COMPARECIMENTO" in sum_cols and "QT_VOTOS_VALIDOS" in sum_cols:
                field_specs.append(("PCT_VOTOS_VALIDOS", "float"))
            if "QT_COMPARECIMENTO" in sum_cols and "QT_VOTOS_BRANCOS" in sum_cols:
                field_specs.append(("PCT_VOTOS_BRANCOS", "float"))
            if "QT_COMPARECIMENTO" in sum_cols and "QT_VOTOS_NULOS" in sum_cols:
                field_specs.append(("PCT_VOTOS_NULOS", "float"))

            results: List[Tuple[str, TseAggregationResult]] = []
            for g_key in sorted(buckets.keys(), key=cargo_sort_key):
                b = buckets[g_key]
                group_title = format_group_title(g_key[0], g_key[1])
                records: List[Dict[str, Any]] = []
                for cd_ibge, (ibge, tse, uf, nome) in sorted(b.mun_meta.items()):
                    sums = b.mun_sums[cd_ibge]
                    rec: Dict[str, Any] = {
                        "CD_MUN_IBGE": ibge,
                        "CD_MUN_TSE": tse,
                        "SG_UF": uf,
                        "NM_MUN": nome,
                        "QT_ZONAS": len(b.mun_zones[cd_ibge]),
                    }
                    for c in sum_cols:
                        rec[c] = sums.get(c, 0)

                    aptos = sums.get("QT_APTOS", 0)
                    comp = sums.get("QT_COMPARECIMENTO", 0)
                    if "QT_APTOS" in sum_cols and "QT_COMPARECIMENTO" in sum_cols:
                        rec["PCT_COMPARECIMENTO"] = round((comp / aptos) * 100.0, 2) if aptos > 0 else 0.0
                    if "QT_APTOS" in sum_cols and "QT_ABSTENCOES" in sum_cols:
                        rec["PCT_ABSTENCAO"] = round((sums.get("QT_ABSTENCOES", 0) / aptos) * 100.0, 2) if aptos > 0 else 0.0
                    if "QT_COMPARECIMENTO" in sum_cols and "QT_VOTOS_VALIDOS" in sum_cols:
                        rec["PCT_VOTOS_VALIDOS"] = round((sums.get("QT_VOTOS_VALIDOS", 0) / comp) * 100.0, 2) if comp > 0 else 0.0
                    if "QT_COMPARECIMENTO" in sum_cols and "QT_VOTOS_BRANCOS" in sum_cols:
                        rec["PCT_VOTOS_BRANCOS"] = round((sums.get("QT_VOTOS_BRANCOS", 0) / comp) * 100.0, 2) if comp > 0 else 0.0
                    if "QT_COMPARECIMENTO" in sum_cols and "QT_VOTOS_NULOS" in sum_cols:
                        rec["PCT_VOTOS_NULOS"] = round((sums.get("QT_VOTOS_NULOS", 0) / comp) * 100.0, 2) if comp > 0 else 0.0
                    records.append(rec)

                results.append(
                    (
                        group_title,
                        TseAggregationResult(
                            aggregation_level="municipio",
                            profile="detalhe_votacao",
                            field_specs=list(field_specs),
                            records=records,
                            total_rows_read=total_rows,
                            matched_rows=b.matched_rows,
                            unmatched_tse_codes=unmatched_codes,
                            detected_ufs=b.detected_ufs,
                            group_label=group_title,
                            primary_pct_field="PCT_COMPARECIMENTO" if "QT_COMPARECIMENTO" in sum_cols else "",
                        ),
                    )
                )

            return results
        finally:
            stream.close()
            if zf is not None:
                zf.close()

    def _process_locais_votacao(
        self,
        config: TseProcessingConfig,
        meta: TseFileMetadata,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> TseAggregationResult:
        stream, zf = self._open_text_stream(config.file_path, meta.zip_member, meta.encoding)
        try:
            reader = csv.reader(stream, delimiter=meta.delimiter, quotechar='"')
            headers = [h.strip().strip('"').upper() for h in next(reader)]
            col_idx = {h: i for i, h in enumerate(headers)}

            mun_code_idx = col_idx.get(config.mun_code_col or meta.mun_code_col, -1)
            mun_name_idx = col_idx.get(config.mun_name_col or meta.mun_name_col, -1)
            uf_idx = col_idx.get(config.uf_col or meta.uf_col, -1)
            zona_idx = col_idx.get("NR_ZONA", col_idx.get("ZONA", -1))
            local_num_idx = col_idx.get("NR_LOCAL_VOTACAO", col_idx.get("Nº LOCAL TSE", -1))
            local_name_idx = col_idx.get("NM_LOCAL_VOTACAO", col_idx.get("LOCAL", -1))
            endereco_idx = col_idx.get("DS_ENDERECO", col_idx.get("ENDEREÇO", -1))
            bairro_idx = col_idx.get("NM_BAIRRO", col_idx.get("BAIRRO", -1))
            cep_idx = col_idx.get("NR_CEP", col_idx.get("CEP", -1))
            secao_idx = col_idx.get("NR_SECAO", -1)
            lat_idx = col_idx.get(meta.lat_col, -1)
            lon_idx = col_idx.get(meta.lon_col, -1)

            val_col = config.value_col or meta.default_value_col
            val_idx = col_idx.get(val_col, -1) if val_col != "__COUNT__" else -1

            filter_uf = (config.filter_uf or "").strip().upper()
            if filter_uf in ("TODAS", "TODOS", "ALL"):
                filter_uf = ""
            filter_mun_norm = normalize_text(config.filter_municipio)
            if filter_mun_norm in ("TODOS", "TODAS", "ALL"):
                filter_mun_norm = ""
            filter_turno = (config.filter_turno or "").strip()
            if filter_turno.upper() in ("TODAS", "TODOS", "ALL"):
                filter_turno = ""
            turno_idx = col_idx.get(meta.turno_col, -1)

            locais: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            total_rows = 0
            matched_rows = 0
            unmatched_codes: Set[str] = set()
            detected_ufs: Set[str] = set()

            for row in reader:
                total_rows += 1
                if progress_callback and total_rows % 50000 == 0:
                    progress_callback(min(85, total_rows // 10000), f"Lendo linha {total_rows:,}...")

                uf_val = row[uf_idx].strip().upper() if 0 <= uf_idx < len(row) else ""
                if config.exclude_exterior_zz and uf_val == "ZZ":
                    continue
                if filter_uf and uf_val != filter_uf:
                    continue
                if filter_turno and 0 <= turno_idx < len(row):
                    if row[turno_idx].strip() != filter_turno:
                        continue

                raw_tse = row[mun_code_idx] if 0 <= mun_code_idx < len(row) else ""
                raw_name = row[mun_name_idx] if 0 <= mun_name_idx < len(row) else ""
                cd_ibge, cd_tse, uf_res, nm_res = self.mapper.resolve(raw_tse, uf_val, raw_name)
                if not cd_ibge and not (0 <= lat_idx < len(row)):
                    if cd_tse:
                        unmatched_codes.add(cd_tse)
                    continue
                if filter_mun_norm and normalize_text(nm_res) != filter_mun_norm and normalize_text(raw_name) != filter_mun_norm:
                    continue

                lat = parse_tse_coord(row[lat_idx]) if 0 <= lat_idx < len(row) else None
                lon = parse_tse_coord(row[lon_idx]) if 0 <= lon_idx < len(row) else None

                nr_zona = row[zona_idx].strip() if 0 <= zona_idx < len(row) else ""
                nr_local = row[local_num_idx].strip() if 0 <= local_num_idx < len(row) else str(total_rows)
                key = (cd_ibge or "0000000", nr_zona, nr_local)

                matched_rows += 1
                if uf_res:
                    detected_ufs.add(uf_res)

                if key not in locais:
                    locais[key] = {
                        "CD_MUN_IBGE": cd_ibge,
                        "CD_MUN_TSE": cd_tse,
                        "SG_UF": uf_res,
                        "NM_MUN": nm_res,
                        "NR_ZONA": nr_zona,
                        "NR_LOCAL_VOTACAO": nr_local,
                        "NM_LOCAL_VOTACAO": row[local_name_idx].strip() if 0 <= local_name_idx < len(row) else "",
                        "DS_ENDERECO": row[endereco_idx].strip() if 0 <= endereco_idx < len(row) else "",
                        "NM_BAIRRO": row[bairro_idx].strip() if 0 <= bairro_idx < len(row) else "",
                        "NR_CEP": row[cep_idx].strip() if 0 <= cep_idx < len(row) else "",
                        "LATITUDE": lat,
                        "LONGITUDE": lon,
                        "_secoes": set(),
                        "QT_ELEITORES": 0,
                    }
                else:
                    if locais[key]["LATITUDE"] is None and lat is not None:
                        locais[key]["LATITUDE"] = lat
                        locais[key]["LONGITUDE"] = lon

                if 0 <= secao_idx < len(row):
                    locais[key]["_secoes"].add(row[secao_idx].strip())

                if 0 <= val_idx < len(row):
                    locais[key]["QT_ELEITORES"] += int(parse_tse_float(row[val_idx]))
                else:
                    locais[key]["QT_ELEITORES"] += 1

            field_specs = [
                ("CD_MUN_IBGE", "str"),
                ("CD_MUN_TSE", "str"),
                ("SG_UF", "str"),
                ("NM_MUN", "str"),
                ("NR_ZONA", "str"),
                ("NR_LOCAL_VOTACAO", "str"),
                ("NM_LOCAL_VOTACAO", "str"),
                ("DS_ENDERECO", "str"),
                ("NM_BAIRRO", "str"),
                ("NR_CEP", "str"),
                ("QT_SECOES", "int"),
                ("QT_ELEITORES", "int"),
                ("LATITUDE", "float"),
                ("LONGITUDE", "float"),
            ]

            records = []
            for key in sorted(locais.keys()):
                item = locais[key]
                secoes = item.pop("_secoes")
                item["QT_SECOES"] = len(secoes) if secoes else 1
                records.append(item)

            return TseAggregationResult(
                aggregation_level="local_votacao",
                profile="locais_votacao",
                field_specs=field_specs,
                records=records,
                total_rows_read=total_rows,
                matched_rows=matched_rows,
                unmatched_tse_codes=unmatched_codes,
                detected_ufs=detected_ufs,
                primary_pct_field="QT_ELEITORES",
            )
        finally:
            stream.close()
            if zf is not None:
                zf.close()

    def _process_municipality_aggregation_grouped(
        self,
        config: TseProcessingConfig,
        meta: TseFileMetadata,
        profile: str,
        mun_code_col: str,
        mun_name_col: str,
        uf_col: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> List[Tuple[str, TseAggregationResult]]:
        stream, zf = self._open_text_stream(config.file_path, meta.zip_member, meta.encoding)
        try:
            reader = csv.reader(stream, delimiter=meta.delimiter, quotechar='"')
            headers = [h.strip().strip('"').upper() for h in next(reader)]
            col_idx = {h: i for i, h in enumerate(headers)}

            mun_code_idx = col_idx.get(mun_code_col, -1)
            mun_name_idx = col_idx.get(mun_name_col, -1)
            uf_idx = col_idx.get(uf_col, -1)
            cargo_idx = col_idx.get(meta.cargo_col, -1)
            turno_idx = col_idx.get(meta.turno_col, -1)

            need_candidates = config.enable_pivot or config.include_winner
            pivot_col = (config.pivot_col or meta.default_pivot_col) if need_candidates else ""

            val_col = config.value_col or meta.default_value_col or "__COUNT__"
            val_idx = col_idx.get(val_col, -1) if val_col != "__COUNT__" else -1
            is_float_metric = val_col.startswith("VR_")

            nr_cand_idx = col_idx.get("NR_CANDIDATO", col_idx.get("NR_VOTAVEL", -1))
            nm_urna_idx = col_idx.get(
                "NM_URNA_CANDIDATO", col_idx.get("NM_VOTAVEL", col_idx.get("NM_CANDIDATO", -1))
            )
            sg_part_idx = col_idx.get("SG_PARTIDO", -1)
            pivot_idx = (
                col_idx.get(pivot_col, -1)
                if pivot_col not in ("CANDIDATO_COMPOSTO", "PARTIDO_COMPOSTO", "AUTO_CARGO")
                else -1
            )

            filter_uf = (config.filter_uf or "").strip().upper()
            if filter_uf in ("TODAS", "TODOS", "ALL"):
                filter_uf = ""
            filter_mun_norm = normalize_text(config.filter_municipio)
            if filter_mun_norm in ("TODOS", "TODAS", "ALL"):
                filter_mun_norm = ""
            filter_cargo = normalize_cargo_label(config.filter_cargo)
            if filter_cargo in ("TODAS", "TODOS", "ALL"):
                filter_cargo = ""
            filter_turno = (config.filter_turno or "").strip()
            if filter_turno.upper() in ("TODAS", "TODOS", "ALL"):
                filter_turno = ""

            specific_filter_set = {
                normalize_text(v) for v in config.specific_pivot_values if v.strip()
            }

            buckets: Dict[Tuple[str, str], _MunicipalityBucket] = defaultdict(_MunicipalityBucket)
            total_rows = 0
            unmatched_codes: Set[str] = set()

            for row in reader:
                total_rows += 1
                if progress_callback and total_rows % 50000 == 0:
                    progress_callback(min(85, total_rows // 10000), f"Processando linha {total_rows:,}...")

                uf_val = row[uf_idx].strip().upper() if 0 <= uf_idx < len(row) else ""
                if config.exclude_exterior_zz and uf_val == "ZZ":
                    continue
                if filter_uf and uf_val != filter_uf:
                    continue

                row_cargo = normalize_cargo_label(row[cargo_idx]) if 0 <= cargo_idx < len(row) else ""
                if filter_cargo and row_cargo != filter_cargo:
                    continue

                row_turno = row[turno_idx].strip() if 0 <= turno_idx < len(row) else ""
                if filter_turno and row_turno != filter_turno:
                    continue

                raw_tse = row[mun_code_idx] if 0 <= mun_code_idx < len(row) else ""
                raw_name = row[mun_name_idx] if 0 <= mun_name_idx < len(row) else ""
                cd_ibge, cd_tse, uf_res, nm_res = self.mapper.resolve(raw_tse, uf_val, raw_name)
                if not cd_ibge:
                    if cd_tse:
                        unmatched_codes.add(cd_tse)
                    continue
                if filter_mun_norm and normalize_text(nm_res) != filter_mun_norm and normalize_text(raw_name) != filter_mun_norm:
                    continue

                if val_col == "__COUNT__":
                    metric_val = 1.0
                elif 0 <= val_idx < len(row):
                    metric_val = parse_tse_float(row[val_idx])
                else:
                    metric_val = 0.0

                g_key = (
                    row_cargo if config.split_layers_by_cargo else (filter_cargo or ""),
                    row_turno if config.split_layers_by_cargo else (filter_turno or ""),
                )
                b = buckets[g_key]
                b.matched_rows += 1
                if uf_res:
                    b.detected_ufs.add(uf_res)
                b.mun_meta[cd_ibge] = (cd_ibge, cd_tse, uf_res, nm_res)
                b.mun_total_geral[cd_ibge] += metric_val

                nr_code = row[nr_cand_idx].strip() if 0 <= nr_cand_idx < len(row) else ""
                if profile == "votacao_candidato" and nr_code in TSE_BLANK_CODES:
                    b.mun_brancos[cd_ibge] += metric_val
                    continue
                if profile == "votacao_candidato" and nr_code in TSE_NULL_VOTE_CODES:
                    b.mun_nulos[cd_ibge] += metric_val
                    continue

                b.mun_total_validos[cd_ibge] += metric_val

                # Determina o modo de pivot efetivo para este cargo (AUTO_CARGO usa Partido nos proporcionais)
                effective_pivot = pivot_col
                if pivot_col == "AUTO_CARGO":
                    effective_pivot = (
                        "PARTIDO_COMPOSTO" if row_cargo in PROPORTIONAL_CARGOS else "CANDIDATO_COMPOSTO"
                    )

                # Separação de Voto de Legenda (2 dígitos) em cargos proporcionais
                is_legend_vote = (
                    profile == "votacao_candidato"
                    and row_cargo in PROPORTIONAL_CARGOS
                    and len(nr_code) == 2
                    and nr_code.isdigit()
                )
                if is_legend_vote:
                    b.mun_legenda[cd_ibge] += metric_val
                    if config.separate_party_legend_votes and effective_pivot not in (
                        "PARTIDO_COMPOSTO",
                        "SG_PARTIDO",
                    ):
                        continue

                if need_candidates and effective_pivot:
                    p_key = self._extract_pivot_key(
                        row, effective_pivot, nr_code, nm_urna_idx, sg_part_idx, pivot_idx
                    )
                    if specific_filter_set:
                        norm_p = normalize_text(p_key)
                        if not any(f in norm_p for f in specific_filter_set):
                            continue

                    b.mun_pivot_values[cd_ibge][p_key] += metric_val
                    b.global_pivot_totals[p_key] += metric_val

            val_type = "float" if is_float_metric else "int"
            prefix_abs = "VOT" if profile in ("votacao_candidato", "votacao_partido") else "QT"
            if is_float_metric:
                prefix_abs = "VR"

            results: List[Tuple[str, TseAggregationResult]] = []
            for g_key in sorted(buckets.keys(), key=cargo_sort_key):
                b = buckets[g_key]
                group_title = format_group_title(g_key[0], g_key[1])

                sorted_pivots = sorted(
                    b.global_pivot_totals.keys(),
                    key=lambda k: (-b.global_pivot_totals[k], k),
                )
                if config.top_n_pivots and config.top_n_pivots > 0:
                    selected_pivots = sorted_pivots[: config.top_n_pivots]
                else:
                    selected_pivots = sorted_pivots

                pivot_col_map: List[Tuple[str, str, str]] = []
                used_col_names: Set[str] = {
                    "CD_MUN_IBGE",
                    "CD_MUN_TSE",
                    "SG_UF",
                    "NM_MUN",
                    "TOTAL_GERAL",
                    "TOTAL_VALIDOS",
                    "VOTOS_BRANCOS",
                    "VOTOS_NULOS",
                    "VOTOS_LEGENDA",
                    "VENCEDOR",
                    "VOTOS_VENCEDOR",
                    "PERC_VENCEDOR",
                    "SEGUNDO_LUGAR",
                    "VOTOS_SEGUNDO",
                    "PERC_SEGUNDO",
                    "TERCEIRO_LUGAR",
                    "DIFF_1_2_PERC",
                }

                for p_key in selected_pivots:
                    slug = make_safe_slug(p_key, max_len=24)
                    abs_col = f"{prefix_abs}_{slug}"
                    pct_col = f"PCT_{slug}"
                    counter = 2
                    while abs_col in used_col_names or pct_col in used_col_names:
                        abs_col = f"{prefix_abs}_{slug}_{counter}"
                        pct_col = f"PCT_{slug}_{counter}"
                        counter += 1
                    used_col_names.add(abs_col)
                    used_col_names.add(pct_col)
                    pivot_col_map.append((p_key, abs_col, pct_col))

                field_specs: List[Tuple[str, str]] = [
                    ("CD_MUN_IBGE", "str"),
                    ("CD_MUN_TSE", "str"),
                    ("SG_UF", "str"),
                    ("NM_MUN", "str"),
                    ("TOTAL_GERAL", val_type),
                    ("TOTAL_VALIDOS", val_type),
                ]
                has_blank_null = any(b.mun_brancos.values()) or any(b.mun_nulos.values())
                has_legenda = any(b.mun_legenda.values())
                if has_legenda:
                    field_specs.append(("VOTOS_LEGENDA", val_type))
                if has_blank_null:
                    field_specs.append(("VOTOS_BRANCOS", val_type))
                    field_specs.append(("VOTOS_NULOS", val_type))

                if config.include_winner and selected_pivots:
                    field_specs.extend(
                        [
                            ("VENCEDOR", "str"),
                            ("VOTOS_VENCEDOR", val_type),
                            ("PERC_VENCEDOR", "float"),
                            ("SEGUNDO_LUGAR", "str"),
                            ("VOTOS_SEGUNDO", val_type),
                            ("PERC_SEGUNDO", "float"),
                            ("TERCEIRO_LUGAR", "str"),
                            ("DIFF_1_2_PERC", "float"),
                        ]
                    )

                if config.enable_pivot:
                    for _, abs_col, pct_col in pivot_col_map:
                        field_specs.append((abs_col, val_type))
                        if config.include_percentages:
                            field_specs.append((pct_col, "float"))

                win_counts: Dict[str, int] = defaultdict(int)
                records: List[Dict[str, Any]] = []
                for cd_ibge, (ibge, tse, uf, nome) in sorted(b.mun_meta.items()):
                    tot_geral = b.mun_total_geral[cd_ibge]
                    tot_val = b.mun_total_validos[cd_ibge]
                    rec: Dict[str, Any] = {
                        "CD_MUN_IBGE": ibge,
                        "CD_MUN_TSE": tse,
                        "SG_UF": uf,
                        "NM_MUN": nome,
                        "TOTAL_GERAL": round(tot_geral, 2) if is_float_metric else int(round(tot_geral)),
                        "TOTAL_VALIDOS": round(tot_val, 2) if is_float_metric else int(round(tot_val)),
                    }

                    if has_legenda:
                        l_val = b.mun_legenda[cd_ibge]
                        rec["VOTOS_LEGENDA"] = round(l_val, 2) if is_float_metric else int(round(l_val))
                    if has_blank_null:
                        b_val = b.mun_brancos[cd_ibge]
                        n_val = b.mun_nulos[cd_ibge]
                        rec["VOTOS_BRANCOS"] = round(b_val, 2) if is_float_metric else int(round(b_val))
                        rec["VOTOS_NULOS"] = round(n_val, 2) if is_float_metric else int(round(n_val))

                    p_dict = b.mun_pivot_values[cd_ibge]
                    if config.include_winner and selected_pivots:
                        if p_dict:
                            ranked = sorted(p_dict.items(), key=lambda kv: (-kv[1], kv[0]))
                            win_key, win_val = ranked[0]
                            win_counts[win_key] += 1
                            win_pct = round((win_val / tot_val) * 100.0, 2) if tot_val > 0 else 0.0
                            if len(ranked) > 1:
                                sec_key, sec_val = ranked[1]
                                sec_pct = round((sec_val / tot_val) * 100.0, 2) if tot_val > 0 else 0.0
                            else:
                                sec_key, sec_val, sec_pct = "", 0.0, 0.0
                            third_key = ranked[2][0] if len(ranked) > 2 else ""

                            rec["VENCEDOR"] = win_key
                            rec["VOTOS_VENCEDOR"] = round(win_val, 2) if is_float_metric else int(round(win_val))
                            rec["PERC_VENCEDOR"] = win_pct
                            rec["SEGUNDO_LUGAR"] = sec_key
                            rec["VOTOS_SEGUNDO"] = round(sec_val, 2) if is_float_metric else int(round(sec_val))
                            rec["PERC_SEGUNDO"] = sec_pct
                            rec["TERCEIRO_LUGAR"] = third_key
                            rec["DIFF_1_2_PERC"] = round(win_pct - sec_pct, 2)
                        else:
                            rec["VENCEDOR"] = ""
                            rec["VOTOS_VENCEDOR"] = 0.0 if is_float_metric else 0
                            rec["PERC_VENCEDOR"] = 0.0
                            rec["SEGUNDO_LUGAR"] = ""
                            rec["VOTOS_SEGUNDO"] = 0.0 if is_float_metric else 0
                            rec["PERC_SEGUNDO"] = 0.0
                            rec["TERCEIRO_LUGAR"] = ""
                            rec["DIFF_1_2_PERC"] = 0.0

                    if config.enable_pivot:
                        for p_key, abs_col, pct_col in pivot_col_map:
                            v = p_dict.get(p_key, 0.0)
                            rec[abs_col] = round(v, 2) if is_float_metric else int(round(v))
                            if config.include_percentages:
                                rec[pct_col] = round((v / tot_val) * 100.0, 2) if tot_val > 0 else 0.0

                    records.append(rec)

                pivot_summary = [
                    (p_key, b.global_pivot_totals[p_key], win_counts.get(p_key, 0))
                    for p_key in selected_pivots
                ]
                selected_set = set(selected_pivots)
                other_wins = sum(cnt for k, cnt in win_counts.items() if k not in selected_set)
                primary_pct = pivot_col_map[0][2] if (pivot_col_map and config.include_percentages) else (
                    pivot_col_map[0][1] if pivot_col_map else "PERC_VENCEDOR"
                )
                primary_cand = pivot_col_map[0][0] if pivot_col_map else ""

                results.append(
                    (
                        group_title,
                        TseAggregationResult(
                            aggregation_level="municipio",
                            profile=profile,
                            field_specs=field_specs,
                            records=records,
                            total_rows_read=total_rows,
                            matched_rows=b.matched_rows,
                            unmatched_tse_codes=unmatched_codes,
                            detected_ufs=b.detected_ufs,
                            group_label=group_title,
                            pivot_summary=pivot_summary,
                            other_winners_count=other_wins,
                            primary_pct_field=primary_pct,
                            primary_candidate_label=primary_cand,
                        ),
                    )
                )

            return results
        finally:
            stream.close()
            if zf is not None:
                zf.close()

    def _process_votacao_por_local_grouped(
        self,
        config: TseProcessingConfig,
        meta: TseFileMetadata,
        profile: str,
        mun_code_col: str,
        mun_name_col: str,
        uf_col: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> List[Tuple[str, TseAggregationResult]]:
        stream, zf = self._open_text_stream(config.file_path, meta.zip_member, meta.encoding)
        try:
            reader = csv.reader(stream, delimiter=meta.delimiter, quotechar='"')
            headers = [h.strip().strip('"').upper() for h in next(reader)]
            col_idx = {h: i for i, h in enumerate(headers)}

            mun_code_idx = col_idx.get(mun_code_col, -1)
            mun_name_idx = col_idx.get(mun_name_col, -1)
            uf_idx = col_idx.get(uf_col, -1)
            cargo_idx = col_idx.get(meta.cargo_col, -1)
            turno_idx = col_idx.get(meta.turno_col, -1)
            ano_idx = col_idx.get("ANO_ELEICAO", col_idx.get("AA_ELEICAO", -1))

            zona_idx = col_idx.get("NR_ZONA", -1)
            local_num_idx = col_idx.get("NR_LOCAL_VOTACAO", -1)
            local_name_idx = col_idx.get("NM_LOCAL_VOTACAO", -1)
            endereco_idx = col_idx.get(
                "DS_LOCAL_VOTACAO_ENDERECO", col_idx.get("DS_ENDERECO", -1)
            )
            bairro_idx = col_idx.get("NM_BAIRRO", -1)
            cep_idx = col_idx.get("NR_CEP", -1)
            secao_idx = col_idx.get("NR_SECAO", -1)
            lat_idx = col_idx.get(meta.lat_col, -1) if meta.lat_col else -1
            lon_idx = col_idx.get(meta.lon_col, -1) if meta.lon_col else -1

            need_candidates = config.enable_pivot or config.include_winner
            pivot_col = (config.pivot_col or meta.default_pivot_col) if need_candidates else ""

            val_col = config.value_col or meta.default_value_col or "__COUNT__"
            val_idx = col_idx.get(val_col, -1) if val_col != "__COUNT__" else -1
            is_float_metric = val_col.startswith("VR_")

            nr_cand_idx = col_idx.get("NR_CANDIDATO", col_idx.get("NR_VOTAVEL", -1))
            nm_urna_idx = col_idx.get(
                "NM_URNA_CANDIDATO", col_idx.get("NM_VOTAVEL", col_idx.get("NM_CANDIDATO", -1))
            )
            sg_part_idx = col_idx.get("SG_PARTIDO", -1)
            pivot_idx = (
                col_idx.get(pivot_col, -1)
                if pivot_col not in ("CANDIDATO_COMPOSTO", "PARTIDO_COMPOSTO", "AUTO_CARGO")
                else -1
            )

            filter_uf = (config.filter_uf or "").strip().upper()
            if filter_uf in ("TODAS", "TODOS", "ALL"):
                filter_uf = ""
            filter_mun_norm = normalize_text(config.filter_municipio)
            if filter_mun_norm in ("TODOS", "TODAS", "ALL"):
                filter_mun_norm = ""
            filter_cargo = normalize_cargo_label(config.filter_cargo)
            if filter_cargo in ("TODAS", "TODOS", "ALL"):
                filter_cargo = ""
            filter_turno = (config.filter_turno or "").strip()
            if filter_turno.upper() in ("TODAS", "TODOS", "ALL"):
                filter_turno = ""

            specific_filter_set = {
                normalize_text(v) for v in config.specific_pivot_values if v.strip()
            }

            buckets: Dict[Tuple[str, str], _LocalVotacaoBucket] = defaultdict(_LocalVotacaoBucket)
            total_rows = 0
            unmatched_codes: Set[str] = set()
            all_detected_ufs: Set[str] = set()
            detected_ano = "2026"

            for row in reader:
                total_rows += 1
                if progress_callback and total_rows % 50000 == 0:
                    progress_callback(
                        min(80, total_rows // 15000),
                        f"Processando votação por local (linha {total_rows:,})...",
                    )

                if total_rows == 1 and 0 <= ano_idx < len(row):
                    val_ano = row[ano_idx].strip()
                    if val_ano.isdigit():
                        detected_ano = val_ano

                uf_val = row[uf_idx].strip().upper() if 0 <= uf_idx < len(row) else ""
                if config.exclude_exterior_zz and uf_val == "ZZ":
                    continue
                if filter_uf and uf_val != filter_uf:
                    continue

                row_cargo = normalize_cargo_label(row[cargo_idx]) if 0 <= cargo_idx < len(row) else ""
                if filter_cargo and row_cargo != filter_cargo:
                    continue

                row_turno = row[turno_idx].strip() if 0 <= turno_idx < len(row) else ""
                if filter_turno and row_turno != filter_turno:
                    continue

                raw_tse = row[mun_code_idx] if 0 <= mun_code_idx < len(row) else ""
                raw_name = row[mun_name_idx] if 0 <= mun_name_idx < len(row) else ""
                cd_ibge, cd_tse, uf_res, nm_res = self.mapper.resolve(raw_tse, uf_val, raw_name)
                if not cd_ibge:
                    if cd_tse:
                        unmatched_codes.add(cd_tse)
                    continue
                if filter_mun_norm and normalize_text(nm_res) != filter_mun_norm and normalize_text(raw_name) != filter_mun_norm:
                    continue

                nr_zona = (
                    str(int(row[zona_idx].strip()))
                    if (0 <= zona_idx < len(row) and row[zona_idx].strip().isdigit())
                    else (row[zona_idx].strip() if 0 <= zona_idx < len(row) else "")
                )
                nr_local = (
                    str(int(row[local_num_idx].strip()))
                    if (0 <= local_num_idx < len(row) and row[local_num_idx].strip().isdigit())
                    else (row[local_num_idx].strip() if 0 <= local_num_idx < len(row) else "0")
                )
                l_key = (cd_ibge, nr_zona, nr_local)

                if val_col == "__COUNT__":
                    metric_val = 1.0
                elif 0 <= val_idx < len(row):
                    metric_val = parse_tse_float(row[val_idx])
                else:
                    metric_val = 0.0

                g_key = (
                    row_cargo if config.split_layers_by_cargo else (filter_cargo or ""),
                    row_turno if config.split_layers_by_cargo else (filter_turno or ""),
                )
                b = buckets[g_key]
                b.matched_rows += 1
                if uf_res:
                    b.detected_ufs.add(uf_res)
                    all_detected_ufs.add(uf_res)

                lat = parse_tse_coord(row[lat_idx]) if 0 <= lat_idx < len(row) else None
                lon = parse_tse_coord(row[lon_idx]) if 0 <= lon_idx < len(row) else None

                if l_key not in b.local_meta:
                    b.local_meta[l_key] = {
                        "CD_MUN_IBGE": cd_ibge,
                        "CD_MUN_TSE": cd_tse,
                        "SG_UF": uf_res,
                        "NM_MUN": nm_res,
                        "NR_ZONA": nr_zona,
                        "NR_LOCAL_VOTACAO": nr_local,
                        "NM_LOCAL_VOTACAO": row[local_name_idx].strip() if 0 <= local_name_idx < len(row) else "",
                        "DS_ENDERECO": row[endereco_idx].strip() if 0 <= endereco_idx < len(row) else "",
                        "NM_BAIRRO": row[bairro_idx].strip() if 0 <= bairro_idx < len(row) else "",
                        "NR_CEP": row[cep_idx].strip() if 0 <= cep_idx < len(row) else "",
                        "LATITUDE": lat,
                        "LONGITUDE": lon,
                    }
                elif b.local_meta[l_key]["LATITUDE"] is None and lat is not None:
                    b.local_meta[l_key]["LATITUDE"] = lat
                    b.local_meta[l_key]["LONGITUDE"] = lon

                if 0 <= secao_idx < len(row):
                    b.local_secoes[l_key].add(row[secao_idx].strip())

                b.local_total_geral[l_key] += metric_val

                nr_code = row[nr_cand_idx].strip() if 0 <= nr_cand_idx < len(row) else ""
                if profile == "votacao_candidato" and nr_code in TSE_BLANK_CODES:
                    b.local_brancos[l_key] += metric_val
                    continue
                if profile == "votacao_candidato" and nr_code in TSE_NULL_VOTE_CODES:
                    b.local_nulos[l_key] += metric_val
                    continue

                b.local_total_validos[l_key] += metric_val

                effective_pivot = pivot_col
                if pivot_col == "AUTO_CARGO":
                    effective_pivot = (
                        "PARTIDO_COMPOSTO" if row_cargo in PROPORTIONAL_CARGOS else "CANDIDATO_COMPOSTO"
                    )

                is_legend_vote = (
                    profile == "votacao_candidato"
                    and row_cargo in PROPORTIONAL_CARGOS
                    and len(nr_code) == 2
                    and nr_code.isdigit()
                )
                if is_legend_vote:
                    b.local_legenda[l_key] += metric_val
                    if config.separate_party_legend_votes and effective_pivot not in (
                        "PARTIDO_COMPOSTO",
                        "SG_PARTIDO",
                    ):
                        continue

                if need_candidates and effective_pivot:
                    p_key = self._extract_pivot_key(
                        row, effective_pivot, nr_code, nm_urna_idx, sg_part_idx, pivot_idx
                    )
                    if specific_filter_set:
                        norm_p = normalize_text(p_key)
                        if not any(f in norm_p for f in specific_filter_set):
                            continue

                    b.local_pivot_values[l_key][p_key] += metric_val
                    b.global_pivot_totals[p_key] += metric_val

            coords_lookup: Dict[str, Dict[str, object]] = {}
            if not meta.has_coordinates and all_detected_ufs:
                coord_client = TseLocaisCoordClient()
                coords_lookup = coord_client.load_coords_for_ufs(
                    all_detected_ufs, ano=detected_ano, progress_callback=progress_callback
                )

            val_type = "float" if is_float_metric else "int"
            prefix_abs = "VOT" if profile in ("votacao_candidato", "votacao_partido") else "QT"
            if is_float_metric:
                prefix_abs = "VR"

            results: List[Tuple[str, TseAggregationResult]] = []
            for g_key in sorted(buckets.keys(), key=cargo_sort_key):
                b = buckets[g_key]
                group_title = format_group_title(g_key[0], g_key[1])

                sorted_pivots = sorted(
                    b.global_pivot_totals.keys(),
                    key=lambda k: (-b.global_pivot_totals[k], k),
                )
                if config.top_n_pivots and config.top_n_pivots > 0:
                    selected_pivots = sorted_pivots[: config.top_n_pivots]
                else:
                    selected_pivots = sorted_pivots

                pivot_col_map: List[Tuple[str, str, str]] = []
                used_col_names: Set[str] = {
                    "CD_MUN_IBGE",
                    "CD_MUN_TSE",
                    "SG_UF",
                    "NM_MUN",
                    "NR_ZONA",
                    "NR_LOCAL_VOTACAO",
                    "NM_LOCAL_VOTACAO",
                    "DS_ENDERECO",
                    "NM_BAIRRO",
                    "NR_CEP",
                    "QT_SECOES",
                    "LATITUDE",
                    "LONGITUDE",
                    "TOTAL_GERAL",
                    "TOTAL_VALIDOS",
                    "VOTOS_BRANCOS",
                    "VOTOS_NULOS",
                    "VOTOS_LEGENDA",
                    "VENCEDOR",
                    "VOTOS_VENCEDOR",
                    "PERC_VENCEDOR",
                    "SEGUNDO_LUGAR",
                    "VOTOS_SEGUNDO",
                    "PERC_SEGUNDO",
                    "TERCEIRO_LUGAR",
                    "DIFF_1_2_PERC",
                }

                for p_key in selected_pivots:
                    slug = make_safe_slug(p_key, max_len=24)
                    abs_col = f"{prefix_abs}_{slug}"
                    pct_col = f"PCT_{slug}"
                    counter = 2
                    while abs_col in used_col_names or pct_col in used_col_names:
                        abs_col = f"{prefix_abs}_{slug}_{counter}"
                        pct_col = f"PCT_{slug}_{counter}"
                        counter += 1
                    used_col_names.add(abs_col)
                    used_col_names.add(pct_col)
                    pivot_col_map.append((p_key, abs_col, pct_col))

                field_specs: List[Tuple[str, str]] = [
                    ("CD_MUN_IBGE", "str"),
                    ("CD_MUN_TSE", "str"),
                    ("SG_UF", "str"),
                    ("NM_MUN", "str"),
                    ("NR_ZONA", "str"),
                    ("NR_LOCAL_VOTACAO", "str"),
                    ("NM_LOCAL_VOTACAO", "str"),
                    ("DS_ENDERECO", "str"),
                    ("NM_BAIRRO", "str"),
                    ("NR_CEP", "str"),
                    ("QT_SECOES", "int"),
                    ("LATITUDE", "float"),
                    ("LONGITUDE", "float"),
                    ("TOTAL_GERAL", val_type),
                    ("TOTAL_VALIDOS", val_type),
                ]
                has_blank_null = any(b.local_brancos.values()) or any(b.local_nulos.values())
                has_legenda = any(b.local_legenda.values())
                if has_legenda:
                    field_specs.append(("VOTOS_LEGENDA", val_type))
                if has_blank_null:
                    field_specs.append(("VOTOS_BRANCOS", val_type))
                    field_specs.append(("VOTOS_NULOS", val_type))

                if config.include_winner and selected_pivots:
                    field_specs.extend(
                        [
                            ("VENCEDOR", "str"),
                            ("VOTOS_VENCEDOR", val_type),
                            ("PERC_VENCEDOR", "float"),
                            ("SEGUNDO_LUGAR", "str"),
                            ("VOTOS_SEGUNDO", val_type),
                            ("PERC_SEGUNDO", "float"),
                            ("TERCEIRO_LUGAR", "str"),
                            ("DIFF_1_2_PERC", "float"),
                        ]
                    )

                if config.enable_pivot:
                    for _, abs_col, pct_col in pivot_col_map:
                        field_specs.append((abs_col, val_type))
                        if config.include_percentages:
                            field_specs.append((pct_col, "float"))

                win_counts: Dict[str, int] = defaultdict(int)
                records: List[Dict[str, Any]] = []

                for l_key in sorted(b.local_meta.keys()):
                    meta_info = b.local_meta[l_key]
                    cd_tse = meta_info["CD_MUN_TSE"]
                    nr_zona = meta_info["NR_ZONA"]
                    nr_local = meta_info["NR_LOCAL_VOTACAO"]

                    lat = meta_info.get("LATITUDE")
                    lon = meta_info.get("LONGITUDE")
                    bairro = meta_info.get("NM_BAIRRO", "")
                    cep = meta_info.get("NR_CEP", "")
                    nm_loc = meta_info.get("NM_LOCAL_VOTACAO", "")
                    end_loc = meta_info.get("DS_ENDERECO", "")

                    if (lat is None or lon is None) and coords_lookup:
                        c_info = coords_lookup.get(f"{cd_tse}:{nr_zona}:{nr_local}")
                        if c_info:
                            lat = c_info.get("lat")
                            lon = c_info.get("lon")
                            if not bairro:
                                bairro = str(c_info.get("bairro", ""))
                            if not cep:
                                cep = str(c_info.get("cep", ""))
                            if not nm_loc:
                                nm_loc = str(c_info.get("nome", ""))
                            if not end_loc:
                                end_loc = str(c_info.get("endereco", ""))
                        if lat is None or lon is None:
                            mun_fallback = coords_lookup.get(f"MUN:{cd_tse}")
                            if mun_fallback:
                                lat = mun_fallback.get("lat")
                                lon = mun_fallback.get("lon")

                    tot_geral = b.local_total_geral[l_key]
                    tot_val = b.local_total_validos[l_key]
                    secoes = b.local_secoes[l_key]

                    rec: Dict[str, Any] = {
                        "CD_MUN_IBGE": meta_info["CD_MUN_IBGE"],
                        "CD_MUN_TSE": cd_tse,
                        "SG_UF": meta_info["SG_UF"],
                        "NM_MUN": meta_info["NM_MUN"],
                        "NR_ZONA": nr_zona,
                        "NR_LOCAL_VOTACAO": nr_local,
                        "NM_LOCAL_VOTACAO": nm_loc,
                        "DS_ENDERECO": end_loc,
                        "NM_BAIRRO": bairro,
                        "NR_CEP": cep,
                        "QT_SECOES": len(secoes) if secoes else 1,
                        "LATITUDE": lat,
                        "LONGITUDE": lon,
                        "TOTAL_GERAL": round(tot_geral, 2) if is_float_metric else int(round(tot_geral)),
                        "TOTAL_VALIDOS": round(tot_val, 2) if is_float_metric else int(round(tot_val)),
                    }

                    if has_legenda:
                        l_val = b.local_legenda[l_key]
                        rec["VOTOS_LEGENDA"] = round(l_val, 2) if is_float_metric else int(round(l_val))
                    if has_blank_null:
                        b_val = b.local_brancos[l_key]
                        n_val = b.local_nulos[l_key]
                        rec["VOTOS_BRANCOS"] = round(b_val, 2) if is_float_metric else int(round(b_val))
                        rec["VOTOS_NULOS"] = round(n_val, 2) if is_float_metric else int(round(n_val))

                    p_dict = b.local_pivot_values[l_key]
                    if config.include_winner and selected_pivots:
                        if p_dict:
                            ranked = sorted(p_dict.items(), key=lambda kv: (-kv[1], kv[0]))
                            win_key, win_val = ranked[0]
                            win_counts[win_key] += 1
                            win_pct = round((win_val / tot_val) * 100.0, 2) if tot_val > 0 else 0.0
                            if len(ranked) > 1:
                                sec_key, sec_val = ranked[1]
                                sec_pct = round((sec_val / tot_val) * 100.0, 2) if tot_val > 0 else 0.0
                            else:
                                sec_key, sec_val, sec_pct = "", 0.0, 0.0
                            third_key = ranked[2][0] if len(ranked) > 2 else ""

                            rec["VENCEDOR"] = win_key
                            rec["VOTOS_VENCEDOR"] = round(win_val, 2) if is_float_metric else int(round(win_val))
                            rec["PERC_VENCEDOR"] = win_pct
                            rec["SEGUNDO_LUGAR"] = sec_key
                            rec["VOTOS_SEGUNDO"] = round(sec_val, 2) if is_float_metric else int(round(sec_val))
                            rec["PERC_SEGUNDO"] = sec_pct
                            rec["TERCEIRO_LUGAR"] = third_key
                            rec["DIFF_1_2_PERC"] = round(win_pct - sec_pct, 2)
                        else:
                            rec["VENCEDOR"] = ""
                            rec["VOTOS_VENCEDOR"] = 0.0 if is_float_metric else 0
                            rec["PERC_VENCEDOR"] = 0.0
                            rec["SEGUNDO_LUGAR"] = ""
                            rec["VOTOS_SEGUNDO"] = 0.0 if is_float_metric else 0
                            rec["PERC_SEGUNDO"] = 0.0
                            rec["TERCEIRO_LUGAR"] = ""
                            rec["DIFF_1_2_PERC"] = 0.0

                    if config.enable_pivot:
                        for p_key, abs_col, pct_col in pivot_col_map:
                            v = p_dict.get(p_key, 0.0)
                            rec[abs_col] = round(v, 2) if is_float_metric else int(round(v))
                            if config.include_percentages:
                                rec[pct_col] = round((v / tot_val) * 100.0, 2) if tot_val > 0 else 0.0

                    records.append(rec)

                pivot_summary = [
                    (p_key, b.global_pivot_totals[p_key], win_counts.get(p_key, 0))
                    for p_key in selected_pivots
                ]
                selected_set = set(selected_pivots)
                other_wins = sum(cnt for k, cnt in win_counts.items() if k not in selected_set)
                primary_pct = pivot_col_map[0][2] if (pivot_col_map and config.include_percentages) else (
                    pivot_col_map[0][1] if pivot_col_map else "PERC_VENCEDOR"
                )
                primary_cand = pivot_col_map[0][0] if pivot_col_map else ""

                results.append(
                    (
                        group_title,
                        TseAggregationResult(
                            aggregation_level="local_votacao",
                            profile=profile,
                            field_specs=field_specs,
                            records=records,
                            total_rows_read=total_rows,
                            matched_rows=b.matched_rows,
                            unmatched_tse_codes=unmatched_codes,
                            detected_ufs=b.detected_ufs,
                            group_label=group_title,
                            pivot_summary=pivot_summary,
                            other_winners_count=other_wins,
                            primary_pct_field=primary_pct,
                            primary_candidate_label=primary_cand,
                        ),
                    )
                )

            return results
        finally:
            stream.close()
            if zf is not None:
                zf.close()
