import subprocess
import os
import logging
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("vpn_bot")

# ====================== НАСТРОЙКИ WIREGUARD (из .env) ======================
WG_INTERFACE = os.getenv("WG_INTERFACE", "wg0")
WG_SERVER_PUBLIC_KEY = os.getenv("WG_SERVER_PUBLIC_KEY", "")
WG_PORT = int(os.getenv("WG_PORT", "51820"))
WG_DNS = os.getenv("WG_DNS", "1.1.1.1, 8.8.8.8")
WG_CLIENT_DIR = os.getenv("WG_CLIENT_DIR", "/etc/wireguard/client")

# Полный путь к wg (чтобы работало из venv)
WG_BIN = "/usr/bin/wg"
# ========================================================================


def get_next_available_ip() -> str | None:
    """Возвращает следующий свободный IP в подсети 10.8.0.0/24"""
    try:
        result = subprocess.run(
            [WG_BIN, "show", WG_INTERFACE, "allowed-ips"],
            capture_output=True, text=True, check=True
        )
        used_ips = set()
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and "/" in parts[1]:
                ip = parts[1].split("/")[0]
                if ip.startswith("10.8.0."):
                    used_ips.add(ip)

        for i in range(2, 255):
            ip = f"10.8.0.{i}"
            if ip not in used_ips:
                return ip
        return None
    except Exception as e:
        log.error("Ошибка при получении свободного IP: %s", e)
        return None


def create_wireguard_client(user_id: int, username: str, days: int) -> tuple[str | None, str | None]:
    """
    Создаёт WireGuard клиента и возвращает (client_name, путь_к_конфигу)
    """
    client_name = f"wg_{user_id}"
    client_ip = get_next_available_ip()

    if not client_ip:
        log.error("Не удалось найти свободный IP для клиента %s", client_name)
        return None, None

    try:
        # Генерируем ключи
        private_key = subprocess.check_output([WG_BIN, "genkey"]).decode().strip()
        public_key = subprocess.check_output(
            [WG_BIN, "pubkey"], input=private_key.encode()
        ).decode().strip()

        # Добавляем клиента на сервер
        subprocess.run([
            WG_BIN, "set", WG_INTERFACE,
            "peer", public_key,
            "allowed-ips", f"{client_ip}/32"
        ], check=True)

        # Создаём папку для клиентов
        os.makedirs(WG_CLIENT_DIR, exist_ok=True)
        config_path = os.path.join(WG_CLIENT_DIR, f"{client_name}.conf")

        # Получаем внешний IP сервера
        try:
            server_ip = subprocess.check_output(
                ["curl", "-s", "ifconfig.me"], timeout=10
            ).decode().strip()
        except:
            server_ip = "YOUR_SERVER_IP"  # fallback

        # Формируем конфиг
        config_content = f"""[Interface]
PrivateKey = {private_key}
Address = {client_ip}/24
DNS = {WG_DNS}

[Peer]
PublicKey = {WG_SERVER_PUBLIC_KEY}
AllowedIPs = 0.0.0.0/0
Endpoint = {server_ip}:{WG_PORT}
PersistentKeepalive = 25
"""

        with open(config_path, "w") as f:
            f.write(config_content)

        log.info("WireGuard клиент создан: %s (IP: %s)", client_name, client_ip)
        return client_name, config_path

    except subprocess.CalledProcessError as e:
        log.error("Ошибка выполнения команды wg: %s", e)
        return None, None
    except Exception as e:
        log.error("Ошибка при создании WireGuard клиента: %s", e)
        return None, None