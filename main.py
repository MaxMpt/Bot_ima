import asyncio
import logging
import math
import sqlite3
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, FSInputFile
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

SERVERS = [
    {"ip": "195.63.144.164", "label": "Amsterdam-3", "url": "https://195.63.144.164:2053/598138a170495e2917d81cf2d7e1617d/panel/api", "token": "rLdhD2DK8Ntan1oB7NDTUERJFCT9LYarVgNdLT0KQrEHQMmS"},
    {"ip": "89.124.64.16",   "label": "Amsterdam-1", "url": "https://89.124.64.16:2053/cc01cf97a2729bee5a159848470f7716/panel/api", "token": "a8MYoaSe9vWFxiA6CDZZ9ifqC1HAwcCkmSZAkCCLxneaCt7Y"},
    {"ip": "103.112.70.204", "label": "Amsterdam-Ch", "url": "http://103.112.70.204:35380/2CGdTIvQfh00N1XGnv/panel/api", "token": "Q87RsvJVdhKzQvxt6Vp6ARY5oz5FON8ndzYXY3zdjBx3MXGu"},
]

SESSION = requests.Session()
PRICES = {30: 219, 90: 599, 365: 2100}

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
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'cancelled', 'pending')),
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
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed', 'refunded', 'cancelled')),
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


async def create_subscription(user_id: int, username: str, days: int, device: str = "ios"):
    def _create():
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            db_user_id = row[0]
        else:
            cursor.execute("INSERT INTO users (telegram_id, username, role) VALUES (?, ?, 'client')", (user_id, username))
            db_user_id = cursor.lastrowid

        plan_type = "router" if device == "router" else "mobile"
        preferred_platform = device if device in ("android", "ios") else "ios"
        start_date = datetime.now().strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        cursor.execute("""
            INSERT INTO user_subscriptions (user_id, plan_type, preferred_platform, start_date, end_date, status, duration_days)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(user_id, plan_type) WHERE status = 'active'
            DO UPDATE SET end_date = excluded.end_date, duration_days = excluded.duration_days, status = 'pending'
        """, (db_user_id, plan_type, preferred_platform, start_date, end_date, days))

        conn.commit()
        conn.close()
        return f"tg{user_id}"

    return await asyncio.to_thread(_create)


# ====================== 3X-UI ======================

def make_request(url, token, method="GET", max_retries=4, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}
    for attempt in range(max_retries):
        try:
            return SESSION.request(method, url, headers=headers, verify=False, timeout=35, **kwargs)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1.5)
    return None


def get_first_inbound(server):
    resp = make_request(f"{server['url']}/inbounds/list", server['token'])
    if resp and resp.json().get("success"):
        return resp.json().get("obj", [None])[0]
    return None


def get_client_from_inbound(inbound, email):
    if not inbound:
        return None
    for client in inbound.get("settings", {}).get("clients", []):
        if client.get("email") == email:
            return client
    return None


def add_new_client(server, email, days=0):
    inbound = get_first_inbound(server)
    if not inbound:
        return None, None
    expiry = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
    client_uuid = str(uuid.uuid4())
    payload = {"client": {"id": client_uuid, "email": email, "flow": "xtls-rprx-vision", "limitIp": 1, "totalGB": 0, "enable": True, "expiryTime": expiry}, "inboundIds": [inbound["id"]]}
    resp = make_request(f"{server['url']}/clients/add", server['token'], method="POST", json=payload)
    if resp and resp.status_code == 200 and resp.json().get("success"):
        return client_uuid, inbound
    return None, None


