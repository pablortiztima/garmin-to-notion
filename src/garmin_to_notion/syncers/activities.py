"""Sincroniza actividades de Garmin con la base "Sesiones y eventos" de Notion.

Versión adaptada: escribe en una base en español y vincula cada actividad
con su implemento (bicicleta, zapatillas...) de la base "Implementos",
buscando por el nombre que el equipo tiene en Garmin Connect.

Reglas:
- Una actividad nueva se crea con Tipo = Entrenamiento y Estado = Realizada.
- Las actividades que ya existen (mismo Garmin ID) solo actualizan sus datos
  técnicos. Nunca se tocan Tipo, Estado, Notas, Esfuerzo ni el nombre, por si
  los editaste a mano. El implemento solo se completa si está vacío.
- Variables opcionales:
    NOTION_GEAR_DB_ID  base de Implementos (si falta, no se vinculan equipos)
    SYNC_FROM          fecha AAAA-MM-DD; se ignoran actividades anteriores
"""

from __future__ import annotations

import logging
import os
from datetime import date

from garminconnect import Garmin as GarminClient
from notion_client import Client as NotionClient

from garmin_to_notion.config import Settings
from garmin_to_notion.formatters import (
    format_activity_type,
    format_effect_rich,
    gmt_to_local,
)
from garmin_to_notion.mappings import ACTIVITY_EMOJIS
from garmin_to_notion.notion_helpers import fetch_all_pages

logger = logging.getLogger(__name__)

# Tipo principal de Garmin -> Disciplina en Notion
DISCIPLINE_MAP = {
    "Running": "Running",
    "Cycling": "Bicicleta",
    "Swimming": "Natación",
    "Strength": "Fitness",
    "HIIT": "Fitness",
    "Cardio": "Fitness",
    "Yoga/Pilates": "Fitness",
    "Stretching": "Fitness",
}


def _discipline(type_key: str, main_type: str) -> str:
    """Disciplina a partir del typeKey de Garmin (ej. road_biking, lap_swimming)."""
    key = (type_key or "").lower()
    if "swim" in key:
        return "Natación"
    if any(k in key for k in ("cycling", "biking", "ride", "bike", "cyclocross")):
        return "Bicicleta"
    if "running" in key or key == "run":
        return "Running"
    return DISCIPLINE_MAP.get(main_type, "Otro")


def _num(value, digits: int = 0):
    """Número redondeado, o None si viene vacío o en cero (deja la celda vacía)."""
    if value in (None, 0, 0.0):
        return None
    return round(value, digits) if digits else round(value)


def _text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}] if value else []}


def _cadence(activity: dict, discipline: str):
    if discipline == "Running":
        return _num(activity.get("averageRunningCadenceInStepsPerMinute"))
    if discipline == "Bicicleta":
        return _num(activity.get("averageBikingCadenceInRevPerMinute"))
    if discipline == "Natación":
        return _num(activity.get("averageSwimCadenceInStrokesPerMinute"))
    return None


def _metric_properties(activity: dict, settings: Settings) -> dict:
    """Datos técnicos que vienen de Garmin (se actualizan en cada sync)."""
    name = activity.get("activityName") or "Actividad"
    main_type, subtype = format_activity_type(
        activity.get("activityType", {}).get("typeKey", "Unknown"), name
    )
    discipline = _discipline(activity.get("activityType", {}).get("typeKey", ""), main_type)
    local_date = gmt_to_local(activity.get("startTimeGMT"), settings.timezone)
    distance_km = (activity.get("distance") or 0) / 1000
    duration_s = activity.get("duration") or 0

    aerobic = activity.get("aerobicTrainingEffect") or 0
    anaerobic = activity.get("anaerobicTrainingEffect") or 0
    effect = ""
    if aerobic or anaerobic:
        effect = (
            "Aeróbico " + format_effect_rich(aerobic, activity.get("aerobicTrainingEffectMessage", ""))
            + " · Anaeróbico " + format_effect_rich(anaerobic, activity.get("anaerobicTrainingEffectMessage", ""))
        )

    return {
        "Fecha": {"date": {"start": local_date.isoformat()}},
        "Disciplina": {"select": {"name": discipline}},
        "Subtipo Garmin": _text(subtype),
        "Duración (min)": {"number": _num(duration_s / 60, 1)},
        "Distancia (km)": {"number": _num(distance_km, 2)},
        "FC media (ppm)": {"number": _num(activity.get("averageHR"))},
        "FC máx (ppm)": {"number": _num(activity.get("maxHR"))},
        "Desnivel + (m)": {"number": _num(activity.get("elevationGain"))},
        "Cadencia media": {"number": _cadence(activity, discipline)},
        "Potencia media (W)": {"number": _num(activity.get("avgPower"))},
        "Calorías": {"number": _num(activity.get("calories"))},
        "Efecto de entrenamiento": _text(effect),
        "Garmin ID": {"number": activity.get("activityId")},
        "Enlace Garmin": {"url": f"https://connect.garmin.com/modern/activity/{activity.get('activityId')}"},
    }


