import os
import hmac
import hashlib
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import boto3
import httpx
import psycopg
from psycopg.rows import dict_row
import redis.asyncio as redis
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-agent")

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
API_URL = os.getenv("API_URL", "http://calcom-api:80").rstrip("/")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "bedrock_haiku_token_2026")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

redis_client = redis.from_url(REDIS_URL, decode_responses=True)
bedrock = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))

DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_available_slots",
                "description": (
                    "Consulta los horarios disponibles reales en el calendario para un rango de fechas. "
                    "Úsala siempre antes de ofrecer horarios, agendar o reagendar una cita."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "start_date": {
                                "type": "string",
                                "description": "Fecha inicial en formato YYYY-MM-DD (zona horaria del negocio)."
                            },
                            "end_date": {
                                "type": "string",
                                "description": "Fecha final en formato YYYY-MM-DD (máximo 7 días después de start_date)."
                            }
                        },
                        "required": ["start_date", "end_date"]
                    }
                }
            }
        },
        {
            "toolSpec": {
                "name": "create_booking",
                "description": (
                    "Crea una cita confirmada en Cal.com. Requiere haber validado primero la disponibilidad "
                    "y contar con nombre completo y correo electrónico del cliente."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "start_time": {
                                "type": "string",
                                "description": "Fecha y hora exacta devuelta por get_available_slots en formato ISO 8601 (ej. 2026-11-20T09:00:00.000-06:00)."
                            },
                            "attendee_name": {
                                "type": "string",
                                "description": "Nombre completo del cliente."
                            },
                            "attendee_email": {
                                "type": "string",
                                "description": "Correo electrónico válido del cliente."
                            }
                        },
                        "required": ["start_time", "attendee_name", "attendee_email"]
                    }
                }
            }
        },
        {
            "toolSpec": {
                "name": "reschedule_booking",
                "description": (
                    "Reagenda una cita existente a un nuevo horario usando su booking_uid. "
                    "Debes consultar primero get_available_slots para verificar que el nuevo horario esté libre."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "booking_uid": {
                                "type": "string",
                                "description": "Identificador único (uid) de la reserva actual a reagendar."
                            },
                            "new_start_time": {
                                "type": "string",
                                "description": "Nueva fecha y hora exacta devuelta por get_available_slots en formato ISO 8601 (ej. 2026-11-23T11:00:00.000-06:00)."
                            },
                            "reason": {
                                "type": "string",
                                "description": "Motivo del cambio de horario proporcionado por el cliente."
                            }
                        },
                        "required": ["booking_uid", "new_start_time"]
                    }
                }
            }
        },
        {
            "toolSpec": {
                "name": "cancel_booking",
                "description": "Cancela una cita existente usando su booking_uid.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "booking_uid": {
                                "type": "string",
                                "description": "Identificador único (uid) de la reserva a cancelar."
                            },
                            "reason": {
                                "type": "string",
                                "description": "Motivo de cancelación proporcionado por el cliente."
                            }
                        },
                        "required": ["booking_uid"]
                    }
                }
            }
        }
    ]
}


def get_tz(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=-6))


def to_utc_iso(iso_str: str, tz_name: str) -> str:
    tz = get_tz(tz_name)
    parsed = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def trim_history(messages: list, max_items: int = 12) -> list:
    trimmed = messages[-max_items:]
    while trimmed:
        first = trimmed[0]
        content = first.get("content", [])
        is_plain_user = (
            first.get("role") == "user"
            and isinstance(content, list)
            and any("text" in block for block in content)
            and not any("toolResult" in block for block in content)
        )
        if is_plain_user:
            break
        trimmed.pop(0)
    return trimmed


async def calcom_get_slots(tenant: dict, start_date: str, end_date: str) -> dict:
    tz = get_tz(tenant["timezone"])
    try:
        dt_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
        dt_end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=tz)
    except ValueError:
        return {"error": "Formato de fecha inválido. Usa YYYY-MM-DD."}

    start_utc = dt_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = dt_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{API_URL}/v2/slots",
            params={
                "eventTypeId": int(tenant["event_type_id"]),
                "start": start_utc,
                "end": end_utc,
                "timeZone": tenant["timezone"],
            },
            headers={"cal-api-version": "2024-09-04"},
        )
        logger.info("Cal.com GET /v2/slots status=%s", resp.status_code)
        if resp.status_code != 200:
            return {"error": f"Cal.com devolvió {resp.status_code}", "detail": resp.text[:300]}
        data = resp.json().get("data", {})
        if not data:
            return {"slots": {}, "message": "No hay horarios disponibles en ese rango."}
        return {"slots": data}


