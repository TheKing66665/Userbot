from __future__ import annotations
import asyncio, json, logging, random, socket, time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import socks  # type: ignore
from telethon import TelegramClient, events, errors
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

# ──────────────────────────────  CONFIG  ──────────────────────────────────────
ACCOUNTS_CFG_PATH = Path("accounts.json")
BROADCAST_PATH   = Path("broadcast.json")

DEVICE_MODEL   = "iPhone 14 Pro"
SYSTEM_VERSION = "iOS 16.0"
APP_VERSION    = "8.9.0"

BASE_MIN_DELAY   = 30      # базовая пауза перед повторным подключением
LOWER_DELAY_LIMIT = 25
MAX_GROUPS_PER_ACCOUNT = 12
PER_MSG_SLEEP   = (5, 9)   # сон между чатами
SCHEDULE_OFFSET = (10, 30) # отложка (UTC) 10-30 сек

CREATION_COOLDOWN = 90     # после новой авторизации
CHECK_COOLDOWN    = 35     # мин. между проверками сессии того же акка


logging.basicConfig(
    handlers=[logging.FileHandler("manager.log", encoding="utf-8"),
              logging.StreamHandler()],
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ─────────────────────────────  MODELS  ───────────────────────────────────────
@dataclass
class ProxyCfg:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    type: str = "SOCKS5"

    def as_tuple(self):
        return (socks.SOCKS5, self.host, self.port, True, self.username, self.password)


@dataclass
class AccountData:
    name: str
    api_id: int
    api_hash: str
    phone: str
    session_string: str
    proxy: Optional[ProxyCfg] = None
    last_used: datetime = field(default_factory=lambda: datetime.min)
    delay: int = BASE_MIN_DELAY
    ok_streak: int = 0

    @property
    def kwargs(self):
        kw = dict(
            session=StringSession(self.session_string),
            api_id=self.api_id,
            api_hash=self.api_hash,
            device_model=DEVICE_MODEL,
            system_version=SYSTEM_VERSION,
            app_version=APP_VERSION,
        )
        if self.proxy:
            kw["proxy"] = self.proxy.as_tuple()
        return kw

# ────────────────────────────  STORAGE  ───────────────────────────────────────
def load_accounts() -> List[AccountData]:
    if not ACCOUNTS_CFG_PATH.exists():
        return []
    raw = json.loads(ACCOUNTS_CFG_PATH.read_text(encoding="utf-8"))
    out: list[AccountData] = []
    for a in raw.get("accounts", []):
        proxy = ProxyCfg(**a["proxy"]) if "proxy" in a else None
        out.append(AccountData(proxy=proxy, **{k: a[k] for k in (
            "name", "api_id", "api_hash", "phone", "session_string")}))
    return out


def save_accounts(lst: List[AccountData]):
    data = {"accounts": [
        {**{k: v for k, v in asdict(a).items()
            if k not in {"last_used", "delay", "ok_streak"}},
         **({"proxy": asdict(a.proxy)} if a.proxy else {})}
        for a in lst]}
    ACCOUNTS_CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

# ────────────────────────────  HELPERS  ───────────────────────────────────────
def probe_proxy(host: str, port: int,
                username: Optional[str] = None, password: Optional[str] = None,
                timeout: int = 3) -> int | None:
    try:
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, host, port, rdns=True, username=username, password=password)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.connect(("core.telegram.org", 443))
        s.close()
        return int((time.perf_counter() - t0) * 1000)
    except OSError:
        return None




async def _adaptive_sleep(acc: AccountData):
    since = (datetime.now() - acc.last_used).total_seconds()
    wait = max(acc.delay, CHECK_COOLDOWN) - since
    if wait > 0:
        await asyncio.sleep(wait)

# ────────────────────────────  VALIDATION  ────────────────────────────────────
async def validate_session(acc: AccountData) -> bool:
    await _adaptive_sleep(acc)
    client = TelegramClient(**acc.kwargs)
    try:
        await client.connect()
        ok = await client.is_user_authorized()
        if ok:
            acc.ok_streak += 1
            if acc.ok_streak >= 10 and acc.delay > LOWER_DELAY_LIMIT:
                acc.delay -= 1
        else:
            acc.ok_streak = 0
        return ok
    except errors.FloodWaitError as e:
        acc.delay = max(acc.delay, e.seconds + random.randint(1, 3))
        acc.ok_streak = 0
        await asyncio.sleep(e.seconds)
        return False
    finally:
        acc.last_used = datetime.now()
        await client.disconnect()
        await asyncio.sleep(random.uniform(1, 4))


# ──────────────────────────  ACCOUNT CREATION  ───────────────────────────────
async def create_string_session(api_id: int, api_hash: str, phone: str,
                                proxy: Optional[ProxyCfg]):
    kw = dict(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
    )
    if proxy:
        kw["proxy"] = proxy.as_tuple()

    client = TelegramClient(**kw)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input("Код: ").strip()
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                pwd = input("Пароль 2FA: ").strip()
                await client.sign_in(password=pwd)
        return client.session.save()
    finally:
        await client.disconnect()


