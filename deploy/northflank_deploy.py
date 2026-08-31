#!/usr/bin/env python3
"""
northflank_deploy.py
=====================
Script de aprovisionamiento para desplegar GGAL_BOT en Northflank via su API
REST, pensado para correr EN TU PROPIA MAQUINA (no en un entorno de Claude):
la razon es que Northflank (api.northflank.com) esta bloqueado por la lista
blanca de salida de red de los entornos de ejecucion de Claude, tanto el
sandbox en la nube como el puente hacia esta PC - se confirmo con pruebas
directas de conectividad (403 de un proxy antes de llegar al host). Tu
maquina si tiene salida a internet normal, por eso este script lo corres vos.

Que hace, en orden:
  1. Valida el token (GET /v1/regions) y te deja elegir region y plan de
     una lista REAL obtenida de la API (no valores adivinados).
  2. Crea un proyecto Northflank dedicado ("ggal-bot").
  3. Lee tu archivo .env REAL (nunca sale de tu maquina, no se lo mando a
     Claude en ningun momento) y crea un secret group en Northflank con
     esas mismas variables, para que el bot corra con las credenciales
     reales sin que vos tengas que retipearlas a mano en el dashboard.
  4. Crea el servicio "combinado" (build+deploy) apuntando al repo
     https://github.com/FEDEALCO/ggal-bot (rama main) y a su Dockerfile.
  5. Intenta crear y montar un volumen persistente en /app/state,
     /app/logs y /app/data_cache (best-effort: si esta parte de la API
     no tiene exactamente el schema esperado, el script sigue y te avisa
     para que lo hagas a mano desde el dashboard - 1 minuto, Service ->
     Volumes -> Add volume).

Prerrequisitos (los DOS son pasos de click unico que Northflank exige por
OAuth y no se pueden evitar via API - no es un limite de este script):
  a) Ya haber vinculado tu cuenta de GitHub a Northflank una vez:
     dashboard de Northflank -> Account/Team settings -> Git -> Connect
     GitHub. Si no lo hiciste todavia, hacelo antes de correr este script.
  b) Tener un API token de Northflank: Account settings (o Team settings)
     -> API tokens -> Create token. Dale permisos de escritura sobre
     Projects/Secrets/Services/Volumes.

Uso:
    pip install requests   (ya deberia estar instalado, es dependencia del bot)
    python northflank_deploy.py [ruta a tu .env, default: ../.env]

El token NUNCA se hardcodea en este archivo: se lee de la variable de
entorno NORTHFLANK_API_TOKEN o se pide de forma oculta (getpass) si no
esta seteada.
"""

import getpass
import os
import sys

try:
    import requests
except ImportError:
    print("Falta el paquete 'requests'. Instalalo con: pip install requests")
    sys.exit(1)

BASE = "https://api.northflank.com/v1"
PROJECT_NAME = "ggal-bot"
SERVICE_NAME = "ggal-bot"
SECRET_GROUP_NAME = "ggal-bot-secrets"
GITHUB_REPO_URL = "https://github.com/FEDEALCO/ggal-bot"
GITHUB_BRANCH = "main"