async def calcom_create_booking(tenant: dict, user_phone: str, start_time: str, attendee_name: str, attendee_email: str) -> dict:
    try:
        start_utc = to_utc_iso(start_time, tenant["timezone"])
    except ValueError:
        return {"error": f"start_time inválido: {start_time}"}

    payload = {
        "start": start_utc,
        "eventTypeId": int(tenant["event_type_id"]),
        "attendee": {
            "name": attendee_name.strip(),
            "email": attendee_email.strip(),
            "timeZone": tenant["timezone"],
            "language": "es",
        },
        "metadata": {
            "whatsapp": user_phone,
            "source": "whatsapp-agent",
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_URL}/v2/bookings",
            json=payload,
            headers={
                "cal-api-version": "2024-08-13",
                "Content-Type": "application/json",
            },
        )
        logger.info("Cal.com POST /v2/bookings status=%s body=%s", resp.status_code, resp.text[:400])
        if resp.status_code not in (200, 201):
            return {"error": f"No se pudo crear la cita (HTTP {resp.status_code})", "detail": resp.text[:400]}
        booking = resp.json().get("data", {})
        return {
            "status": "confirmed",
            "booking_uid": booking.get("uid"),
            "title": booking.get("title"),
            "start": booking.get("start"),
            "end": booking.get("end"),
        }


async def calcom_reschedule_booking(tenant: dict, booking_uid: str, new_start_time: str, reason: str = "Reagendado por el cliente vía WhatsApp") -> dict:
    try:
        start_utc = to_utc_iso(new_start_time, tenant["timezone"])
    except ValueError:
        return {"error": f"new_start_time inválido: {new_start_time}"}

    payload = {
        "start": start_utc,
        "reschedulingReason": reason,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_URL}/v2/bookings/{booking_uid}/reschedule",
            json=payload,
            headers={
                "cal-api-version": "2024-08-13",
                "Content-Type": "application/json",
            },
        )
        logger.info("Cal.com POST /v2/bookings/%s/reschedule status=%s body=%s", booking_uid, resp.status_code, resp.text[:400])
        if resp.status_code not in (200, 201):
            return {"error": f"No se pudo reagendar la cita (HTTP {resp.status_code})", "detail": resp.text[:400]}
        booking = resp.json().get("data", {})
        return {
            "status": "rescheduled",
            "old_booking_uid": booking_uid,
            "new_booking_uid": booking.get("uid"),
            "title": booking.get("title"),
            "start": booking.get("start"),
            "end": booking.get("end"),
        }


async def calcom_cancel_booking(booking_uid: str, reason: str = "Cancelado por el cliente vía WhatsApp") -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_URL}/v2/bookings/{booking_uid}/cancel",
            json={"cancellationReason": reason},
            headers={
                "cal-api-version": "2024-08-13",
                "Content-Type": "application/json",
            },
        )
        logger.info("Cal.com POST /v2/bookings/%s/cancel status=%s", booking_uid, resp.status_code)
        if resp.status_code not in (200, 201):
            return {"error": f"No se pudo cancelar (HTTP {resp.status_code})", "detail": resp.text[:300]}
        return {"status": "cancelled", "booking_uid": booking_uid}


async def execute_tool(tool_name: str, tool_input: dict, tenant: dict, user_phone: str) -> dict:
    if tool_name == "get_available_slots":
        return await calcom_get_slots(tenant, tool_input["start_date"], tool_input["end_date"])
    if tool_name == "create_booking":
        return await calcom_create_booking(
            tenant,
            user_phone,
            tool_input["start_time"],
            tool_input["attendee_name"],
            tool_input["attendee_email"],
        )
    if tool_name == "reschedule_booking":
        return await calcom_reschedule_booking(
            tenant,
            tool_input["booking_uid"],
            tool_input["new_start_time"],
            tool_input.get("reason", "Reagendado vía WhatsApp"),
        )
    if tool_name == "cancel_booking":
        return await calcom_cancel_booking(
            tool_input["booking_uid"],
            tool_input.get("reason", "Cancelado vía WhatsApp"),
        )
    return {"error": f"Herramienta desconocida: {tool_name}"}


@app.get("/webhook/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Token de verificación inválido")