async def add_account_flow(accs: List[AccountData]):
    name = input("Метка: ").strip() or f"acc_{len(accs) + 1}"
    api_id = int(input("API_ID: "))
    api_hash = input("API_HASH: ").strip()

    while True:
        phone = input("Телефон (+…): ").strip().replace(" ", "").replace("-", "")
        if not phone.startswith("+"):
            phone = "+" + phone
        if phone[1:].isdigit():
            break

    proxy = None
    if input("SOCKS5 proxy? y/n ").lower().startswith("y"):
        full = input(" proxy (user:pass@ip:port или ip:port): ").strip()
        import re
        m = re.fullmatch(r"(?:(\w+):(\S+)@)?([\d.]+):(\d{2,5})", full)
        if not m:
            print("⚠️  Неверный формат прокси"); return
        user, pw, host, port = m.group(1), m.group(2), m.group(3), int(m.group(4))
        rtt = probe_proxy(host, port, username=user, password=pw)
        if rtt is None:
            print("⚠️  Прокси не отвечает.")
            if not input("Продолжить без проверки? y/n ").lower().startswith("y"):
                return
        else:
            print(f"✅ Прокси живой, RTT≈{rtt} мс.")
        proxy = ProxyCfg(host=host, port=port, username=user, password=pw)

    try:
        sess = await create_string_session(api_id, api_hash, phone, proxy)
    except Exception as e:
        print("Auth fail:", e)
        return

    accs.append(AccountData(name=name, api_id=api_id, api_hash=api_hash,
                            phone=phone, session_string=sess, proxy=proxy))
    save_accounts(accs)
    print("✅ Saved.  Остываем…")
    await asyncio.sleep(CREATION_COOLDOWN + random.uniform(3, 7))



# ─────────────────────────────  CAPTURE TEXT  ────────────────────────────────
async def capture_broadcast(accs: list[AccountData]):
    if not accs:
        print("Нет аккаунтов."); return

    PREFIX = "/рассылка "
    leader = accs[0]
    client = TelegramClient(**leader.kwargs)
    await client.start(proxy=leader.proxy.as_tuple() if leader.proxy else None)

    print('Отправьте в Избранное "/рассылка …" — жду 60 с…')
    done = asyncio.Event()

    async def handler(event):
        if event.is_private and event.out and event.chat_id == event.sender_id:
            if event.raw_text.startswith(PREFIX):
                full = event.raw_text
                text = full[len(PREFIX):]

                entities = []
                for ent in event.entities or []:
                    if ent.offset >= len(PREFIX):
                        ent = ent.to_dict()
                        ent["offset"] -= len(PREFIX)
                        entities.append(ent)

                BROADCAST_PATH.write_text(
                    json.dumps({"text": text, "entities": entities},
                               ensure_ascii=False),
                    encoding="utf-8")
                print(f"✅ Содержимое сохранено ({len(text)} симв.)")
                done.set()

    client.add_event_handler(handler, events.NewMessage)

    try:
        await asyncio.wait_for(done.wait(), timeout=60)
    except asyncio.TimeoutError:
        print("⏳ Не увидел /рассылка … за 60 с.")
    finally:
        await client.disconnect()


# ─────────────────────────────  BROADCAST  ───────────────────────────────────
async def run_broadcast(accs: list[AccountData]):
    if not BROADCAST_PATH.exists():
        print("Сначала пункт 3 — задайте текст."); return

    payload = json.loads(BROADCAST_PATH.read_text(encoding="utf-8"))
    text = payload["text"]

    from telethon.tl import types as _t
    def ent_from_dict(d: dict):
        cls = getattr(_t, d.pop("_"))
        return cls(**d)

    fmt_entities = [ent_from_dict(e) for e in payload["entities"]]

    for acc in accs:
        print(f"==> {acc.name}  (delay {acc.delay}s)")
        await _adaptive_sleep(acc)

        client = TelegramClient(**acc.kwargs)
        try:
            await client.connect()

            dialogs = [d for d in await client.get_dialogs()
                       if (d.is_group or getattr(d.entity, "megagroup", False))
                       and not getattr(d.entity, "bot", False)]
            random.shuffle(dialogs)

            sent = 0
            for dlg in dialogs:
                if sent >= MAX_GROUPS_PER_ACCOUNT:
                    break
                when = datetime.utcnow() + timedelta(
                    seconds=random.randint(*SCHEDULE_OFFSET))
                try:
                    await client.send_message(
                        dlg.entity, text,
                        formatting_entities=fmt_entities,
                        schedule=when
                    )
                    logging.info("Scheduled → %s (%s)", dlg.name, acc.name)
                    sent += 1
                    await asyncio.sleep(random.uniform(*PER_MSG_SLEEP))

                except errors.ChatWriteForbiddenError:
                    logging.info("Skip (no rights) %s", dlg.name)
                    continue

                except errors.FloodWaitError as e:
                    logging.warning("%s FloodWait %s", acc.name, e.seconds)
                    acc.delay = max(acc.delay, e.seconds + random.randint(1, 3))
                    break

            print(f"   Отложено: {sent} групп.")

        finally:
            await client.disconnect()
            acc.last_used = datetime.now()
            await asyncio.sleep(random.uniform(5, 10))

    save_accounts(accs)


# ───────────────────────────────  CLI  ───────────────────────────────────────
async def list_flow(accs: List[AccountData]):
    for acc in accs:
        ok = await validate_session(acc)
        mark = "🟢" if ok else "🔴"
        print(f" {mark} {acc.name}  delay={acc.delay}s")
    save_accounts(accs)


async def main():
    accs = load_accounts()
    MENU = ("\n1 Добавить аккаунт\n2 Проверить сессии\n3 Ввести текст для рассылки"
            "\n4 Начать рассылку\n0 Выход\n")
    while True:
        choice = input(MENU + "> ").strip()
        if choice == "1":
            await add_account_flow(accs)
        elif choice == "2":
            await list_flow(accs)
        elif choice == "3":
            await capture_broadcast(accs)
        elif choice == "4":
            await run_broadcast(accs)
        elif choice == "0":
            break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑  Остановлено")
