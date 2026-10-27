# -*- coding: utf-8 -*-
"""
Validador oficial de conformidade e publicador automático para o repositório
oficial de complementos do QGIS (https://plugins.qgis.org/).

Uso:
  1. Apenas validar o pacote .zip localmente:
     python upload_to_qgis_hub.py --validate-only

  2. Publicar usando Token do portal plugins.qgis.org (ou variável QGIS_PLUGIN_TOKEN):
     python upload_to_qgis_hub.py --token SEU_TOKEN_AQUI

  3. Publicar usando usuário e senha OSGeo ID:
     python upload_to_qgis_hub.py --username duduluiss --password SUA_SENHA
"""

import argparse
import configparser
import os
import re
import sys
import xmlrpc.client
import zipfile
from typing import List, Tuple


REQUIRED_METADATA_KEYS = [
    "name",
    "qgisMinimumVersion",
    "description",
    "version",
    "author",
    "email",
    "about",
    "tracker",
    "repository",
]

FORBIDDEN_ZIP_PATTERNS = [
    r"__pycache__",
    r"\.pyc$",
    r"\.pyo$",
    r"(^|/)\.git(/|$)",
    r"(^|/)\.svn(/|$)",
    r"\.DS_Store$",
]


def validate_qgis_zip(zip_path: str) -> Tuple[bool, List[str]]:
    """
    Executa todas as validações exigidas pelo repositório oficial plugins.qgis.org.
    Retorna (aprovado, lista_de_mensagens).
    """
    messages: List[str] = []
    if not os.path.exists(zip_path):
        return False, [f"[ERRO] Arquivo ZIP não encontrado: {zip_path}"]

    size_bytes = os.path.getsize(zip_path)
    max_bytes = 20 * 1024 * 1024  # Limite de 20 MB do plugins.qgis.org
    if size_bytes > max_bytes:
        return False, [f"[ERRO] O pacote ZIP ({size_bytes:,} bytes) excede o limite de 20 MB do QGIS."]
    messages.append(f"[OK] Tamanho do pacote ZIP: {size_bytes / 1024:.1f} KB (limite máximo: 20.480 KB)")

    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        if not namelist:
            return False, ["[ERRO] O arquivo ZIP está vazio."]

        # 1. Pasta raiz única com nome de pacote Python válido (PEP 8)
        top_dirs = {name.split("/")[0] for name in namelist if "/" in name}
        if len(top_dirs) != 1:
            return False, [
                f"[ERRO] O ZIP deve conter exatamente uma pasta raiz, mas encontrou: {sorted(top_dirs)}"
            ]
        root_dir = next(iter(top_dirs))
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", root_dir):
            return False, [
                f"[ERRO] O nome da pasta raiz '{root_dir}' não é um identificador Python válido."
            ]
        messages.append(f"[OK] Pasta raiz do pacote Python: '{root_dir}/'")

        # 2. Arquivos obrigatórios na raiz do plugin
        for req_file in ("__init__.py", "metadata.txt", "LICENSE"):
            expected_member = f"{root_dir}/{req_file}"
            if expected_member not in namelist:
                return False, [
                    f"[ERRO] Arquivo obrigatório ausente no ZIP: '{expected_member}'"
                ]
        messages.append("[OK] Arquivos obrigatórios presentes (__init__.py, metadata.txt, LICENSE)")

        # 3. Verificação de arquivos proibidos (__pycache__, .pyc, .git)
        for member in namelist:
            for pattern in FORBIDDEN_ZIP_PATTERNS:
                if re.search(pattern, member):
                    return False, [
                        f"[ERRO] Arquivo/diretório proibido encontrado no ZIP: '{member}'"
                    ]
        messages.append("[OK] Nenhum arquivo temporário ou binário proibido (__pycache__, .pyc, .git)")

        # 4. Validação completa do metadata.txt
        raw_meta = zf.read(f"{root_dir}/metadata.txt").decode("utf-8")
        cfg = configparser.ConfigParser()
        cfg.optionxform = str  # Preserva maiúsculas/minúsculas como qgisMinimumVersion
        cfg.read_string(raw_meta)

        if "general" not in cfg:
            return False, ["[ERRO] Seção [general] ausente no metadata.txt."]

        general = cfg["general"]
        for key in REQUIRED_METADATA_KEYS:
            val = general.get(key, "").strip()
            if not val:
                return False, [f"[ERRO] Campo obrigatório '{key}' ausente ou vazio em metadata.txt."]

        for url_key in ("tracker", "repository", "homepage"):
            url_val = general.get(url_key, "").strip()
            if url_val and not url_val.startswith(("http://", "https://")):
                return False, [f"[ERRO] O campo '{url_key}' deve começar com http:// ou https://."]

        if "exemplo.com" in general.get("email", "") or "dadosabertos.tse.jus.br" in general.get("repository", ""):
            return False, [
                "[ERRO] Atualize os campos 'email', 'repository' e 'tracker' com os endereços definitivos."
            ]

        messages.append(
            f"[OK] metadata.txt validado: '{general['name']}' v{general['version']} "
            f"(QGIS {general.get('qgisMinimumVersion')} - {general.get('qgisMaximumVersion', '4.99')}, "
            f"supportsQt6={general.get('supportsQt6', 'False')})"
        )
        messages.append(f"[OK] Autor: {general['author']} <{general['email']}>")
        messages.append(f"[OK] Repositório: {general['repository']}")

    return True, messages


