#!/usr/bin/env python3
"""Diagnóstico rápido: prueba varias URLs candidatas para "crear servicio
combinado" en la API de Northflank y reporta qué método/ruta responde con
algo distinto de 404/405-solo-lectura, para confirmar la ruta real antes
de corregir el script principal."""
import getpass
import requests

token = getpass.getpass("Token: ")
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
project_id = "ggal-bot"

candidates = [
    f"/v1/projects/{project_id}/services/combined",
    f"/v1/projects/{project_id}/services/combined-service",
    f"/v1/projects/{project_id}/combined-services",
    f"/v1/project/{project_id}/services",
    f"/v1/project/{project_id}/services/combined",
    f"/v1/projects/{project_id}/services?type=combined",
]

for path in candidates:
    url = f"https://api.northflank.com{path}"
    try:
        r = requests.post(url, headers=headers, json={}, timeout=10)
        print(f"{path}\n  -> status={r.status_code} allow={r.headers.get('Allow')} body={r.text[:150]!r}\n")
    except Exception as e:
        print(f"{path}\n  -> EXCEPTION: {e}\n")
