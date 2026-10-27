# GeoTSE - Espacializador Eleitoral (QGIS 3 & QGIS 4 Plugin)

**Autor / Author:** Luís Eduardo Araújo de Souza (`luisaraujodesouza.geo@gmail.com`)  
**Repositório / Repository:** [https://github.com/duduluiss/geotse-qgis](https://github.com/duduluiss/geotse-qgis)  
**Compatibilidade / Compatibility:** QGIS 3.28+ & QGIS 4.x (`PyQt5` / `PyQt6`, `supportsQt6=True`)  
**Licença / License:** GNU GPL v2+ / v3

---

## 🇧🇷 Descrição (Português)

O **GeoTSE - Espacializador Eleitoral** é um complemento para o QGIS projetado para automatizar a leitura, agregação e espacialização das tabelas de **Dados Abertos do Tribunal Superior Eleitoral (TSE)** do Brasil, tanto para **Eleições Gerais** (Presidente, Governador, Senador, Deputado Federal e Deputado Estadual/Distrital) quanto para **Eleições Municipais** (Prefeito e Vereador).

### Principais Funcionalidades
1. **Leitura Direta de Arquivos `.zip`, `.csv` e `.txt` do TSE**:
   - Suporta `votacao_candidato_munzona`, `votacao_secao`, `votacao_partido_munzona`, `detalhe_votacao_munzona`, `perfil_eleitorado`, `eleitorado_local_votacao`, `consulta_cand` e `prestacao_de_contas`.
2. **Tradução Automática TSE (5 dígitos) -> IBGE (7 dígitos)**:
   - Contém dicionário oficial embutido com todos os **5.571 municípios brasileiros** (incluindo Boa Esperança do Norte/MT), convertendo `CD_MUNICIPIO` / `SG_UE` (5 dígitos) para `CD_MUN_IBGE` (7 dígitos).
3. **Agregação e Pivotagem Inteligente**:
   - Soma zonas e seções eleitorais e transforma candidatos, partidos ou categorias de perfil em colunas (`VOT_...`, `PCT_...`), gerando **1 feição por município** ou **1 ponto por local de votação**.
   - **Eleições Proporcionais**: separa automaticamente votos de legenda de 2 dígitos na coluna `VOTOS_LEGENDA` e oferece agrupamento por partido (`PARTIDO_COMPOSTO` / `AUTO_CARGO`).
4. **4 Modos de Espacialização**:
   - Download automático da malha municipal via **API de Malhas v3 do IBGE** (`servicodados.ibge.gov.br`);
   - União (`Join 1:1`) com camada municipal já aberta no projeto do QGIS;
   - Geração de camada de **Pontos dos Locais de Votação** (com busca automática de coordenadas via HTTP Range no CDN do TSE quando ausentes em `votacao_secao`);
   - Geração de tabela alfanumérica padronizada com `CD_MUN_IBGE`.
5. **Simbologia Temática Automática**:
   - **Vencedor Graduado por Margem (`Data-Defined Color`)**: aplica a cor oficial do partido/candidato vencedor variando do tom claro (vitória disputada) ao tom escuro (vitória folgada) conforme `DIFF_1_2_PERC`.
   - **Vencedor Bruto (Cor Sólida)**: aplica a cor sólida oficial do partido/candidato vencedor.
   - **Graduado por `%` de Votos**: gera mapa graduado em 5 classes para o candidato principal ou filtrado.

---

## 🌐 Overview (English — For QGIS Plugin Reviewers)

**GeoTSE - Espacializador Eleitoral** is a QGIS 3 & QGIS 4 (`supportsQt6=True`) plugin that imports, aggregates, pivots, and spatializes open electoral datasets from Brazil's **Superior Electoral Court (TSE - Tribunal Superior Eleitoral)**.

- **Zero external dependencies**: Uses only Python's standard library and PyQGIS (`qgis.core`, `qgis.PyQt`).
- **TSE (5-digit) to IBGE (7-digit) Geocoding**: Ships with an offline lookup table mapping all 5,571 Brazilian municipalities (`data/tse_ibge_municipios.json`).
- **Automatic Geometry Retrieval**: Connects to the official IBGE v3 Municipal Mesh API (`servicodados.ibge.gov.br/api/v3/malhas`) and TSE Open Data CDN (`cdn.tse.jus.br`) to spatialize municipalities and polling places on the fly.
- **Processing Toolbox Support**: Includes both a compact GUI dialog and a native QGIS Processing Toolbox algorithm (`TseProcessingProvider`).
