import asyncio
import logging
import math
import sqlite3
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, time as dt_time
from io import BytesIO

import qrcode

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, FSInputFile,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import requests
import uuid
import github as gh
import urllib3
from urllib.parse import quote
from openpyxl import Workbook

import os
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ====================== НАСТРОЙКИ ======================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

INSTRUCTION_ANDROID = os.getenv("INSTRUCTION_ANDROID", "Инструкция Android.docx")
INSTRUCTION_IOS = os.getenv("INSTRUCTION_IOS", "Инструкция IOS.docx")

DB_NAME = os.getenv("DB_NAME", "vpn_bot.db")
TINKOFF_COLLECTION_LINK = os.getenv("TINKOFF_COLLECTION_LINK", "https://tbank.ru/cf/1W5S3zUX13t")
MASS_CONCURRENCY = int(os.getenv("MASS_CONCURRENCY", "6"))

LIMIT_IP = 2

SERVERS = [
    {
        "ip": "89.127.211.136",
        "label": "Amsterdam-1.1",
        "url": "http://89.127.211.136:16233/Nirreexag8xbcV8Mtv/panel/api",
        "token": "G90Xn0b6bMlWVinRVdxef569nO6zJhvplb6XJmQP0yNlITQM",
    },
    {
        "ip": "89.40.70.124",
        "label": "Amsterdam-2.1",
        "url": "http://89.40.70.124:45903/OY02850ZgFN4LJMaCE/panel/api",
        "token": "VqWD9uwHKZqX2xtObIhlcOw4oPcrA2lR6JeTSkfmLO4z1DE4",
    },
]

SESSION = requests.Session()
PRICES = {30: 219, 90: 599, 365: 2100}
OPENVPN_PRICE = int(os.getenv("OPENVPN_PRICE", "219"))
OPENVPN_DISCOUNT_PERCENT = int(os.getenv("OPENVPN_DISCOUNT_PERCENT", "30"))
OPENVPN_DAYS = int(os.getenv("OPENVPN_DAYS", "30"))
OPENVPN_REMOTE = os.getenv("OPENVPN_REMOTE", "")
OPENVPN_PORT = os.getenv("OPENVPN_PORT", "1194")
OPENVPN_PROTO = os.getenv("OPENVPN_PROTO", "udp")
OPENVPN_TEMPLATE = os.getenv("OPENVPN_TEMPLATE", "openvpn_template.ovpn")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("vpn_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
github_lock = asyncio.Lock()


class AdminStates(StatesGroup):
    waiting_for_temp_email = State()
    waiting_for_temp_days = State()
    waiting_for_temp_device = State()
    waiting_for_email_to_delete = State()


class SubscriptionStates(StatesGroup):
    choosing_device = State()
    choosing_duration = State()


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_IDS) and user_id in ADMIN_IDS


def calc_openvpn_price(has_active_vless: bool) -> tuple[int, int]:
    """Возвращает (к оплате, размер скидки в рублях)."""
    base = OPENVPN_PRICE
    if not has_active_vless:
        return base, 0
    discounted = int(round(base * (100 - OPENVPN_DISCOUNT_PERCENT) / 100))
    return discounted, base - discounted


def has_active_vless(telegram_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM user_subscriptions us
        JOIN users u ON u.id = us.user_id
        WHERE u.telegram_id = ?
          AND us.plan_type = 'mobile'
          AND us.status = 'active'
          AND date(us.end_date) >= date('now')
        LIMIT 1
        """,
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row)


def build_openvpn_profile(email: str) -> str:
    if OPENVPN_TEMPLATE and os.path.isfile(OPENVPN_TEMPLATE):
        with open(OPENVPN_TEMPLATE, "r", encoding="utf-8") as f:
            raw = f.read()
        return (
            raw.replace("{email}", email)
            .replace("{remote}", OPENVPN_REMOTE)
            .replace("{port}", str(OPENVPN_PORT))
            .replace("{proto}", OPENVPN_PROTO)
        )
    remote = OPENVPN_REMOTE or "CHANGE_ME_OPENVPN_HOST"
    return (
        "client\n"
        "dev tun\n"
        f"proto {OPENVPN_PROTO}\n"
        f"remote {remote} {OPENVPN_PORT}\n"
        "resolv-retry infinite\n"
        "nobind\n"
        "persist-key\n"
        "persist-tun\n"
        "remote-cert-tls server\n"
        "auth-user-pass\n"
        f"# username: {email}\n"
        "verb 3\n"
    )


def make_qr_png(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            role TEXT NOT NULL DEFAULT 'client' CHECK (role IN ('client', 'admin', 'superadmin')),
            language TEXT DEFAULT 'ru',
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plan_type TEXT NOT NULL CHECK (plan_type IN ('mobile', 'router')),
            preferred_platform TEXT CHECK (preferred_platform IN ('android', 'ios')),
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'expired', 'cancelled', 'pending')),
            duration_days INTEGER,
            last_purchase_id INTEGER,
            config_link TEXT,
            config_file_path TEXT,
            config_details TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_sub
        ON user_subscriptions (user_id, plan_type)
        WHERE status = 'active'
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subscription_id INTEGER REFERENCES user_subscriptions(id) ON DELETE SET NULL,
            plan_type TEXT NOT NULL CHECK (plan_type IN ('mobile', 'router')),
            chosen_platform TEXT CHECK (chosen_platform IN ('android', 'ios')),
            duration_days INTEGER NOT NULL CHECK (duration_days > 0),
            amount REAL NOT NULL DEFAULT 0,
            currency TEXT DEFAULT 'RUB',
            payment_provider TEXT,
            external_payment_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paid', 'failed', 'refunded', 'cancelled')),
            paid_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            notes TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_notifications (
            email TEXT PRIMARY KEY,
            message_ids TEXT
        )
    """)

    conn.commit()
    conn.close()


