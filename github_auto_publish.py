# -*- coding: utf-8 -*-
"""
Publicador 100% automatizado do repositório 'duduluiss/geotse-qgis' no GitHub
sem precisar ter o Git instalado na máquina!

Funcionalidades:
  1. Autentica via GitHub OAuth Device Flow (abre o navegador em github.com/login/device)
  2. Cria automaticamente o repositório público 'geotse-qgis' na conta 'duduluiss' (se ainda não existir)
  3. Envia toda a árvore de arquivos do plugin em um commit limpo na branch 'main' via Git Data API
  4. Cria a Release 'v1.0.0' e anexa o pacote 'tse_to_qgis.zip' pronto para o QGIS!
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Dict, List, Optional, Tuple


# Client ID oficial do GitHub CLI para OAuth Device Flow
GITHUB_CLI_CLIENT_ID = "178c6fc778ccc68e1d6a"
REPO_OWNER = "duduluiss"
REPO_NAME = "geotse-qgis"
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _http_json(
    url: str,
    method: str = "GET",
    data: Optional[Dict] = None,
    token: str = "",
    headers_extra: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GeoTSE-QGIS-Publisher/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers_extra:
        headers.update(headers_extra)

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw_err)
        except Exception:
            return e.code, {"message": raw_err}
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(e.reason, ssl.SSLError):
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        raise


def request_device_code() -> Dict:
    status, data = _http_json(
        "https://github.com/login/device/code",
        method="POST",
        data={
            "client_id": GITHUB_CLI_CLIENT_ID,
            "scope": "repo workflow",
        },
    )
    if status != 200 or "device_code" not in data:
        raise RuntimeError(f"Erro ao iniciar Device Flow no GitHub ({status}): {data}")
    return data


def poll_for_token(device_code: str, interval: int = 5, expires_in: int = 900) -> str:
    deadline = time.time() + expires_in
    wait_sec = max(5, interval)
    while time.time() < deadline:
        time.sleep(wait_sec)
        status, data = _http_json(
            "https://github.com/login/oauth/access_token",
            method="POST",
            data={
                "client_id": GITHUB_CLI_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if "access_token" in data:
            return data["access_token"]
        err = data.get("error", "")
        if err == "authorization_pending":
            continue
        elif err == "slow_down":
            wait_sec += 5
            continue
        else:
            raise RuntimeError(f"Falha na autorização OAuth do GitHub: {data}")
    raise TimeoutError("Tempo de autorização no GitHub expirou.")


def collect_repo_files() -> List[Tuple[str, bytes]]:
    """Lista todos os arquivos do projeto que devem ir para o repositório Git."""
    files_to_upload: List[Tuple[str, bytes]] = []
    skip_dirs = {"__pycache__", ".git", ".pytest_cache", ".vscode", ".idea"}
    skip_files = {"tse_to_qgis.zip", ".device_state.json"}

    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(files):
            if fname in skip_files or fname.endswith((".pyc", ".pyo", ".gpkg")):
                continue
            abs_p = os.path.join(root, fname)
            rel_p = os.path.relpath(abs_p, PROJECT_ROOT).replace("\\", "/")
            with open(abs_p, "rb") as f:
                files_to_upload.append((rel_p, f.read()))
    return files_to_upload


def publish_to_github(token: str) -> None:
    # 1. Verifica usuário autenticado
    st, user_info = _http_json("https://api.github.com/user", token=token)
    if st != 200:
        raise RuntimeError(f"Token inválido ({st}): {user_info}")
    owner = user_info.get("login", REPO_OWNER)
    print(f"[OK] Autenticado no GitHub como: {owner}")

    # 2. Verifica ou cria o repositório 'geotse-qgis'
    st_repo, repo_data = _http_json(
        f"https://api.github.com/repos/{owner}/{REPO_NAME}", token=token
    )
    if st_repo == 404:
        print(f"Criando repositório público https://github.com/{owner}/{REPO_NAME} ...")
        st_create, repo_data = _http_json(
            "https://api.github.com/user/repos",
            method="POST",
            token=token,
            data={
                "name": REPO_NAME,
                "description": "GeoTSE - Espacializador Eleitoral: Complemento para QGIS 3 e QGIS 4 para importar, agregar e espacializar todos os dados abertos do TSE (Municípios IBGE e Locais de Votação).",
                "homepage": f"https://github.com/{owner}/{REPO_NAME}",
                "private": False,
                "has_issues": True,
                "auto_init": True,
            },
        )
        if st_create not in (200, 201):
            raise RuntimeError(f"Falha ao criar repositório ({st_create}): {repo_data}")
        time.sleep(2)
        print(f"[OK] Repositório criado: https://github.com/{owner}/{REPO_NAME}")
    else:
        print(f"[OK] Repositório encontrado: https://github.com/{owner}/{REPO_NAME}")

    # 3. Garante que existe ao menos um commit inicial (caso o repo tenha sido criado vazio pelo usuário)
    st_ref, ref_data = _http_json(
        f"https://api.github.com/repos/{owner}/{REPO_NAME}/git/ref/heads/main", token=token
    )
    if st_ref != 200:
        # Inicializa a branch main usando a Contents API com o README.md
        with open(os.path.join(PROJECT_ROOT, "README.md"), "rb") as rf:
            readme_b64 = base64.b64encode(rf.read()).decode("ascii")
        st_init, _ = _http_json(
            f"https://api.github.com/repos/{owner}/{REPO_NAME}/contents/README.md",
            method="PUT",
            token=token,
            data={
                "message": "Initial commit",
                "content": readme_b64,
                "branch": "main",
            },
        )
        time.sleep(1)
        st_ref, ref_data = _http_json(
            f"https://api.github.com/repos/{owner}/{REPO_NAME}/git/ref/heads/main", token=token
        )
        if st_ref != 200:
            raise RuntimeError(f"Não foi possível obter a ref heads/main ({st_ref}): {ref_data}")

    base_commit_sha = ref_data["object"]["sha"]

    # 4. Cria Blobs para todos os arquivos do projeto
    repo_files = collect_repo_files()
    tree_items = []
    print(f"Enviando {len(repo_files)} arquivos para o GitHub...")
    for rel_path, content_bytes in repo_files:
        b64_str = base64.b64encode(content_bytes).decode("ascii")
        st_blob, blob_data = _http_json(
            f"https://api.github.com/repos/{owner}/{REPO_NAME}/git/blobs",
            method="POST",
            token=token,
            data={"content": b64_str, "encoding": "base64"},
        )
        if st_blob not in (200, 201):
            raise RuntimeError(f"Erro ao enviar arquivo {rel_path} ({st_blob}): {blob_data}")
        tree_items.append(
            {
                "path": rel_path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_data["sha"],
            }
        )

    # 5. Cria a Git Tree e o Commit na branch 'main'
    st_tree, tree_data = _http_json(
        f"https://api.github.com/repos/{owner}/{REPO_NAME}/git/trees",
        method="POST",
        token=token,
        data={"tree": tree_items},
    )
    if st_tree not in (200, 201):
        raise RuntimeError(f"Erro ao criar Git Tree ({st_tree}): {tree_data}")

    st_commit, commit_data = _http_json(
        f"https://api.github.com/repos/{owner}/{REPO_NAME}/git/commits",
        method="POST",
        token=token,
        data={
            "message": "Release v1.0.0 - GeoTSE - Espacializador Eleitoral (QGIS 3 & QGIS 4)",
            "tree": tree_data["sha"],
            "parents": [base_commit_sha],
        },
    )
    if st_commit not in (200, 201):
        raise RuntimeError(f"Erro ao criar Commit ({st_commit}): {commit_data}")

    new_commit_sha = commit_data["sha"]
    st_upd, upd_data = _http_json(
        f"https://api.github.com/repos/{owner}/{REPO_NAME}/git/refs/heads/main",
        method="PATCH",
        token=token,
        data={"sha": new_commit_sha, "force": True},
    )
    if st_upd != 200:
        raise RuntimeError(f"Erro ao atualizar branch main ({st_upd}): {upd_data}")
    print(f"[OK] Código-fonte publicado em https://github.com/{owner}/{REPO_NAME} (commit {new_commit_sha[:7]})")

    # 6. Cria a Release v1.0.0 e anexa o arquivo tse_to_qgis.zip
    st_rel, rel_data = _http_json(
        f"https://api.github.com/repos/{owner}/{REPO_NAME}/releases/tags/v1.0.0",
        token=token,
    )
    if st_rel == 404:
        st_rel, rel_data = _http_json(
            f"https://api.github.com/repos/{owner}/{REPO_NAME}/releases",
            method="POST",
            token=token,
            data={
                "tag_name": "v1.0.0",
                "target_commitish": "main",
                "name": "v1.0.0 - GeoTSE - Espacializador Eleitoral",
                "body": (
                    "Versão 1.0.0 oficial do **GeoTSE - Espacializador Eleitoral** para QGIS 3.28+ e QGIS 4.x (`PyQt6`).\n\n"
                    "- Pacote `tse_to_qgis.zip` pronto para instalação direta no QGIS ou submissão em `plugins.qgis.org`."
                ),
                "draft": False,
                "prerelease": False,
            },
        )
    if st_rel in (200, 201) and "id" in rel_data:
        rel_id = rel_data["id"]
        zip_path = os.path.join(PROJECT_ROOT, "tse_to_qgis.zip")
        if os.path.exists(zip_path):
            with open(zip_path, "rb") as zf:
                zip_bytes = zf.read()
            upload_url = (
                f"https://uploads.github.com/repos/{owner}/{REPO_NAME}/releases/{rel_id}/assets"
                f"?name=tse_to_qgis.zip"
            )
            req = urllib.request.Request(
                upload_url,
                data=zip_bytes,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/zip",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "GeoTSE-QGIS-Publisher/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    pass
                print(f"[OK] Pacote tse_to_qgis.zip anexado à Release v1.0.0!")
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    print("[OK] Release v1.0.0 já possui o arquivo tse_to_qgis.zip anexado.")
                else:
                    print(f"[AVISO] Não foi possível anexar o ZIP à release ({e.code}).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    state_file = os.path.join(PROJECT_ROOT, ".device_state.json")

    if cmd == "start":
        dev = request_device_code()
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(dev, f)
        url = dev.get("verification_uri", "https://github.com/login/device")
        code = dev.get("user_code", "")
        print(f"USER_CODE={code}")
        print(f"VERIFICATION_URI={url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass
    elif cmd == "complete":
        with open(state_file, "r", encoding="utf-8") as f:
            dev = json.load(f)
        token = poll_for_token(
            dev["device_code"],
            interval=int(dev.get("interval", 5)),
            expires_in=int(dev.get("expires_in", 900)),
        )
        if os.path.exists(state_file):
            os.remove(state_file)
        publish_to_github(token)
    elif cmd == "token" and len(sys.argv) > 2:
        publish_to_github(sys.argv[2])
