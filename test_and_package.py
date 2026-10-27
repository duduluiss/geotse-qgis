# -*- coding: utf-8 -*-
"""
Script de testes automatizados e empacotamento (.zip) do plugin TSE GeoBrasil.
Testa todos os layouts de arquivos do TSE, a conversão de 5 dígitos -> 7 dígitos (IBGE),
o pivot de linhas em colunas e o download da API v3 do IBGE.
"""

import csv
import io
import os
import sys
import tempfile
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from tse_to_qgis.core.municipality_mapper import MunicipalityMapper
from tse_to_qgis.core.tse_engine import TseEngine, TseProcessingConfig
from tse_to_qgis.core.ibge_api import IbgeMalhasClient


def test_mapper():
    mapper = MunicipalityMapper.instance()
    # São Paulo: TSE 71072 -> IBGE 3550308
    ibge, tse, uf, nome = mapper.resolve("71072", "SP", "SÃO PAULO")
    assert ibge == "3550308", f"Esperado 3550308, obtido {ibge}"
    # Acrelândia: TSE 01120 (ou 1120) -> IBGE 1200013
    ibge, tse, uf, nome = mapper.resolve(1120, "AC", "ACRELÂNDIA")
    assert ibge == "1200013" and tse == "01120", f"Falha Acrelândia: {ibge}, {tse}"
    # Boa Esperança do Norte (emancipado 2024): IBGE 5101837
    ibge, tse, uf, nome = mapper.resolve("73709", "MT", "BOA ESPERANÇA DO NORTE")
    assert ibge == "5101837", f"Falha Boa Esperança do Norte: {ibge}"
    # Fallback por nome quando código TSE é desconhecido
    ibge, _, _, _ = mapper.resolve("99999", "RJ", "RIO DE JANEIRO")
    assert ibge == "3304557", f"Falha fallback RJ: {ibge}"
    print("[OK] MunicipalityMapper validado com sucesso (5.571 municípios)!")