def ensure_db_columns():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    cols = {r[1] for r in cur.fetchall()}
    if "config_email" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN config_email TEXT")
    if "is_temp" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_temp INTEGER NOT NULL DEFAULT 0")
    cur.execute("""
        UPDATE users
        SET config_email = 'tg' || telegram_id
        WHERE telegram_id > 0
          AND (config_email IS NULL OR config_email = '')
    """)
    conn.commit()
    conn.close()


async def create_subscription(user_id: int, username: str, days: int, device: str = "ios"):
    def _create():
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        cursor = conn.cursor()
        config_email = f"tg{user_id}"

        cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            db_user_id = row[0]
            cursor.execute(
                "UPDATE users SET config_email = ?, username = ?, is_temp = 0, updated_at = datetime('now') WHERE id = ?",
                (config_email, username, db_user_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO users (telegram_id, username, role, config_email, is_temp)
                VALUES (?, ?, 'client', ?, 0)
                """,
                (user_id, username, config_email),
            )
            db_user_id = cursor.lastrowid

        plan_type = "router" if device == "router" else "mobile"
        preferred_platform = device if device in ("android", "ios") else "ios"
        start_date = datetime.now().strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        cursor.execute(
            """
            INSERT INTO user_subscriptions
            (user_id, plan_type, preferred_platform, start_date, end_date, status, duration_days)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (db_user_id, plan_type, preferred_platform, start_date, end_date, days),
        )

        conn.commit()
        conn.close()
        return config_email

    return await asyncio.to_thread(_create)


def register_temp_client(email: str, days: int):
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cur = conn.cursor()
    start = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    cur.execute("SELECT id FROM users WHERE config_email = ?", (email,))
    row = cur.fetchone()
    if row:
        uid = row[0]
        cur.execute(
            """
            UPDATE users
            SET is_temp = 1, username = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (email, uid),
        )
    else:
        cur.execute("SELECT COALESCE(MIN(telegram_id), 0) FROM users")
        min_tid = cur.fetchone()[0]
        new_tid = min(min_tid, 0) - 1
        cur.execute(
            """
            INSERT INTO users (telegram_id, username, role, config_email, is_temp)
            VALUES (?, ?, 'client', ?, 1)
            """,
            (new_tid, email, email),
        )
        uid = cur.lastrowid

    cur.execute(
        """
        SELECT id FROM user_subscriptions
        WHERE user_id = ? AND plan_type = 'mobile' AND status = 'active'
        """,
        (uid,),
    )
    sub = cur.fetchone()
    if sub:
        cur.execute(
            """
            UPDATE user_subscriptions
            SET end_date = ?, duration_days = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (end, days, sub[0]),
        )
    else:
        cur.execute(
            """
            INSERT INTO user_subscriptions
            (user_id, plan_type, preferred_platform, start_date, end_date, status, duration_days)
            VALUES (?, 'mobile', 'ios', ?, ?, 'active', ?)
            """,
            (uid, start, end, days),
        )

    conn.commit()
    conn.close()


# ====================== 3X-UI ======================

def make_request(url, token, method="GET", max_retries=4, **kwargs):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    for attempt in range(max_retries):
        try:
            return SESSION.request(
                method, url, headers=headers, verify=False, timeout=35, **kwargs
            )
        except Exception as e:
            log.warning("Request %s try %s: %s", url, attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(1.5)
    return None


def parse_inbound_settings(inbound):
    if not inbound:
        return {}
    settings = inbound.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    return settings


def get_first_inbound(server):
    resp = make_request(f"{server['url']}/inbounds/list", server["token"])
    if not resp:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if data.get("success"):
        obj = data.get("obj") or []
        return obj[0] if obj else None
    return None


def get_client_from_inbound(inbound, email):
    if not inbound:
        return None
    for client in parse_inbound_settings(inbound).get("clients") or []:
        if client.get("email") == email:
            return client
    return None


def add_new_client(server, email, days=0, expiry_ms=None):
    inbound = get_first_inbound(server)
    if not inbound:
        return None, None

    if expiry_ms is None:
        if days > 0:
            expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
        else:
            expiry_ms = 0

    client_uuid = str(uuid.uuid4())
    payload = {
        "client": {
            "id": client_uuid,
            "email": email,
            "flow": "xtls-rprx-vision",
            "limitIp": LIMIT_IP,
            "totalGB": 0,
            "enable": True,
            "expiryTime": int(expiry_ms),
        },
        "inboundIds": [inbound["id"]],
    }
    resp = make_request(
        f"{server['url']}/clients/add", server["token"], method="POST", json=payload
    )
    if resp and resp.status_code == 200:
        try:
            if resp.json().get("success"):
                return client_uuid, inbound
        except Exception:
            pass
    return None, None


def extend_client_expiry(server, email: str, additional_days: int):
    inbound = get_first_inbound(server)
    if not inbound:
        return False
    client = get_client_from_inbound(inbound, email)
    if not client:
        return False
    current = client.get("expiryTime") or 0
    now = int(datetime.now().timestamp() * 1000)
    base = current if current > now else now
    new_expiry = base + (additional_days * 24 * 60 * 60 * 1000)
    payload = {
        "id": client["id"],
        "email": email,
        "flow": client.get("flow") or "xtls-rprx-vision",
        "limitIp": LIMIT_IP,
        "totalGB": client.get("totalGB", 0),
        "enable": True,
        "expiryTime": new_expiry,
    }
    resp = make_request(
        f"{server['url']}/clients/update/{email}",
        server["token"],
        method="POST",
        json=payload,
    )
    return bool(resp and resp.status_code == 200 and resp.json().get("success"))


def create_or_extend_client(server, email: str, days: int):
    inbound = get_first_inbound(server)
    if not inbound:
        return None, None
    client = get_client_from_inbound(inbound, email)
    if client:
        if extend_client_expiry(server, email, days):
            return client["id"], inbound
        return add_new_client(server, email, days)
    return add_new_client(server, email, days)


def _delete_client_from_server(server, email: str) -> bool:
    url = f"{server['url']}/clients/del/{email}?keepTraffic=0"
    resp = make_request(url, server["token"], method="POST")
    if resp and resp.status_code == 200:
        try:
            return resp.json().get("success", False)
        except Exception:
            return False
    return False


def delete_client_everywhere(email: str):
    deleted = 0
    with ThreadPoolExecutor(max_workers=len(SERVERS)) as pool:
        futures = {pool.submit(_delete_client_from_server, s, email): s for s in SERVERS}
        for future in as_completed(futures):
            try:
                if future.result():
                    deleted += 1
            except Exception:
                pass

    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        cursor = conn.cursor()

        # id пользователей по config_email / tg
        cursor.execute("SELECT id FROM users WHERE config_email = ?", (email,))
        ids = [r[0] for r in cursor.fetchall()]

        if email.startswith("tg"):
            try:
                tid = int(email[2:])
                cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (tid,))
                ids += [r[0] for r in cursor.fetchall()]
            except ValueError:
                pass

        ids = list(set(ids))
        for uid in ids:
            cursor.execute("DELETE FROM user_subscriptions WHERE user_id = ?", (uid,))
            cursor.execute("DELETE FROM users WHERE id = ?", (uid,))

        conn.commit()
        conn.close()
        log.info("Удалён из БД: %s (users=%s)", email, ids)
    except Exception as e:
        log.error("Ошибка удаления из базы: %s", e)

    try:
        g = gh.Github(auth=gh.Auth.Token(GITHUB_TOKEN))
        repo = g.get_repo(GITHUB_REPO)
        file = repo.get_contents(f"{email}.txt")
        repo.delete_file(f"{email}.txt", f"Delete {email}", file.sha)
    except Exception:
        pass

    return deleted


def build_vless_link(server_ip, label, inbound, client_uuid, name):
    stream = inbound.get("streamSettings") or {}
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}
    reality = stream.get("realitySettings") or {}
    settings = reality.get("settings") or {}
    pk = settings.get("publicKey") or ""
    sid = (reality.get("shortIds") or [""])[0]
    spx = settings.get("spiderX") or reality.get("spiderX") or "/"
    port = inbound.get("port", 443)
    sni_list = reality.get("serverNames") or []
    sni = sni_list[0] if sni_list else "www.sony.com"
    fp = settings.get("fingerprint") or "firefox"

    return (
        f"vless://{client_uuid}@{server_ip}:{port}?"
        f"encryption=none&flow=xtls-rprx-vision&fp={fp}&pbk={pk}"
        f"&security=reality&sid={sid}&sni={sni}"
        f"&spx={quote(spx, safe='')}&type=tcp#{label}-{name}"
    )