def extend_client_expiry(server, email: str, additional_days: int):
    inbound = get_first_inbound(server)
    if not inbound:
        return False
    client = get_client_from_inbound(inbound, email)
    if not client:
        return False
    current = client.get("expiryTime", 0)
    now = int(datetime.now().timestamp() * 1000)
    base = current if current > now else now
    new_expiry = base + (additional_days * 24 * 60 * 60 * 1000)
    payload = {"id": client["id"], "email": email, "flow": client.get("flow", "xtls-rprx-vision"), "limitIp": client.get("limitIp", 1), "totalGB": client.get("totalGB", 0), "enable": True, "expiryTime": new_expiry}
    resp = make_request(f"{server['url']}/clients/update/{email}", server['token'], method="POST", json=payload)
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
    resp = make_request(url, server['token'], method="POST")
    if resp and resp.status_code == 200:
        try:
            return resp.json().get("success", False)
        except:
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
            except:
                pass

    # Удаляем подписку из базы
    try:
        telegram_id = int(email.replace("tg", ""))
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM user_subscriptions 
            WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
        """, (telegram_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Ошибка удаления из базы: %s", e)

    # Удаляем файл из GitHub
    try:
        g = gh.Github(auth=gh.Auth.Token(GITHUB_TOKEN))
        repo = g.get_repo(GITHUB_REPO)
        file = repo.get_contents(f"{email}.txt")
        repo.delete_file(f"{email}.txt", f"Delete {email}", file.sha)
    except:
        pass

    return deleted


def build_vless_link(server_ip, label, inbound, client_uuid, name):
    reality = inbound["streamSettings"]["realitySettings"]
    pk = reality["settings"]["publicKey"]
    sid = reality.get("shortIds", [""])[0]
    spx = reality.get("spiderX", "/")
    port = inbound.get("port", 443)
    return f"vless://{client_uuid}@{server_ip}:{port}?encryption=none&flow=xtls-rprx-vision&fp=firefox&pbk={pk}&security=reality&sid={sid}&sni=www.sony.com&spx={quote(spx, safe='')}&type=tcp#{label}-{name}"


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
            except:
                repo.create_file(filename, f"Create {name}", content)
        try:
            await asyncio.to_thread(_update)
        except Exception as e:
            log.error("GitHub error для %s: %s", name, e)


def _check_server_days(server, email: str) -> int:
    inbound = get_first_inbound(server)
    if not inbound:
        return 0
    client = get_client_from_inbound(inbound, email)
    if client and client.get("expiryTime", 0) > 0:
        remaining_ms = client["expiryTime"] - int(datetime.now().timestamp() * 1000)
        if remaining_ms > 0:
            return math.ceil(remaining_ms / (1000 * 60 * 60 * 24))
    return 0


async def get_user_remaining_days(email: str) -> int:
    results = await asyncio.gather(*[asyncio.to_thread(_check_server_days, server, email) for server in SERVERS], return_exceptions=True)
    return max((r for r in results if isinstance(r, int)), default=0)


async def _notify_one_user(user_id: int, email: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            remaining = await get_user_remaining_days(email)
            if remaining <= 1:
                await bot.send_message(user_id, "⚠️ <b>Внимание!</b>\n\nВаша подписка VPN заканчивается в течение 1 дня.\nПродлите подписку.", parse_mode="HTML")
        except TelegramForbiddenError:
            pass
        except Exception as e:
            log.error("Ошибка уведомления %s: %s", user_id, e)


async def notify_expiring_subscriptions():
    while True:
        await asyncio.sleep(86400)
        try:
            conn = sqlite3.connect(DB_NAME, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.telegram_id 
                FROM user_subscriptions us
                JOIN users u ON u.id = us.user_id
                WHERE us.status = 'active' AND date(us.end_date) <= date('now', '+1 day')
            """)
            users = cursor.fetchall()
            conn.close()

            for (telegram_id,) in users:
                try:
                    await bot.send_message(telegram_id, "⚠️ <b>Внимание!</b>\n\nВаша подписка VPN заканчивается в течение 1 дня.\nПродлите подписку.", parse_mode="HTML")
                except:
                    pass
        except Exception as e:
            log.error("Ошибка в notify_expiring_subscriptions: %s", e)


# ====================== МАССОВОЕ ОБНОВЛЕНИЕ ======================