def _icon(activity: dict) -> dict:
    name = activity.get("activityName", "")
    _, subtype = format_activity_type(
        activity.get("activityType", {}).get("typeKey", "Unknown"), name
    )
    return {"emoji": ACTIVITY_EMOJIS.get(subtype, ACTIVITY_EMOJIS["Other"])}


def _load_gear_index(notion: NotionClient, gear_db_id: str | None) -> dict[str, str]:
    """{nombre del implemento en minúsculas: page_id} desde la base Implementos."""
    if not gear_db_id:
        return {}
    index = {}
    for page in fetch_all_pages(notion, gear_db_id):
        for prop in page["properties"].values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                if title.strip():
                    index[title.strip().lower()] = page["id"]
    logger.info("Implementos en Notion: %d", len(index))
    return index


def _gear_relation(garmin: GarminClient, activity_id, gear_index: dict[str, str]) -> list[dict]:
    """Busca el equipo usado en Garmin y lo cruza por nombre con Implementos."""
    if not gear_index or not activity_id:
        return []
    try:
        gear_list = garmin.get_activity_gear(activity_id) or []
    except Exception as e:  # el equipo es opcional; nunca detiene la sync
        logger.debug("Sin equipo para %s: %s", activity_id, e)
        return []
    related = []
    for gear in gear_list if isinstance(gear_list, list) else []:
        for key in ("displayName", "customMakeModel"):
            name = (gear.get(key) or "").strip().lower()
            if name and name in gear_index:
                related.append({"id": gear_index[name]})
                break
        else:
            logger.info(
                "Equipo de Garmin '%s' no coincide con ningún implemento en Notion",
                gear.get("displayName") or gear.get("customMakeModel"),
            )
    return related


def _existing_by_garmin_id(notion: NotionClient, database_id: str) -> dict[int, dict]:
    pages = fetch_all_pages(
        notion, database_id,
        filter={"property": "Garmin ID", "number": {"is_not_empty": True}},
    )
    return {
        int(p["properties"]["Garmin ID"]["number"]): p
        for p in pages
        if p["properties"].get("Garmin ID", {}).get("number") is not None
    }


def _changed(existing: dict, new_props: dict) -> bool:
    props = existing["properties"]
    for key in ("Distancia (km)", "Duración (min)", "FC media (ppm)", "Calorías"):
        if props.get(key, {}).get("number") != new_props[key]["number"]:
            return True
    return False


def sync_activities(
    garmin: GarminClient,
    notion: NotionClient,
    settings: Settings,
) -> None:
    """Sincroniza las actividades de Garmin con "Sesiones y eventos"."""
    db_id = settings.activities_db_id
    gear_index = _load_gear_index(notion, os.getenv("NOTION_GEAR_DB_ID"))
    sync_from = os.getenv("SYNC_FROM", "").strip()
    since = date.fromisoformat(sync_from) if sync_from else None

    activities = garmin.get_activities(0, settings.fetch_limit)
    logger.info("Actividades obtenidas de Garmin: %d", len(activities))
    existing = _existing_by_garmin_id(notion, db_id)

    created = updated = skipped = 0
    for activity in activities:
        activity_id = activity.get("activityId")
        local_date = gmt_to_local(activity.get("startTimeGMT"), settings.timezone)
        if since and local_date.date() < since:
            skipped += 1
            continue

        props = _metric_properties(activity, settings)
        page = existing.get(int(activity_id)) if activity_id else None

        if page:
            if not page["properties"].get("Implementos", {}).get("relation"):
                gear = _gear_relation(garmin, activity_id, gear_index)
                if gear:
                    props["Implementos"] = {"relation": gear}
            elif not _changed(page, props):
                skipped += 1
                continue
            if "Implementos" in props or _changed(page, props):
                notion.pages.update(page_id=page["id"], properties=props)
                updated += 1
            else:
                skipped += 1
            continue

        props["Sesión"] = {"title": [{"text": {"content": activity.get("activityName") or "Actividad"}}]}
        props["Tipo"] = {"select": {"name": "Entrenamiento"}}
        props["Estado"] = {"select": {"name": "Realizada"}}
        gear = _gear_relation(garmin, activity_id, gear_index)
        if gear:
            props["Implementos"] = {"relation": gear}
        notion.pages.create(
            parent={"database_id": db_id},
            properties=props,
            icon=_icon(activity),
        )
        created += 1

    logger.info(
        "Sync terminada: %d nuevas, %d actualizadas, %d sin cambios u omitidas",
        created, updated, skipped,
    )