async def update_github_file_completely(name: str, links: list):
    async with github_lock:
        def _update():
            g = gh.Github(auth=gh.Auth.Token(GITHUB_TOKEN))
            repo = g.get_repo(GITHUB_REPO)
            content = "\n".join(links)
            filename = f"{name}.txt"
            try:
                file = repo.get_contents(filename)
                repo.update_file(filename, f"Update {name}", content, file.sha)
            except Exception:
                repo.create_file(filename, f"Create {name}", content)

        try:
            await asyncio.to_thread(_update)
        except Exception as e:
            log.error("GitHub error для %s: %s", name, e)


# ====================== ДНИ ======================

def get_sub_end_date(email: str):
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT us.end_date
        FROM user_subscriptions us
        JOIN users u ON u.id = us.user_id
        WHERE u.config_email = ?
          AND us.status = 'active'
        ORDER BY date(us.end_date) DESC
        LIMIT 1
        """,
        (email,),
    )
    row = cur.fetchone()
    if not row and email.startswith("tg"):
        try:
            tid = int(email[2:])
            cur.execute(
                """
                SELECT us.end_date
                FROM user_subscriptions us
                JOIN users u ON u.id = us.user_id
                WHERE u.telegram_id = ?
                  AND us.status = 'active'
                ORDER BY date(us.end_date) DESC
                LIMIT 1
                """,
                (tid,),
            )
            row = cur.fetchone()
        except ValueError:
            pass
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(str(row[0]).split()[0], "%Y-%m-%d").date()
    except Exception:
        return None


def resolve_remaining_and_expiry(email: str):
    """
    remaining: int (может быть < 0)
    expiry_ms: timestamp конца суток end_date или с панели
    """
    end = get_sub_end_date(email)
    if end is not None:
        remaining = (end - datetime.now().date()).days
        exp_dt = datetime.combine(end, dt_time(23, 59, 59))
        expiry_ms = int(exp_dt.timestamp() * 1000)
        return remaining, expiry_ms

    best_rem = None
    best_exp = None
    now_ms = int(datetime.now().timestamp() * 1000)
    for server in SERVERS:
        inbound = get_first_inbound(server)
        client = get_client_from_inbound(inbound, email)
        if not client:
            continue
        exp = client.get("expiryTime") or 0
        if exp <= 0:
            return 3650, 0
        rem = math.floor((exp - now_ms) / 86400000)
        if best_exp is None or exp > best_exp:
            best_exp = exp
            best_rem = rem
    if best_exp is None:
        return 0, None
    return best_rem if best_rem is not None else 0, best_exp


def _check_server_days(server, email: str) -> int:
    inbound = get_first_inbound(server)
    client = get_client_from_inbound(inbound, email)
    if not client:
        return 0
    exp = client.get("expiryTime") or 0
    if exp <= 0:
        return 0
    remaining_ms = exp - int(datetime.now().timestamp() * 1000)
    if remaining_ms > 0:
        return math.ceil(remaining_ms / 86400000)
    return 0


async def get_user_remaining_days(email: str) -> int:
    remaining, _ = await asyncio.to_thread(resolve_remaining_and_expiry, email)
    return max(0, remaining or 0)


def get_user_payments(telegram_id: int) -> list:
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT p.paid_at, p.amount, p.duration_days, p.chosen_platform,
               p.plan_type, p.status, p.created_at
        FROM purchases p
        JOIN users u ON u.id = p.user_id
        WHERE u.telegram_id = ?
          AND p.status = 'paid'
        ORDER BY COALESCE(p.paid_at, p.created_at) DESC
        LIMIT 50
        """,
        (telegram_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def collect_all_emails() -> list:
    emails = set()
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cur = conn.cursor()
    # только с подпиской active/pending
    cur.execute("""
        SELECT DISTINCT COALESCE(u.config_email, 'tg' || u.telegram_id)
        FROM users u
        JOIN user_subscriptions us ON us.user_id = u.id
        WHERE us.status IN ('active', 'pending')
          AND (
            (u.config_email IS NOT NULL AND u.config_email != '')
            OR u.telegram_id > 0
          )
    """)
    for (em,) in cur.fetchall():
        if em:
            emails.add(em)
    conn.close()

    # кто ещё есть на панелях (на случай рассинхрона)
    for server in SERVERS:
        inbound = get_first_inbound(server)
        if not inbound:
            continue
        for c in parse_inbound_settings(inbound).get("clients") or []:
            em = (c.get("email") or "").strip()
            if em:
                emails.add(em)
    return sorted(emails)


async def notify_expiring_subscriptions():
    while True:
        await asyncio.sleep(86400)
        try:
            conn = sqlite3.connect(DB_NAME, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT u.telegram_id, u.username, us.plan_type, us.preferred_platform, us.end_date
                FROM user_subscriptions us
                JOIN users u ON u.id = us.user_id
                WHERE us.status = 'active'
                  AND u.is_temp = 0
                  AND u.telegram_id > 0
                  AND date(us.end_date) <= date('now', '+1 day')
                """
            )
            expiring = cursor.fetchall()
            conn.close()

            for telegram_id, username, plan_type, platform, end_date in expiring:
                try:
                    remaining = 0
                    if end_date:
                        try:
                            exp = datetime.strptime(str(end_date).split()[0], "%Y-%m-%d").date()
                            remaining = max(0, (exp - datetime.now().date()).days)
                        except Exception:
                            remaining = 0
                    text = (
                        f"⚠️ <b>Внимание!</b>\n\n"
                        f"У тебя заканчивается подписка на <b>{plan_type}</b> "
                        f"({platform or '—'}).\n"
                        f"Осталось дней: <b>{remaining}</b>\n\n"
                        f"Продли подписку, чтобы не остаться без доступа."
                    )
                    await bot.send_message(telegram_id, text, parse_mode="HTML")
                except TelegramForbiddenError:
                    pass
                except Exception as e:
                    log.error("notify %s: %s", telegram_id, e)
        except Exception as e:
            log.error("notify_expiring_subscriptions: %s", e)


# ====================== МАССОВОЕ ОБНОВЛЕНИЕ ======================

async def _sync_one_user(email: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        remaining, expiry_ms = await asyncio.to_thread(resolve_remaining_and_expiry, email)

        links = []
        for server in SERVERS:
            inbound = await asyncio.to_thread(get_first_inbound, server)
            if not inbound:
                log.warning("[%s] нет inbound: %s", email, server["label"])
                continue

            client = get_client_from_inbound(inbound, email)
            if client:
                links.append(
                    build_vless_link(
                        server["ip"], server["label"], inbound, client["id"], email
                    )
                )
                continue

            # Нет на сервере — создаём с реальной датой (даже если просрочен)
            if expiry_ms is None:
                log.info("[%s] нет даты окончания, skip create на %s", email, server["label"])
                continue

            days_arg = max(remaining, 0) if remaining and remaining > 0 else 0
            result = await asyncio.to_thread(
                add_new_client, server, email, days_arg, expiry_ms
            )
            if result and result[0] and result[1]:
                links.append(
                    build_vless_link(
                        server["ip"], server["label"], result[1], result[0], email
                    )
                )
                log.info(
                    "[%s] создан на %s (remaining=%s)",
                    email,
                    server["label"],
                    remaining,
                )
            else:
                log.error("[%s] fail create %s", email, server["label"])

        if links:
            await update_github_file_completely(email, links)
            return True
        return False


async def sync_all_clients():
    emails = await asyncio.to_thread(collect_all_emails)
    total = len(emails)
    log.info("Mass update emails: %s", total)

    semaphore = asyncio.Semaphore(MASS_CONCURRENCY)
    results = await asyncio.gather(
        *[_sync_one_user(email, semaphore) for email in emails],
        return_exceptions=True,
    )

    updated = sum(1 for r in results if r is True)
    failed = 0
    for email, r in zip(emails, results):
        if isinstance(r, Exception):
            failed += 1
            log.error("sync %s: %s", email, r)
        elif r is False:
            failed += 1
    return total, updated, failed


@dp.callback_query(F.data == "admin_mass_update")
async def admin_mass_update(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.answer()
    await callback.message.answer("🔄 Запущено массовое обновление...")
    total, updated, failed = await sync_all_clients()
    await callback.message.answer(
        f"✅ Готово.\nВсего: {total}\nОбновлено: {updated}\nПропущено/ошибок: {failed}"
    )
    await show_admin_panel(callback)


# ====================== АДМИН ======================

@dp.callback_query(F.data == "admin_all_clients")
async def admin_all_clients(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.telegram_id, u.username, u.config_email, u.is_temp,
               us.plan_type, us.preferred_platform, us.status, us.end_date, us.duration_days
        FROM users u
        LEFT JOIN user_subscriptions us
            ON us.user_id = u.id AND us.status = 'active'
        ORDER BY u.is_temp, u.id
        """
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await callback.message.answer("Пользователей нет.")
        return await show_admin_panel(callback)

    text = "📋 <b>Клиенты (active):</b>\n\n"
    for tid, username, cfg, is_temp, plan, platform, status, end_date, bought in rows:
        mark = "🕒 TEMP" if is_temp else f"@{username}" if username else f"tg{tid}"
        cfg = cfg or f"tg{tid}"
        remaining = "—"
        if end_date:
            try:
                exp = datetime.strptime(str(end_date).split()[0], "%Y-%m-%d").date()
                remaining = (exp - datetime.now().date()).days
            except Exception:
                remaining = "?"
        if plan:
            text += (
                f"<b>{mark}</b> <code>{cfg}</code>\n"
                f"  • {plan} ({platform or '—'}): {remaining} дн. (куплено {bought})\n"
            )
        else:
            text += f"<b>{mark}</b> <code>{cfg}</code>\n  • нет active\n"

    # Telegram лимит ~4096
    if len(text) > 4000:
        text = text[:3900] + "\n…обрезано"
    await callback.message.answer(text, parse_mode="HTML")
    await show_admin_panel(callback)


@dp.callback_query(F.data == "admin_create_temp_config")
async def admin_create_temp_config_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Android", callback_data="temp_device_android")],
            [InlineKeyboardButton(text="iOS", callback_data="temp_device_ios")],
        ]
    )
    await callback.message.answer("Выберите устройство для временного конфига:", reply_markup=kb)
    await state.set_state(AdminStates.waiting_for_temp_device)