async def _sync_one_user(email: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        remaining_days = await get_user_remaining_days(email) or 30
        if remaining_days > 400:
            remaining_days = 30

        links = []
        for server in SERVERS:
            inbound = get_first_inbound(server)
            if not inbound:
                continue
            client = get_client_from_inbound(inbound, email)
            if client:
                links.append(build_vless_link(server["ip"], server["label"], inbound, client["id"], email))
            else:
                result = await asyncio.to_thread(add_new_client, server, email, remaining_days)
                if result and result[0] and result[1]:
                    links.append(build_vless_link(server["ip"], server["label"], result[1], result[0], email))

        if links:
            await update_github_file_completely(email, links)
            return True
        return False


async def sync_all_clients():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users")
    telegram_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    emails = [f"tg{tid}" for tid in telegram_ids]
    total = len(emails)
    semaphore = asyncio.Semaphore(MASS_CONCURRENCY)
    results = await asyncio.gather(*[_sync_one_user(email, semaphore) for email in emails], return_exceptions=True)
    updated = sum(1 for r in results if r is True)
    return total, updated, total - updated


@dp.callback_query(F.data == "admin_mass_update")
async def admin_mass_update(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.answer()
    await callback.message.answer("🔄 Запущено массовое обновление...")
    total, updated, failed = await sync_all_clients()
    await callback.message.answer(f"✅ Готово. Всего: {total}, Обновлено: {updated}, Ошибок: {failed}")
    await show_admin_panel(callback)


# ====================== АДМИН ======================

@dp.callback_query(F.data == "admin_all_clients")
async def admin_all_clients(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            u.telegram_id,
            u.username,
            u.role,
            COALESCE(SUM(us.duration_days), 0) as total_days,
            MAX(us.end_date) as last_end_date
        FROM users u
        LEFT JOIN user_subscriptions us ON us.user_id = u.id
        GROUP BY u.id
        ORDER BY u.id
    """)
    clients = cursor.fetchall()
    conn.close()

    if not clients:
        await callback.message.answer("Пользователей нет.")
        return await show_admin_panel(callback)

    text = "📋 <b>Все пользователи:</b>\n\n"

    for telegram_id, username, role, total_days, last_end_date in clients:
        display_name = f"@{username}" if username else f"tg{telegram_id}"

        remaining = 0
        if last_end_date:
            try:
                exp = datetime.strptime(last_end_date.split()[0], "%Y-%m-%d").date()
                remaining = max(0, (exp - datetime.now().date()).days)
            except:
                remaining = 0

        text += f"{display_name} | Роль: {role} | Всего дней: {total_days} | Осталось: {remaining}\n"

    await callback.message.answer(text, parse_mode="HTML")
    await show_admin_panel(callback)


@dp.callback_query(F.data == "admin_create_temp_config")
async def admin_create_temp_config_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Android", callback_data="temp_device_android")],
        [InlineKeyboardButton(text="iOS", callback_data="temp_device_ios")]
    ])
    await callback.message.answer("Выберите устройство для временного конфига:", reply_markup=kb)
    await state.set_state(AdminStates.waiting_for_temp_device)


@dp.callback_query(AdminStates.waiting_for_temp_device)
async def admin_temp_config_device(callback: CallbackQuery, state: FSMContext):
    device = callback.data.split("_")[2]
    await state.update_data(temp_device=device)
    await callback.message.edit_text("Введите email (или логин) для временного конфига:")
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
    device = data.get("temp_device", "android")
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("Введите число дней.")
        return

    await state.clear()
    await message.answer(f"Создаю конфиг для <code>{email}</code> на {days} дней...", parse_mode="HTML")

    results = await asyncio.gather(*[asyncio.to_thread(add_new_client, server, email, days) for server in SERVERS], return_exceptions=True)
    links = []
    for i, result in enumerate(results):
        if isinstance(result, tuple) and result[0] and result[1]:
            links.append(build_vless_link(SERVERS[i]["ip"], SERVERS[i]["label"], result[1], result[0], email))

    if links:
        await update_github_file_completely(email, links)
        await message.answer(f"✅ Временный конфиг создан.\n\nСсылка: https://raw.githubusercontent.com/{GITHUB_REPO}/main/{email}.txt")
    else:
        await message.answer("Не удалось создать конфиг.")

    await show_admin_panel(message)


@dp.callback_query(F.data == "admin_delete_client")
async def admin_delete_client_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.message.answer("Введите email клиента для удаления:")
    await state.set_state(AdminStates.waiting_for_email_to_delete)


@dp.message(AdminStates.waiting_for_email_to_delete)
async def admin_delete_client_confirm(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    email = message.text.strip()
    await state.clear()
    deleted = await asyncio.to_thread(delete_client_everywhere, email)
    await message.answer(f"✅ Удалено с {deleted} серверов")
    await show_admin_panel(message)


@dp.callback_query(F.data == "admin_export_excel")
async def admin_export_excel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.message.answer("Генерирую Excel...")

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.username, u.telegram_id, us.plan_type, us.preferred_platform, us.status, us.end_date
        FROM user_subscriptions us
        JOIN users u ON u.id = us.user_id
    """)
    clients = cursor.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.append(["Username", "Telegram ID", "Тип", "Платформа", "Статус", "Осталось дней"])

    for username, telegram_id, plan_type, platform, status, end_date in clients:
        remaining = 0
        if end_date:
            try:
                exp = datetime.strptime(end_date.split()[0], "%Y-%m-%d").date()
                remaining = max(0, (exp - datetime.now().date()).days)
            except:
                pass
        ws.append([f"@{username}" if username else "", telegram_id, plan_type, platform or "", status, remaining])

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    await callback.message.answer_document(
        BufferedInputFile(buffer.read(), filename="clients.xlsx"),
        caption="📊 Список клиентов"
    )
    await show_admin_panel(callback)


# ====================== ПОЛЬЗОВАТЕЛЬ ======================

def get_main_keyboard(user_id: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить дни подписки", callback_data="check_days")],
        [InlineKeyboardButton(text="Купить / Продлить подписку", callback_data="buy_subscription")]
    ])
    if is_admin(user_id):
        kb.inline_keyboard.append([InlineKeyboardButton(text="Админ-панель", callback_data="admin_panel")])
    return kb


@dp.message(Command("start"))
async def start_cmd(message: Message):
    remaining = await get_user_remaining_days(f"tg{message.from_user.id}")
    text = f"Привет! У тебя осталось <b>{remaining} дней</b> подписки." if remaining > 0 else "Привет! У тебя пока нет активной подписки."
    await message.answer(text, reply_markup=get_main_keyboard(message.from_user.id), parse_mode="HTML")


@dp.callback_query(F.data == "check_days")
async def check_days(callback: CallbackQuery):
    await callback.answer()
    remaining = await get_user_remaining_days(f"tg{callback.from_user.id}")
    text = f"У тебя осталось <b>{remaining} дней</b> подписки." if remaining > 0 else "У тебя пока нет активной подписки."
    await callback.message.answer(text, parse_mode="HTML")


@dp.callback_query(F.data == "buy_subscription")
async def choose_device(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Android", callback_data="device_android")],
        [InlineKeyboardButton(text="iOS", callback_data="device_ios")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
    ])
    await callback.message.edit_text("Выберите устройство:", reply_markup=kb)
    await state.set_state(SubscriptionStates.choosing_device)


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_text("Главное меню", reply_markup=get_main_keyboard(callback.from_user.id))
    except TelegramBadRequest:
        pass


@dp.callback_query(SubscriptionStates.choosing_device)
async def choose_duration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    device = callback.data.split("_")[1]
    await state.update_data(device=device)

    user_id = callback.from_user.id
    email = f"tg{user_id}"
    remaining = await get_user_remaining_days(email)

    text = f"⚠️ У тебя осталось <b>{remaining} дней</b>.\n\nВыберите срок продления:" if remaining > 0 else "Выберите срок подписки:"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 месяц — {PRICES[30]} ₽", callback_data="duration_30")],
        [InlineKeyboardButton(text=f"3 месяца — {PRICES[90]} ₽", callback_data="duration_90")],
        [InlineKeyboardButton(text=f"12 месяцев — {PRICES[365]} ₽", callback_data="duration_365")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_device")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await state.set_state(SubscriptionStates.choosing_duration)


@dp.callback_query(F.data == "back_to_device")
async def back_to_device(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Android", callback_data="device_android")],
        [InlineKeyboardButton(text="iOS", callback_data="device_ios")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
    ])
    try:
        await callback.message.edit_text("Выберите устройство:", reply_markup=kb)
    except TelegramBadRequest:
        pass
    await state.set_state(SubscriptionStates.choosing_device)


@dp.callback_query(SubscriptionStates.choosing_duration)
async def create_order(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    days = int(callback.data.split("_")[1])
    price = PRICES.get(days, 0)
    data = await state.get_data()
    device = data.get("device", "android")

    user_id = callback.from_user.id
    username = callback.from_user.username or f"user{user_id}"
    email = await create_subscription(user_id, username, days, device)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить подписку", url=TINKOFF_COLLECTION_LINK)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid_{user_id}_{days}_{email}_{device}")]
    ])

    await callback.message.edit_text(
        f"✅ Заявка создана!\n\nСумма: <b>{price} ₽</b> за <b>{days} дней</b>\n\nНажми кнопку ниже для оплаты.\nПосле оплаты нажми «Я оплатил».",
        reply_markup=kb, parse_mode="HTML"
    )
    await state.clear()

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🆕 <b>Новая заявка!</b>\n\nПользователь: @{username}\nTelegram ID: <code>{user_id}</code>\nКонфиг: <code>{email}</code>\nУстройство: {device} | Срок: <b>{days} дней</b> | Сумма: <b>{price} ₽</b>", parse_mode="HTML")
        except:
            pass