@app.post("/webhook/whatsapp")
async def receive_whatsapp(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    if WHATSAPP_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Firma inválida")

    payload = json.loads(raw_body)
    background_tasks.add_task(process_whatsapp_event, payload)
    return {"status": "accepted"}


async def send_whatsapp_message(phone_number_id: str, to_phone: str, text: str):
    if not WHATSAPP_ACCESS_TOKEN:
        logger.error("WHATSAPP_ACCESS_TOKEN no definido en el contenedor")
        return

    url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        logger.info("Meta Graph API status=%s body=%s", resp.status_code, resp.text)


async def process_whatsapp_event(payload: dict):
    if await redis_client.get("agent:paused") == "1":
        logger.info("Agente en pausa (agent:paused=1). Mensaje ignorado.")
        return

    try:
        value = payload["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return
        phone_number_id = value["metadata"]["phone_number_id"]
        msg = value["messages"][0]
        msg_id = msg.get("id")
        user_phone = msg["from"]
        msg_type = msg.get("type", "")
    except (KeyError, IndexError):
        return

    if msg_id:
        is_new = await redis_client.set(f"dedup:wa:{msg_id}", "1", ex=300, nx=True)
        if not is_new:
            logger.info("Mensaje duplicado ignorado wamid=%s", msg_id)
            return

    if msg_type != "text":
        await send_whatsapp_message(
            phone_number_id,
            user_phone,
            "Por el momento solo puedo procesar mensajes de texto. Escríbeme en qué fecha y horario te gustaría agendar tu cita."
        )
        return

    text = msg.get("text", {}).get("body", "").strip()
    if not text:
        return

    logger.info("Mensaje entrante phone_id=%s from=%s text=%s", phone_number_id, user_phone, text)

    database_url = os.getenv("DATABASE_DIRECT_URL")
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT calcom_api_key, event_type_id, timezone, business_name FROM agent_tenants WHERE phone_number_id = %s",
                    (phone_number_id,)
                )
                tenant = cur.fetchone()
                if not tenant:
                    logger.warning("Tenant no encontrado para phone_number_id=%s", phone_number_id)
                    return
    except Exception as e:
        logger.exception("Error consultando PostgreSQL: %s", e)
        return

    tz = get_tz(tenant["timezone"])
    now_local = datetime.now(tz)
    day_name = DAYS_ES[now_local.weekday()]

    system_prompt = (
        f"Eres el asistente virtual de citas de {tenant['business_name']}. "
        f"Fecha y hora actual: {day_name} {now_local.strftime('%Y-%m-%d %H:%M')} (zona horaria {tenant['timezone']}). "
        "Reglas estrictas:\n"
        "1. NUNCA inventes horarios disponibles. Siempre llama a get_available_slots antes de proponer horas, agendar o reagendar.\n"
        "2. Antes de llamar a create_booking, asegúrate de tener confirmado el horario exacto, el nombre completo y el correo electrónico del cliente.\n"
        "3. Al confirmar o reagendar una cita, incluye la fecha, hora en formato legible (ej. 9:00 AM) y el código de reserva vigente (booking_uid o new_booking_uid) por si desea modificarla o cancelarla después.\n"
        "4. Si el cliente solicita cambiar de hora una cita existente, usa reschedule_booking (no canceles y crees manualmente salvo que reschedule_booking falle).\n"
        "5. Responde en español, de forma amable, clara y concisa para WhatsApp (sin tablas largas)."
    )

    lock_key = f"lock:wa:{phone_number_id}:{user_phone}"
    async with redis_client.lock(lock_key, timeout=45, blocking_timeout=15):
        history_key = f"chat:wa:{phone_number_id}:{user_phone}"
        raw_history = await redis_client.get(history_key)
        messages = trim_history(json.loads(raw_history) if raw_history else [], max_items=12)
        messages.append({"role": "user", "content": [{"text": text}]})

        try:
            reply_text = ""
            for _ in range(5):
                response = await asyncio.to_thread(
                    bedrock.converse,
                    modelId=BEDROCK_MODEL_ID,
                    system=[{"text": system_prompt}],
                    messages=messages,
                    toolConfig=TOOL_CONFIG,
                    inferenceConfig={"maxTokens": 500, "temperature": 0.1},
                )
                output_message = response["output"]["message"]
                messages.append(output_message)

                if response.get("stopReason") == "tool_use":
                    tool_results = []
                    for block in output_message.get("content", []):
                        if "toolUse" in block:
                            tu = block["toolUse"]
                            logger.info("Tool call: %s input=%s", tu["name"], tu["input"])
                            result_data = await execute_tool(tu["name"], tu["input"], tenant, user_phone)
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tu["toolUseId"],
                                    "content": [{"json": result_data}],
                                    "status": "error" if "error" in result_data else "success",
                                }
                            })
                    messages.append({"role": "user", "content": tool_results})
                    continue

                reply_text = "".join(
                    block.get("text", "") for block in output_message.get("content", [])
                ).strip()
                break

            await redis_client.set(history_key, json.dumps(trim_history(messages, max_items=12)), ex=86400)

            if reply_text:
                await send_whatsapp_message(phone_number_id, user_phone, reply_text)
        except Exception as e:
            logger.exception("Error en ciclo Bedrock/Cal.com: %s", e)
            await send_whatsapp_message(
                phone_number_id,
                user_phone,
                "Tuve un inconveniente técnico momentáneo al consultar la agenda. ¿Puedes intentarlo de nuevo en un momento?"
            )