@dp.callback_query(AdminStates.waiting_for_temp_device)
async def admin_temp_config_device(callback: CallbackQuery, state: FSMContext):
    device = callback.data.split("_")[2]
    await state.update_data(temp_device=device)
    await callback.message.edit_text("Введите email (логин) временного конфига:")
    await state.set_state(AdminStates.waiting_for_temp_email)


@dp.message(AdminStates.waiting_for_temp_email)
async def admin_temp_config_email(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(temp_email=message.text.strip())
    await message.answer("Введите количество дней:")
    await state.set_state(AdminStates.waiting_for_temp_days)


@dp.message(AdminStates.waiting_for_temp_days)
async def admin_temp_config_days(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    email = data.get("temp_email")
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("Введите число дней.")
        return

    await state.clear()
    await message.answer(
        f"Создаю конфиг <code>{email}</code> на {days} дн. …", parse_mode="HTML"
    )

    expiry_ms = int(
        (datetime.now() + timedelta(days=days)).replace(
            hour=23, minute=59, second=59, microsecond=0
        ).timestamp()
        * 1000
    )
    results = await asyncio.gather(
        *[
            asyncio.to_thread(add_new_client, server, email, days, expiry_ms)
            for server in SERVERS
        ],
        return_exceptions=True,
    )
    links = []
    for i, result in enumerate(results):
        if isinstance(result, tuple) and result[0] and result[1]:
            links.append(
                build_vless_link(
                    SERVERS[i]["ip"], SERVERS[i]["label"], result[1], result[0], email
                )
            )

    if links:
        await asyncio.to_thread(register_temp_client, email, days)
        await update_github_file_completely(email, links)
        await message.answer(
            f"✅ Временный конфиг создан и записан в БД.\n\n"
            f"Ссылка: https://raw.githubusercontent.com/{GITHUB_REPO}/main/{email}.txt"
        )
    else:
        await message.answer("Не удалось создать конфиг.")

    await show_admin_panel(message)


@dp.callback_query(F.data == "admin_delete_client")
async def admin_delete_client_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.message.answer("Введите email (config_email) для удаления:")
    await state.set_state(AdminStates.waiting_for_email_to_delete)


@dp.message(AdminStates.waiting_for_email_to_delete)
async def admin_delete_client_confirm(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    email = message.text.strip()
    await state.clear()
    deleted = await asyncio.to_thread(delete_client_everywhere, email)
    await message.answer(f"✅ Удалено с {deleted} серверов + БД/GitHub")
    await show_admin_panel(message)


@dp.callback_query(F.data == "admin_export_excel")
async def admin_export_excel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.message.answer("Генерирую Excel...")

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.username, u.telegram_id, u.config_email, u.is_temp,
               us.plan_type, us.preferred_platform, us.status, us.end_date
        FROM users u
        LEFT JOIN user_subscriptions us ON us.user_id = u.id
        """
    )
    clients = cursor.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.append(
        ["Username", "TG ID", "config_email", "temp", "Тип", "Платформа", "Статус", "Осталось"]
    )
    for username, tid, cfg, is_temp, plan, platform, status, end_date in clients:
        remaining = ""
        if end_date:
            try:
                exp = datetime.strptime(str(end_date).split()[0], "%Y-%m-%d").date()
                remaining = (exp - datetime.now().date()).days
            except Exception:
                remaining = ""
        ws.append(
            [
                f"@{username}" if username else "",
                tid,
                cfg or "",
                is_temp,
                plan or "",
                platform or "",
                status or "",
                remaining,
            ]
        )

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    await callback.message.answer_document(
        BufferedInputFile(buffer.read(), filename="clients.xlsx"),
        caption="📊 Список клиентов",
    )
    await show_admin_panel(callback)


# ====================== ПОЛЬЗОВАТЕЛЬ ======================

def get_main_keyboard(user_id: int):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Проверить дни подписки", callback_data="check_days")],
            [InlineKeyboardButton(text="🛒 Купить / Продлить", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔌 Подключить VPN", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
        ]
    )
    if is_admin(user_id):
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="Админ-панель", callback_data="admin_panel")]
        )
    return kb


def device_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Android (VLESS)", callback_data="device_android")],
            [InlineKeyboardButton(text="iOS (VLESS)", callback_data="device_ios")],
            [InlineKeyboardButton(text="Роутер (OpenVPN)", callback_data="device_router")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")],
        ]
    )


async def show_device_menu(target, state: FSMContext):
    """Общий вход для /renew, /connect и кнопки Купить/Продлить."""
    await state.set_state(SubscriptionStates.choosing_device)
    text = "Выберите устройство:"
    kb = device_keyboard()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@dp.message(Command("start"))
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    remaining = await get_user_remaining_days(f"tg{message.from_user.id}")
    text = (
        f"Привет! У тебя осталось <b>{remaining} дней</b> подписки."
        if remaining > 0
        else "Привет! У тебя пока нет активной подписки."
    )
    await message.answer(
        text, reply_markup=get_main_keyboard(message.from_user.id), parse_mode="HTML"
    )


@dp.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(message.from_user.id),
    )


@dp.message(Command("renew"))
@dp.message(Command("connect"))
async def renew_or_connect_cmd(message: Message, state: FSMContext):
    await state.clear()
    await show_device_menu(message, state)


@dp.callback_query(F.data == "check_days")
async def check_days(callback: CallbackQuery):
    await callback.answer()
    remaining = await get_user_remaining_days(f"tg{callback.from_user.id}")
    text = (
        f"У тебя осталось <b>{remaining} дней</b> подписки."
        if remaining > 0
        else "У тебя пока нет активной подписки."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_main")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "buy_subscription")
async def choose_device(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_device_menu(callback, state)


@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    await callback.answer()
    rows = get_user_payments(callback.from_user.id)
    if not rows:
        text = (
            "📊 <b>Твои платежи</b>\n\n"
            "Пока нет подтверждённых оплат.\n"
            "После того как администратор подтвердит оплату — запись появится здесь."
        )
    else:
        lines = ["📊 <b>Твои платежи</b>\n"]
        for paid_at, amount, duration_days, platform, plan_type, status, created_at in rows:
            when = paid_at or created_at or "—"
            if when and len(str(when)) >= 10:
                when = str(when)[:10]
            device = platform or plan_type or "—"
            if device == "mobile":
                device = "телефон"
            elif device == "router":
                device = "роутер"
            elif device == "android":
                device = "Android"
            elif device == "ios":
                device = "iOS"
            try:
                amount_s = f"{int(amount)} ₽"
            except Exception:
                amount_s = f"{amount} ₽"
            lines.append(
                f"• <b>{when}</b> — {amount_s}, {duration_days} дн., {device}"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_main")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_text(
            "Главное меню", reply_markup=get_main_keyboard(callback.from_user.id)
        )
    except TelegramBadRequest:
        await callback.message.answer(
            "Главное меню", reply_markup=get_main_keyboard(callback.from_user.id)
        )


@dp.callback_query(SubscriptionStates.choosing_device)
async def choose_duration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not callback.data.startswith("device_"):
        return
    device = callback.data.split("_")[1]
    await state.update_data(device=device)

    if device == "router":
        vless_active = has_active_vless(callback.from_user.id)
        price, discount = calc_openvpn_price(vless_active)
        lines = [
            "🛡 <b>OpenVPN для роутера</b>",
            f"Срок: <b>{OPENVPN_DAYS} дней</b>",
            f"Базовая цена: <b>{OPENVPN_PRICE} ₽</b>",
        ]
        if discount:
            lines.append(
                f"Скидка {OPENVPN_DISCOUNT_PERCENT}% за активный VLESS: <b>−{discount} ₽</b>"
            )
            lines.append(f"К оплате: <b>{price} ₽</b>")
        else:
            lines.append("Скидка 30% действует только при активном VLESS.")
            lines.append(f"К оплате: <b>{price} ₽</b>")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"Оформить за {price} ₽",
                    callback_data=f"duration_{OPENVPN_DAYS}",
                )],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_device")],
            ]
        )
        text = "\n".join(lines)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await state.set_state(SubscriptionStates.choosing_duration)
        return

    remaining = await get_user_remaining_days(f"tg{callback.from_user.id}")
    text = (
        f"⚠️ У тебя осталось <b>{remaining} дней</b>.\n\nВыберите срок продления:"
        if remaining > 0
        else "Выберите срок подписки:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"1 месяц — {PRICES[30]} ₽", callback_data="duration_30")],
            [InlineKeyboardButton(text=f"3 месяца — {PRICES[90]} ₽", callback_data="duration_90")],
            [InlineKeyboardButton(text=f"12 месяцев — {PRICES[365]} ₽", callback_data="duration_365")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_device")],
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(SubscriptionStates.choosing_duration)


@dp.callback_query(F.data == "back_to_device")
async def back_to_device(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_text("Выберите устройство:", reply_markup=device_keyboard())
    except TelegramBadRequest:
        await callback.message.answer("Выберите устройство:", reply_markup=device_keyboard())
    await state.set_state(SubscriptionStates.choosing_device)


@dp.callback_query(SubscriptionStates.choosing_duration)
async def create_order(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    days = int(callback.data.split("_")[1])
    data = await state.get_data()
    device = data.get("device", "android")
    user_id = callback.from_user.id
    username = callback.from_user.username or f"user{user_id}"
    if device == "router":
        price, discount = calc_openvpn_price(has_active_vless(user_id))
        proto_label = "OpenVPN / роутер"
    else:
        price = PRICES.get(days, 0)
        discount = 0
        proto_label = f"VLESS / {device}"
    email = await create_subscription(user_id, username, days, device)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить подписку", url=TINKOFF_COLLECTION_LINK)],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил",
                    callback_data=f"paid_{user_id}_{days}_{email}_{device}_{price}",
                )
            ],
        ]
    )
    extra = f"\nСкидка: <b>−{discount} ₽</b>" if discount else ""
    await callback.message.edit_text(
        f"✅ Заявка создана!\n\n"
        f"{proto_label}\n"
        f"Сумма: <b>{price} ₽</b> за <b>{days} дней</b>{extra}\n\n"
        f"Оплати и нажми «Я оплатил».",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.clear()

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 <b>Новая заявка!</b>\n\n"
                f"@{username} | <code>{user_id}</code>\n"
                f"Конфиг: <code>{email}</code>\n"
                f"{proto_label} | {days} дн. | {price} ₽",
                parse_mode="HTML",
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("paid_"))
async def user_confirmed_payment(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    user_id = int(parts[1])
    days = int(parts[2])
    email = parts[3]
    device = parts[4] if len(parts) > 4 else "android"
    price = int(parts[5]) if len(parts) > 5 else (
        calc_openvpn_price(has_active_vless(user_id))[0] if device == "router" else PRICES.get(days, 0)
    )
    username = callback.from_user.username or f"user{user_id}"
    proto_label = "OpenVPN / роутер" if device == "router" else f"VLESS / {device}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=f"approve_{user_id}_{days}_{email}_{device}_{price}",
                )
            ],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}_{email}")],
        ]
    )

    message_ids = {}
    for admin_id in ADMIN_IDS:
        try:
            msg = await bot.send_message(
                admin_id,
                f"💰 Подтверждение оплаты\n\n"
                f"@{username} <code>{user_id}</code>\n"
                f"<code>{email}</code> | {days} дн. | {proto_label} | {price} ₽",
                parse_mode="HTML",
                reply_markup=kb,
            )
            message_ids[str(admin_id)] = msg.message_id
        except Exception:
            pass

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO payment_notifications (email, message_ids) VALUES (?, ?)",
        (email, json.dumps(message_ids)),
    )
    conn.commit()
    conn.close()

    await callback.message.edit_text(
        "Спасибо! Администратор проверит оплату.", parse_mode="HTML"
    )


async def _delete_payment_notifications(email: str):
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT message_ids FROM payment_notifications WHERE email = ?", (email,)
    )
    row = cursor.fetchone()
    if row:
        for admin_id_str, msg_id in json.loads(row[0]).items():
            try:
                await bot.delete_message(chat_id=int(admin_id_str), message_id=msg_id)
            except Exception:
                pass
        cursor.execute("DELETE FROM payment_notifications WHERE email = ?", (email,))
        conn.commit()
    conn.close()


@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.answer()

    data = callback.data.split("_")
    user_id = int(data[1])
    days = int(data[2])
    email = data[3]
    device = data[4] if len(data) > 4 else "ios"
    locked_price = int(data[5]) if len(data) > 5 else None

    await _delete_payment_notifications(email)

    plan_type = "router" if device == "router" else "mobile"
    preferred_platform = device if device in ("android", "ios") else "ios"

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return await callback.message.answer("Пользователь не найден.")

    db_user_id = row[0]
    cursor.execute(
        "UPDATE users SET config_email = ?, is_temp = 0 WHERE id = ?",
        (email, db_user_id),
    )

    cursor.execute(
        """
        SELECT id, end_date FROM user_subscriptions
        WHERE user_id = ? AND plan_type = ? AND status = 'active'
        """,
        (db_user_id, plan_type),
    )
    existing = cursor.fetchone()

    if existing:
        sub_id, current_end = existing
        try:
            base_date = datetime.strptime(str(current_end).split()[0], "%Y-%m-%d")
        except Exception:
            base_date = datetime.now()
        if base_date.date() < datetime.now().date():
            base_date = datetime.now()
        new_end = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
        cursor.execute(
            """
            UPDATE user_subscriptions
            SET end_date = ?, duration_days = COALESCE(duration_days, 0) + ?,
                preferred_platform = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (new_end, days, preferred_platform, sub_id),
        )
    else:
        start_date = datetime.now().strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        cursor.execute(
            """
            INSERT INTO user_subscriptions
            (user_id, plan_type, preferred_platform, start_date, end_date, status, duration_days)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (db_user_id, plan_type, preferred_platform, start_date, end_date, days),
        )
        sub_id = cursor.lastrowid
        cursor.execute(
            """
            UPDATE user_subscriptions SET status = 'cancelled'
            WHERE user_id = ? AND status = 'pending'
            """,
            (db_user_id,),
        )

    if locked_price is not None:
        amount = locked_price
    elif device == "router":
        amount, _ = calc_openvpn_price(has_active_vless(user_id))
    else:
        amount = PRICES.get(days, 0)
    platform_for_purchase = preferred_platform if preferred_platform in ("android", "ios") else None
    cursor.execute(
        """
        INSERT INTO purchases
        (user_id, subscription_id, plan_type, chosen_platform,
         duration_days, amount, currency, payment_provider, status, paid_at)
        VALUES (?, ?, ?, ?, ?, ?, 'RUB', 'manual', 'paid', datetime('now'))
        """,
        (db_user_id, sub_id, plan_type, platform_for_purchase, days, amount),
    )

    conn.commit()
    conn.close()

    if device == "router":
        profile = build_openvpn_profile(email)
        qr_bytes = make_qr_png(profile)
        try:
            await bot.send_message(
                user_id,
                f"✅ Оплата подтверждена!\n\n"
                f"Подписка <b>OpenVPN / роутер</b> на <b>{days} дней</b>.\n"
                f"Сумма: <b>{int(amount)} ₽</b>\n\n"
                f"Ниже QR и файл <code>{email}.ovpn</code>.",
                parse_mode="HTML",
            )
            await bot.send_photo(
                user_id,
                BufferedInputFile(qr_bytes, filename=f"{email}_openvpn.png"),
                caption="📱 QR-код OpenVPN для роутера",
            )
            await bot.send_document(
                user_id,
                BufferedInputFile(profile.encode("utf-8"), filename=f"{email}.ovpn"),
                caption="📄 OpenVPN-профиль",
            )
            try:
                await callback.message.edit_text("✅ Подтверждено, OpenVPN QR выдан.")
            except TelegramBadRequest:
                pass
        except Exception as e:
            log.error("send openvpn: %s", e)
            await callback.message.answer(f"❌ Оплата записана, но QR не ушёл: {e}")
        return

    results = await asyncio.gather(
        *[asyncio.to_thread(create_or_extend_client, s, email, days) for s in SERVERS],
        return_exceptions=True,
    )
    links = []
    for i, result in enumerate(results):
        if isinstance(result, tuple) and result[0] and result[1]:
            links.append(
                build_vless_link(
                    SERVERS[i]["ip"], SERVERS[i]["label"], result[1], result[0], email
                )
            )

    if links:
        await update_github_file_completely(email, links)
        try:
            await bot.send_message(
                user_id,
                f"✅ Оплата подтверждена!\n\n"
                f"Подписка <b>{plan_type}</b> на <b>{days} дней</b>.\n\n"
                f"<code>https://raw.githubusercontent.com/{GITHUB_REPO}/main/{email}.txt</code>\n\n"
                f"При продлении ссылку заново добавлять не нужно.",
                parse_mode="HTML",
            )
            try:
                instr = INSTRUCTION_IOS if device == "ios" else INSTRUCTION_ANDROID
                await bot.send_document(user_id, FSInputFile(instr), caption="📄 Инструкция")
            except Exception as e:
                log.error("instruction: %s", e)
        except Exception as e:
            log.error("send user: %s", e)
        try:
            await callback.message.edit_text("✅ Подтверждено, конфиг выдан.")
        except TelegramBadRequest:
            pass
    else:
        await callback.message.answer("❌ Не удалось создать клиентов на серверах.")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.answer()
    data = callback.data.split("_")
    user_id = int(data[1])
    email = data[2] if len(data) > 2 else None
    if email:
        await _delete_payment_notifications(email)

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE user_subscriptions SET status = 'cancelled'
        WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
          AND status = 'pending'
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()

    try:
        await callback.message.edit_text("Заявка отклонена.")
    except TelegramBadRequest:
        await callback.message.answer("Заявка отклонена.")
    try:
        await bot.send_message(user_id, "❌ Заявка отклонена.")
    except Exception:
        pass


@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.answer()
    await show_admin_panel(callback)


async def show_admin_panel(target):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Массовое обновление", callback_data="admin_mass_update")],
            [InlineKeyboardButton(text="🗑 Удалить клиента", callback_data="admin_delete_client")],
            [InlineKeyboardButton(text="➕ Временный конфиг", callback_data="admin_create_temp_config")],
            [InlineKeyboardButton(text="📋 Все клиенты", callback_data="admin_all_clients")],
            [InlineKeyboardButton(text="📊 Excel", callback_data="admin_export_excel")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")],
        ]
    )
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text("Админ-панель", reply_markup=kb)
        except TelegramBadRequest:
            await target.message.answer("Админ-панель", reply_markup=kb)
    else:
        await target.answer("Админ-панель", reply_markup=kb)


async def main():
    init_db()
    ensure_db_columns()
    asyncio.create_task(notify_expiring_subscriptions())
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())