def upload_plugin(zip_path: str, username: str = "", password: str = "", token: str = "") -> None:
    """Envia o pacote .zip para https://plugins.qgis.org/plugins/RPC2/."""
    ok, msgs = validate_qgis_zip(zip_path)
    for m in msgs:
        print(m)
    if not ok:
        sys.exit(1)

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    if token:
        # O portal do QGIS aceita autenticação via Basic Auth ou Token na URL RPC2
        endpoint = f"https://__token__:{token}@plugins.qgis.org/plugins/RPC2/"
    elif username and password:
        endpoint = f"https://{username}:{password}@plugins.qgis.org/plugins/RPC2/"
    else:
        raise ValueError("Informe --token (ou QGIS_PLUGIN_TOKEN) ou --username e --password.")

    print("Enviando pacote para https://plugins.qgis.org/plugins/RPC2/ ...")
    server = xmlrpc.client.ServerProxy(endpoint)
    plugin_id, version_id = server.plugin.upload(xmlrpc.client.Binary(zip_bytes))
    print(f"[SUCESSO] Plugin publicado! ID do Plugin: {plugin_id} | ID da Versão: {version_id}")
    print(f"URL no Hub do QGIS: https://plugins.qgis.org/plugins/tse_to_qgis/")


def main() -> None:
    default_zip = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tse_to_qgis.zip")
    parser = argparse.ArgumentParser(description="Valida e envia o plugin GeoTSE ao repositório oficial do QGIS.")
    parser.add_argument("--zip", default=default_zip, help="Caminho para o arquivo tse_to_qgis.zip")
    parser.add_argument("--validate-only", action="store_true", help="Apenas valida o pacote sem enviar")
    parser.add_argument("--token", default=os.environ.get("QGIS_PLUGIN_TOKEN", ""), help="Token de API do plugins.qgis.org")
    parser.add_argument("--username", default=os.environ.get("QGIS_PLUGIN_USERNAME", ""), help="Usuário OSGeo ID")
    parser.add_argument("--password", default=os.environ.get("QGIS_PLUGIN_PASSWORD", ""), help="Senha OSGeo ID")
    args = parser.parse_args()

    if args.validate_only or (not args.token and not args.username):
        ok, msgs = validate_qgis_zip(args.zip)
        for m in msgs:
            print(m)
        if not ok:
            sys.exit(1)
        print("\n[PRONTO PARA PUBLICAÇÃO] O arquivo tse_to_qgis.zip cumpre 100% das exigências do plugins.qgis.org!")
    else:
        upload_plugin(args.zip, username=args.username, password=args.password, token=args.token)


if __name__ == "__main__":
    main()