def test_all_tse_profiles():
    engine = TseEngine()

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Teste votacao_candidato_munzona dentro de um arquivo .ZIP em Latin-1
        zip_path = os.path.join(tmpdir, "votacao_candidato_munzona_2024_SP.zip")
        csv_content = (
            '"SG_UF";"CD_MUNICIPIO";"NM_MUNICIPIO";"NR_ZONA";"NR_TURNO";"DS_CARGO";"NR_CANDIDATO";"NM_URNA_CANDIDATO";"SG_PARTIDO";"QT_VOTOS_NOMINAIS_VALIDOS"\n'
            '"SP";"71072";"SÃO PAULO";"1";"1";"PREFEITO";"15";"CANDIDATO A";"MDB";"12000"\n'
            '"SP";"71072";"SÃO PAULO";"2";"1";"PREFEITO";"15";"CANDIDATO A";"MDB";"8000"\n'
            '"SP";"71072";"SÃO PAULO";"1";"1";"PREFEITO";"50";"CANDIDATO B";"PSOL";"15000"\n'
            '"SP";"62910";"CAMPINAS";"33";"1";"PREFEITO";"15";"CANDIDATO C";"MDB";"40000"\n'
        )
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("votacao_candidato_munzona_2024_SP.csv", csv_content.encode("iso-8859-1"))

        meta = engine.inspect_file(zip_path)
        assert meta.profile == "votacao_candidato"
        res = engine.process(
            TseProcessingConfig(
                file_path=zip_path,
                filter_cargo="PREFEITO",
                filter_turno="1",
                enable_pivot=True,
                pivot_col="SG_PARTIDO",
            )
        )
        assert len(res.records) == 2
        sp_rec = next(r for r in res.records if r["CD_MUN_IBGE"] == "3550308")
        # Soma das zonas 1 e 2 de SP para MDB = 12000 + 8000 = 20000; PSOL = 15000; Total = 35000
        assert sp_rec["TOTAL_VALIDOS"] == 35000
        assert sp_rec["VOT_MDB"] == 20000
        assert sp_rec["VOT_PSOL"] == 15000
        assert sp_rec["VENCEDOR"] == "MDB"
        print("[OK] Perfil 'votacao_candidato' (ZIP + soma de zonas + pivot + vencedor) validado!")

        # 2. Teste detalhe_votacao_munzona
        det_path = os.path.join(tmpdir, "detalhe_votacao_munzona.csv")
        with open(det_path, "w", encoding="iso-8859-1") as f:
            f.write(
                '"SG_UF";"CD_MUNICIPIO";"NM_MUNICIPIO";"NR_ZONA";"NR_TURNO";"DS_CARGO";"QT_APTOS";"QT_COMPARECIMENTO";"QT_ABSTENCOES";"QT_VOTOS_VALIDOS";"QT_VOTOS_BRANCOS";"QT_VOTOS_NULOS"\n'
                '"AC";"01392";"RIO BRANCO";"1";"1";"PREFEITO";"1000";"800";"200";"750";"20";"30"\n'
                '"AC";"01392";"RIO BRANCO";"9";"1";"PREFEITO";"1000";"900";"100";"850";"25";"25"\n'
            )
        res_det = engine.process(
            TseProcessingConfig(file_path=det_path, enable_pivot=False)
        )
        assert len(res_det.records) == 1
        rb = res_det.records[0]
        assert rb["CD_MUN_IBGE"] == "1200401"
        assert rb["QT_ZONAS"] == 2
        assert rb["QT_APTOS"] == 2000
        assert rb["QT_COMPARECIMENTO"] == 1700
        assert rb["PCT_COMPARECIMENTO"] == 85.0
        assert rb["PCT_ABSTENCAO"] == 15.0
        print("[OK] Perfil 'detalhe_votacao' validado!")

        # 3. Teste eleitorado_local_votacao (Pontos com NR_LATITUDE / NR_LONGITUDE)
        loc_path = os.path.join(tmpdir, "eleitorado_local_votacao.csv")
        with open(loc_path, "w", encoding="iso-8859-1") as f:
            f.write(
                '"SG_UF";"CD_MUNICIPIO";"NM_MUNICIPIO";"NR_ZONA";"NR_LOCAL_VOTACAO";"NM_LOCAL_VOTACAO";"DS_ENDERECO";"NM_BAIRRO";"NR_CEP";"NR_SECAO";"QT_ELEITOR_SECAO";"NR_LATITUDE";"NR_LONGITUDE"\n'
                '"AC";"01392";"RIO BRANCO";"1";"1015";"ESCOLA A";"RUA X";"CENTRO";"69900000";"1";"350";"-9,97499";"-67,8243"\n'
                '"AC";"01392";"RIO BRANCO";"1";"1015";"ESCOLA A";"RUA X";"CENTRO";"69900000";"2";"320";"-9,97499";"-67,8243"\n'
            )
        res_loc = engine.process(
            TseProcessingConfig(file_path=loc_path, aggregation_level="local_votacao")
        )
        assert len(res_loc.records) == 1
        loc_rec = res_loc.records[0]
        assert loc_rec["CD_MUN_IBGE"] == "1200401"
        assert loc_rec["QT_SECOES"] == 2
        assert loc_rec["QT_ELEITORES"] == 670
        assert abs(loc_rec["LATITUDE"] - (-9.97499)) < 1e-5
        print("[OK] Perfil 'locais_votacao' (coordenadas e agregação de seções) validado!")

        # 4. Teste consulta_cand / receitas_candidatos (usando coluna SG_UE para código TSE)
        rec_path = os.path.join(tmpdir, "receitas_candidatos.csv")
        with open(rec_path, "w", encoding="iso-8859-1") as f:
            f.write(
                '"SG_UF";"SG_UE";"NM_UE";"DS_CARGO";"SG_PARTIDO";"VR_RECEITA"\n'
                '"AC";"01120";"ACRELÂNDIA";"PREFEITO";"MDB";"15000,50"\n'
                '"AC";"01120";"ACRELÂNDIA";"PREFEITO";"MDB";"4999,50"\n'
                '"AC";"01120";"ACRELÂNDIA";"PREFEITO";"PP";"10000,00"\n'
            )
        res_rec = engine.process(
            TseProcessingConfig(file_path=rec_path, enable_pivot=True, pivot_col="SG_PARTIDO")
        )
        assert len(res_rec.records) == 1
        ac_rec = res_rec.records[0]
        assert ac_rec["CD_MUN_IBGE"] == "1200013"
        assert ac_rec["VR_MDB"] == 20000.0
        assert ac_rec["VR_PP"] == 10000.0
        # 5. Teste separação em grupos/camadas quando Cargo="TODOS" (Eleições Gerais + Municipais: Prefeito e Vereador)
        multi_path = os.path.join(tmpdir, "votacao_multi_cargos.csv")
        with open(multi_path, "w", encoding="iso-8859-1") as f:
            f.write(
                '"SG_UF";"CD_MUNICIPIO";"NM_MUNICIPIO";"NR_ZONA";"NR_TURNO";"DS_CARGO";"NR_CANDIDATO";"NM_URNA_CANDIDATO";"SG_PARTIDO";"QT_VOTOS_NOMINAIS_VALIDOS"\n'
                '"SP";"71072";"SÃO PAULO";"1";"1";"PREFEITO";"15";"CAND PREF A";"MDB";"50000"\n'
                '"SP";"71072";"SÃO PAULO";"1";"2";"PREFEITO";"15";"CAND PREF A";"MDB";"60000"\n'
                '"SP";"71072";"SÃO PAULO";"1";"1";"VEREADOR";"15123";"CAND VER B";"MDB";"5000"\n'
                '"SP";"71072";"SÃO PAULO";"1";"1";"PRESIDENTE";"13";"CAND PRES";"PT";"80000"\n'
                '"SP";"71072";"SÃO PAULO";"1";"1";"DEPUTADO FEDERAL";"1313";"CAND DEP FED";"PT";"12000"\n'
            )
        grouped = engine.process_by_groups(
            TseProcessingConfig(
                file_path=multi_path,
                filter_cargo="TODOS",
                filter_turno="TODOS",
                enable_pivot=True,
                pivot_col="SG_PARTIDO",
            )
        )
        group_titles = [g[0] for g in grouped]
        assert group_titles == [
            "PRESIDENTE - 1º Turno",
            "DEPUTADO FEDERAL - 1º Turno",
            "PREFEITO - 1º Turno",
            "PREFEITO - 2º Turno",
            "VEREADOR - 1º Turno",
        ], f"Ordem/grupos inesperados: {group_titles}"
        print("[OK] Separação automática em camadas por Cargo e Turno (Gerais + Prefeito/Vereador) validada!")

        # 6. Teste Voto de Legenda (2 dígitos em Deputado Estadual), PARTIDO_COMPOSTO, filtro de Município e Outros Vencedores
        secao_path = os.path.join(tmpdir, "votacao_secao_2026_SC_sample.csv")
        with open(secao_path, "w", encoding="iso-8859-1") as f:
            f.write(
                '"ANO_ELEICAO";"SG_UF";"CD_MUNICIPIO";"NM_MUNICIPIO";"NR_ZONA";"NR_SECAO";"NR_LOCAL_VOTACAO";"NM_LOCAL_VOTACAO";"DS_LOCAL_VOTACAO_ENDERECO";"NR_TURNO";"DS_CARGO";"NR_VOTAVEL";"NM_VOTAVEL";"QT_VOTOS"\n'
                '"2026";"SC";"81051";"FLORIANÓPOLIS";"12";"1";"1015";"COLÉGIO CATARINENSE";"RUA ESTEUS";"1";"Deputado Estadual";"22";"PARTIDO LIBERAL";"180"\n'
                '"2026";"SC";"81051";"FLORIANÓPOLIS";"12";"1";"1015";"COLÉGIO CATARINENSE";"RUA ESTEUS";"1";"Deputado Estadual";"22123";"CANDIDATO NOMINAL PL";"140"\n'
                '"2026";"SC";"81051";"FLORIANÓPOLIS";"12";"1";"1015";"COLÉGIO CATARINENSE";"RUA ESTEUS";"1";"Deputado Estadual";"13123";"CANDIDATO NOMINAL PT";"120"\n'
                '"2026";"SC";"81051";"FLORIANÓPOLIS";"12";"1";"1023";"ESCOLA B";"RUA Y";"1";"Deputado Estadual";"50999";"CANDIDATO BAIRRO PSOL";"110"\n'
                '"2026";"SC";"81795";"JOINVILLE";"19";"10";"1015";"ESCOLA JOINVILLE";"RUA Z";"1";"Deputado Estadual";"30111";"CANDIDATO NOVO";"250"\n'
            )

        # 6a. Modo CANDIDATO_COMPOSTO deve separar o voto de legenda "22" em VOTOS_LEGENDA
        res_nom = engine.process(
            TseProcessingConfig(
                file_path=secao_path,
                aggregation_level="local_votacao",
                filter_municipio="FLORIANÓPOLIS",
                pivot_col="CANDIDATO_COMPOSTO",
                top_n_pivots=2,
            )
        )
        assert len(res_nom.records) == 2, f"Esperado 2 locais em Florianópolis, obtido {len(res_nom.records)}"
        loc_cat = next(r for r in res_nom.records if r["NR_LOCAL_VOTACAO"] == "1015")
        assert loc_cat["VOTOS_LEGENDA"] == 180, f"VOTOS_LEGENDA incorreto: {loc_cat}"
        assert loc_cat["VENCEDOR"] == "22123_CANDIDATO NOMINAL PL", f"VENCEDOR nominal incorreto: {loc_cat['VENCEDOR']}"
        assert loc_cat["LATITUDE"] is not None and loc_cat["LONGITUDE"] is not None
        assert res_nom.other_winners_count == 1, f"Esperado 1 outro vencedor fora do Top 2, obtido {res_nom.other_winners_count}"

        # 6b. Modo PARTIDO_COMPOSTO deve somar legenda + nominais na mesma sigla ("22_PL" = 180 + 140 = 320)
        res_part = engine.process(
            TseProcessingConfig(
                file_path=secao_path,
                aggregation_level="local_votacao",
                filter_municipio="FLORIANÓPOLIS",
                pivot_col="PARTIDO_COMPOSTO",
            )
        )
        loc_part = next(r for r in res_part.records if r["NR_LOCAL_VOTACAO"] == "1015")
        assert loc_part["VENCEDOR"] == "22_PL"
        assert loc_part["VOTOS_VENCEDOR"] == 320
        print("[OK] Separação de Votos de Legenda, PARTIDO_COMPOSTO, filtro de Município e Outros Vencedores validados!")