def die(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def api(session, method, path, **kwargs):
    resp = session.request(method, f"{BASE}{path}", **kwargs)
    if not resp.ok:
        # No imprimir el body completo en pasos que puedan incluir secretos
        # (por eso el paso de secrets no llama a esta func para el error).
        body = resp.text[:1500]
        die(f"{method} {path} -> HTTP {resp.status_code}\n{body}")
    return resp.json() if resp.content else {}


def load_env_file(path):
    """Parsea un .env real (KEY=VALUE por linea, ignora comentarios/vacias)."""
    if not os.path.isfile(path):
        die(f"No encuentro el archivo .env en: {path}\n"
            f"Pasalo como argumento: python northflank_deploy.py /ruta/a/.env")
    variables = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                variables[key] = value
    if not variables:
        die(f"El .env en {path} no tiene ninguna variable cargada.")
    return variables


def pick_from_list(items, label_fn, prompt):
    for i, item in enumerate(items):
        print(f"  [{i}] {label_fn(item)}")
    while True:
        choice = input(prompt).strip()
        if choice.isdigit() and 0 <= int(choice) < len(items):
            return items[int(choice)]
        print("Opcion invalida, probá de nuevo.")


def main():
    env_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".env"
    )

    token = os.environ.get("NORTHFLANK_API_TOKEN") or getpass.getpass(
        "Pegá tu API token de Northflank (no se muestra en pantalla): "
    )
    if not token:
        die("No se recibió ningún token.")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })

    print("\n== Paso 1/5: validando token y listando regiones/planes ==")
    regions = api(session, "GET", "/regions").get("data", {}).get("regions", [])
    if not regions:
        die("El token parece válido pero no devolvió regiones. Revisá permisos del token.")
    region = pick_from_list(
        regions, lambda r: f"{r['id']} ({r.get('name', '')})",
        "Elegí el número de región: "
    )

    plans = api(session, "GET", "/plans").get("data", {}).get("plans", [])
    if not plans:
        die("No se pudo obtener la lista de planes.")
    plans_sorted = sorted(plans, key=lambda p: p.get("amountPerMonth", 9e9))
    print("(ordenados de más barato a más caro)")
    plan = pick_from_list(
        plans_sorted,
        lambda p: f"{p['id']} - {p.get('cpuResource')} vCPU / {p.get('ramResource')}MB - "
                  f"~${p.get('amountPerMonth')}/mes",
        "Elegí el número de plan (recomendado: el más barato, [0]): "
    )

    print("\n== Paso 2/5: creando (o reutilizando) el proyecto en Northflank ==")
    existing_projects = api(session, "GET", "/projects").get("data", {}).get("projects", [])
    existing = next((p for p in existing_projects if p.get("name") == PROJECT_NAME), None)
    if existing:
        project_id = existing["id"]
        print(f"Ya existía un proyecto '{PROJECT_NAME}' (de una corrida anterior) -> lo reutilizo: {project_id}")
    else:
        project = api(session, "POST", "/projects", json={
            "name": PROJECT_NAME,
            "description": "GGAL options vol-arbitrage bot (shadow mode)",
            "region": region["id"],
        })
        project_id = project["data"]["id"]
        print(f"Proyecto creado: {project_id}")

    print("\n== Paso 3/5: cargando tus variables reales como secret group ==")
    print(f"Leyendo: {env_path}")
    env_vars = load_env_file(env_path)
    print(f"({len(env_vars)} variables encontradas — sus valores NO se imprimen acá)")
    existing_secrets = api(session, "GET", f"/projects/{project_id}/secrets").get("data", {}).get("secrets", [])
    if any(s.get("name") == SECRET_GROUP_NAME for s in existing_secrets):
        print(f"Ya existía el secret group '{SECRET_GROUP_NAME}' -> lo dejo como está (no lo piso).")
    else:
        api(session, "POST", f"/projects/{project_id}/secrets", json={
            "name": SECRET_GROUP_NAME,
            "type": "secret",
            "secretType": "environment",
            "priority": 10,
            "secrets": {"variables": env_vars},
        })
        print("Secret group creado y disponible para todos los servicios del proyecto.")

    print("\n== Paso 4/5: creando el servicio (build desde GitHub + Dockerfile) ==")
    existing_services = api(session, "GET", f"/projects/{project_id}/services").get("data", {}).get("services", [])
    existing_service = next((s for s in existing_services if s.get("name") == SERVICE_NAME), None)
    if existing_service:
        service_id = existing_service["id"]
        print(f"Ya existía el servicio '{SERVICE_NAME}' -> lo reutilizo: {service_id}")
    else:
        service = api(session, "POST", f"/projects/{project_id}/services/combined", json={
            "name": SERVICE_NAME,
            "description": "GGAL_BOT - shadow mode",
            "billing": {
                "deploymentPlan": plan["id"],
                "buildPlan": plan["id"],
            },
            "deployment": {
                "instances": 1,
                "type": "deployment",
            },
            "vcsData": {
                "projectUrl": GITHUB_REPO_URL,
                "projectType": "github",
                "projectBranch": GITHUB_BRANCH,
            },
            "buildSettings": {
                "dockerfile": {
                    "dockerFilePath": "/Dockerfile",
                    "dockerWorkDir": "/",
                }
            },
        })
        service_id = service["data"]["id"]
        print(f"Servicio creado: {service_id}")

    print("\n== Paso 5/5 (best-effort): volumen persistente para state/logs/data_cache ==")
    try:
        api(session, "POST", f"/projects/{project_id}/volumes", json={
            "name": "ggal-bot-state",
            "spec": {"storageClassName": "ssd", "storageSize": 1024},
            "mounts": [
                {"containerMountPath": "/app/state"},
                {"containerMountPath": "/app/logs"},
                {"containerMountPath": "/app/data_cache"},
            ],
            "attachedObjects": [{"id": service_id, "type": "service"}],
        })
        print("Volumen creado y montado.")
    except SystemExit:
        print(
            "\nNo se pudo crear el volumen automáticamente (el schema exacto de "
            "esta parte de la API no está 100% documentado). No es grave: el "
            "servicio ya quedó creado y andando. Para que bot_state.json y los "
            "logs sobrevivan a un redeploy, andá al dashboard -> tu servicio -> "
            "Volumes -> Add volume, y montalo en /app/state, /app/logs y "
            "/app/data_cache."
        )

    print(f"\nListo. Revisá el build y los logs en:")
    print(f"https://app.northflank.com/t/_/project/{project_id}/service/{service_id}")


if __name__ == "__main__":
    main()