@dp.callback_query(F.data.startswith("paid_"))
async def user_confirmed_payment(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    user_id = int(parts[1])
    days = int(parts[2])
    email = parts[3]
    device = parts[4] if len(parts) > 4 else "android"
    username = callback.from_user.username or f"user{user_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"approve_{user_id}_{days}_{email}_{device}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}_{email}")]
    ])

    message_ids = {}
    for admin_id in ADMIN_IDS:
        try:
            msg = await bot.send_message(admin_id, f"💰 Пользователь подтвердил оплату!\n\n@{username} (ID: <code>{user_id}</code>)\nEmail: <code>{email}</code>\nСрок: {days} дней | Устройство: {device}", parse_mode="HTML", reply_markup=kb)
            message_ids[str(admin_id)] = msg.message_id
        except:
            pass

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO payment_notifications (email, message_ids) VALUES (?, ?)", (email, json.dumps(message_ids)))
    conn.commit()
    conn.close()

    await callback.message.edit_text("Спасибо! Администратор проверит оплату и активирует подписку.", parse_mode="HTML")


async def _delete_payment_notifications(email: str):
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT message_ids FROM payment_notifications WHERE email = ?", (email,))
    row = cursor.fetchone()
    if row:
        message_ids = json.loads(row[0])
        for admin_id_str, msg_id in message_ids.items():
            try:
                await bot.delete_message(chat_id=int(admin_id_str), message_id=msg_id)
            except:
                pass
        cursor.execute("DELETE FROM payment_notifications WHERE email = ?", (email))
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

    await _delete_payment_notifications(email)

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return await callback.message.answer("Пользователь не найден в базе.")

    db_user_id = row[0]
    plan_type = "router" if device == "router" else "mobile"
    preferred_platform = device if device in ("android", "ios") else "ios"
    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    cursor.execute("""
        INSERT INTO user_subscriptions (user_id, plan_type, preferred_platform, start_date, end_date, status, duration_days)
        VALUES (?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(user_id, plan_type) WHERE status = 'active'
        DO UPDATE SET end_date = excluded.end_date, duration_days = excluded.duration_days, status = 'active'
    """, (db_user_id, plan_type, preferred_platform, start_date, end_date, days))
    conn.commit()
    conn.close()

    results = await asyncio.gather(*[asyncio.to_thread(create_or_extend_client, server, email, days) for server in SERVERS], return_exceptions=True)
    links = []
    for i, result in enumerate(results):
        if isinstance(result, tuple) and result[0] and result[1]:
            links.append(build_vless_link(SERVERS[i]["ip"], SERVERS[i]["label"], result[1], result[0], email))

    if links:
        await update_github_file_completely(email, links)
        try:
            await bot.send_message(user_id, f"✅ Оплата подтверждена!\n\nПодписка активирована на <b>{days} дней</b>.\n\nСсылка: <code>https://raw.githubusercontent.com/{GITHUB_REPO}/main/{email}.txt</code>", parse_mode="HTML")
            instruction_file = INSTRUCTION_IOS if device == "ios" else INSTRUCTION_ANDROID
            await bot.send_document(user_id, FSInputFile(instruction_file), caption="📄 Инструкция по установке VPN")
        except Exception as e:
            log.error("Ошибка отправки пользователю: %s", e)
    else:
        await callback.message.answer("❌ Не удалось создать/продлить клиента.")


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
    cursor.execute("UPDATE user_subscriptions SET status = 'cancelled' WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)", (user_id,))
    conn.commit()
    conn.close()

    await callback.message.answer("Заявка отклонена.")
    try:
        await bot.send_message(user_id, "❌ Ваша заявка была отклонена.", parse_mode="HTML")
    except:
        pass


@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.answer()
    await show_admin_panel(callback)


async def show_admin_panel(target):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Массовое обновление", callback_data="admin_mass_update")],
        [InlineKeyboardButton(text="🗑 Удалить клиента по email", callback_data="admin_delete_client")],
        [InlineKeyboardButton(text="➕ Создать временный конфиг", callback_data="admin_create_temp_config")],
        [InlineKeyboardButton(text="📋 Все клиенты", callback_data="admin_all_clients")],
        [InlineKeyboardButton(text="📊 Excel со всеми клиентами", callback_data="admin_export_excel")],
        [InlineKeyboardButton(text="◀️ Назад в главное меню", callback_data="back_to_main")]
    ])
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text("Админ-панель", reply_markup=kb)
        except TelegramBadRequest:
            await target.message.answer("Админ-панель", reply_markup=kb)
    else:
        await target.answer("Админ-панель", reply_markup=kb)


async def main():
    init_db()
    asyncio.create_task(notify_expiring_subscriptions())
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())