def test_ibge_api():
    client = IbgeMalhasClient()
    geojson_path = client.fetch_geojson_for_ufs(["AC"], qualidade="minima")
    assert os.path.exists(geojson_path) and os.path.getsize(geojson_path) > 1000
    print(f"[OK] IbgeMalhasClient validado! Arquivo em cache: {geojson_path}")


def package_plugin_zip():
    import shutil

    plugin_folder = os.path.join(PROJECT_ROOT, "tse_to_qgis")
    out_zip = os.path.join(PROJECT_ROOT, "tse_to_qgis.zip")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(plugin_folder):
            dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
            for file in files:
                if file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, PROJECT_ROOT)
                zf.write(abs_path, rel_path)
    print(f"[OK] Pacote ZIP do plugin gerado em: {out_zip} ({os.path.getsize(out_zip):,} bytes)")

    qgis4_plugin_dir = os.path.expandvars(
        r"%APPDATA%\QGIS\QGIS4\profiles\default\python\plugins\tse_to_qgis"
    )
    if os.path.exists(os.path.dirname(qgis4_plugin_dir)):
        if os.path.exists(qgis4_plugin_dir):
            shutil.rmtree(qgis4_plugin_dir)
        shutil.copytree(plugin_folder, qgis4_plugin_dir)
        print(f"[OK] Plugin sincronizado na pasta ativa do QGIS 4: {qgis4_plugin_dir}")

    from upload_to_qgis_hub import validate_qgis_zip
    ok, msgs = validate_qgis_zip(out_zip)
    for m in msgs:
        print(m)
    assert ok, "Falha na validação oficial do repositório de complementos do QGIS!"


if __name__ == "__main__":
    test_mapper()
    test_all_tse_profiles()
    test_ibge_api()
    package_plugin_zip()
