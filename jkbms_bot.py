import asyncio
import logging
import struct
import json
import os
import subprocess
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from bleak import BleakClient, BleakScanner

TELEGRAM_TOKEN = ""
GOD_ID = 575590315
DB_FILE = "akb_database.json"
USERS_FILE = "users.json"
REGISTRY_FILE = "bms_registry.json"
PRESET_FILE = "preset.json"
AUDIT_FILE = "audit.log"
WORKSHOP_FILE = "workshop.json"
CMD_PASS_FILE = "cmd_password.json"
SALARY_FILE = "salary.json"
SECRETS_FILE = "secrets.json"
# Заводський .jkcfg файл (читається з файлу)
FACTORY_JKCFG_FILE = "150bms.jkcfg"
def load_factory_jkcfg():
    try:
        with open(FACTORY_JKCFG_FILE, "rb") as f:
            return f.read()
    except:
        return None
GEMINI_API_KEY = ""
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
JK_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
JK_CHAR_UUID2 = "0000ffe2-0000-1000-8000-00805f9b34fb"
CMD_DEVICE_INFO = bytes([0xAA,0x55,0x90,0xEB,0x97,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0x11])
CMD_CELL_INFO   = bytes([0xAA,0x55,0x90,0xEB,0x96,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0x10])

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ── JSON ──────────────────────────────────────────────────────
def load_json(f, d):
    if os.path.exists(f):
        with open(f,"r",encoding="utf-8-sig") as fp: return json.load(fp)
    return d

def save_json(f, d):
    # Атомарний запис: спочатку у tmp, потім підмінюємо оригiнал.
    # Якщо бот впаде під час запису — оригiнальний файл залишається цiлим.
    tmp = f + ".tmp"
    with open(tmp,"w",encoding="utf-8") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, f)

# ── ВАЛІДАЦІЯ ─────────────────────────────────────────────────
import re
# Дозволено: цифри, латиниця, кирилиця (укр/рос), дефiс, пiдкреслення
AKB_NUM_RE = re.compile(r'^[0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ_\-]{1,20}$')

def validate_akb_num(num):
    """Перевiряє коректнiсть номера АКБ.
    Повертає (True, очищений_номер) або (False, текст_помилки)."""
    if not num: return False, "❌ Номер не може бути порожнiм"
    num = num.strip()
    if not num: return False, "❌ Номер не може бути порожнiм"
    if len(num) > 20: return False, "❌ Номер занадто довгий (макс 20 символiв)"
    if not AKB_NUM_RE.match(num):
        return False, ("❌ Невiрний номер АКБ!\n"
                       "Дозволено: цифри, букви, дефiс, пiдкреслення\n"
                       "Приклад: 34137 або АКБ-001")
    return True, num

akb_db       = load_json(DB_FILE, {})
users_db     = load_json(USERS_FILE, {str(GOD_ID): {"role":"god","permissions":["all"],"name":"Kaatastroffaa"}})
bms_registry = load_json(REGISTRY_FILE, {})
workshop_db  = load_json(WORKSHOP_FILE, {})
cmd_pass_cfg = load_json(CMD_PASS_FILE, {"password": None, "hint": None, "verified": []})
salary_db    = load_json(SALARY_FILE, {})

# ── СЕКРЕТИ ──────────────────────────────────────────────────
_secrets = load_json(SECRETS_FILE, {})
TELEGRAM_TOKEN = _secrets.get("telegram_token", "")
GEMINI_API_KEY = _secrets.get("gemini_api_key", "")
if not TELEGRAM_TOKEN:
    print("❌ ПОМИЛКА: secrets.json не знайдено або порожнiй telegram_token!")
    print("   Створи файл secrets.json поряд з ботом з ключами:")
    print('   {"telegram_token": "...", "gemini_api_key": "..."}')
    import sys
    sys.exit(1)

# Завантажуємо audit.log в пам'ять при старті
def load_audit_log():
    logs = []
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                logs = [line.strip() for line in f.readlines() if line.strip()]
        except Exception: pass
    return logs[-1000:]  # Останні 200 записів

DEFAULT_PERMISSIONS = {
    "god":   ["all"],
    "admin": ["akb","akb_add","logs","workshop"],
    "user":  ["logs","workshop"]
}

ALL_PERMISSIONS = ["bms","akb","akb_add","akb_reports","akb_export","logs","workshop","salary"]
PERM_NAMES = {
    "bms":"📟 BMS Керування",
    "akb":"📂 База АКБ",
    "akb_add":"🔋 Додати АКБ",
    "akb_reports":"📅 Звiти",
    "akb_export":"📑 Експорт Excel",
    "logs":"📉 Логи",
    "workshop":"🪪 Робочий кабiнет",
    "salary":"💰 Зарплата",
}

# ── МIГРАЦIЯ ПРАВ ───────────────────────────────────────────
# Старi ключi → новi. Виконується одноразово при старті.
def _migrate_permissions():
    changed = False
    valid_perms = set(ALL_PERMISSIONS)
    for u_id, info in list(users_db.items()):
        if not isinstance(info, dict): continue
        perms = info.get("permissions", [])
        if not isinstance(perms, list): continue
        if "all" in perms:
            continue
        # Розгортаємо випадки коли права записанi одним рядком через кому
        expanded = []
        for p in perms:
            if isinstance(p, str) and "," in p:
                for sub in p.split(","):
                    sub = sub.strip()
                    if sub: expanded.append(sub)
            else:
                expanded.append(p)
        # Перейменування
        new_perms = []
        for p in expanded:
            if p == "akb_excel":
                if "akb_reports" not in new_perms: new_perms.append("akb_reports")
                if "akb_export" not in new_perms: new_perms.append("akb_export")
            elif p in ("akb_view", "registry", "alerts", "VIEW", "monitoring"):
                # мертвi/застарiлi — пропускаємо
                continue
            elif p in valid_perms:
                if p not in new_perms: new_perms.append(p)
            # iншi незрозумiлi ключi теж пропускаємо
        if new_perms != perms:
            info["permissions"] = new_perms
            changed = True
    if changed:
        save_json(USERS_FILE, users_db)

_migrate_permissions()

WORKSHOP_STAGES = [
    "1️⃣ Прийом елементiв",
    "2️⃣ Замiр опору",
    "3️⃣ Звiт опорiв четвiрки",
    "4️⃣ Дата та запакування",
    "5️⃣ Готовий АКБ",
]

# Опцiональний етап — Контроль якостi (вiдео), додається пiсля завершення АКБ
QC_STAGE_NAME = "📹 Контроль якостi"
QC_STAGE_NAMES_ALL = ("📹 Контроль якостi", "6️⃣ Контроль якостi", "6️⃣ Готовий АКБ", "7️⃣ Готовий АКБ")

# Етапи що потребують 2 фото
WORKSHOP_TWO_PHOTOS = [
    "1️⃣ Прийом елементiв",
]

# Чеклист контролю якості
QUALITY_CHECKLIST = [
    "1. Пошкодження корпусу",
    "2. Вiдсутнiсть наклейок",
    "3. Погано усаджена термоусадка (PG та SP)",
    "4. Порвана резинка кнопки увiмкнення",
    "5. Неробочий iндикатор заряду",
    "6. Щiльнiсть та герметичнiсть корпусу",
    "7. Кручення синьої гайки на SP та наявнiсть сальника",
    "8. Iдентифiкацiя четвiрки АКБ наклейками",
    "9. Вiдсутнє клеймо",
    "10. Наявнiсть резинки на ХТ мама",
    "11. Рух пiнiв у роз'ємi ХТ, пошкодження чи нагар",
    "12. Наявнiсть ковпачка на ХТ тато",
    "13. Переплюсовка у вимкненому станi",
    "14. Вiдсутнiсть струму на дротах в увiмкненому станi",
    "15. Прокрут вивiдної кнопки SP",
    "16. Увiмкнення з вивiдної кнопки, контроль iндикатора",
    "17. Увiмкнення/вимкнення зi штатної кнопки",
    "18. Налаштування BMS",
    "19. Невiдповiднiсть АКБ вище максимального опору",
    "20. Баланс по опорах четвiрки",
    "21. Невiдповiднiсть акту здачi до номера BMS",
    "22. Неодночасний запуск/вимкнення четвiрки через павука",
    "23. Не заряджений АКБ (менше 29В)",
    "24. Провал комiрки вiдносно iнших елементiв",
]

# ── ПРЕСЕТ ────────────────────────────────────────────────────
DEFAULT_PRESET = {
    "ovp":        {"val":4.200,  "unit":"V",  "desc":"Cell OVP"},
    "ovpr":       {"val":4.150,  "unit":"V",  "desc":"Cell OVPR"},
    "uvp":        {"val":3.060,  "unit":"V",  "desc":"Cell UVP"},
    "uvpr":       {"val":3.300,  "unit":"V",  "desc":"Cell UVPR"},
    "bal_start":  {"val":4.100,  "unit":"V",  "desc":"Balance Start Volt"},
    "bal_delta":  {"val":0.010,  "unit":"V",  "desc":"Balance Trig. Volt"},
    "bal_max_cur":{"val":1.0,    "unit":"A",  "desc":"Max Balance Current"},
    "chg_oc":     {"val":70.0,   "unit":"A",  "desc":"Continued Charge Curr"},
    "chg_ocp_d":  {"val":60,     "unit":"s",  "desc":"Charge OCP Delay"},
    "chg_ocpr_t": {"val":60,     "unit":"s",  "desc":"Charge OCPR Time"},
    "dchg_oc":    {"val":100.0,  "unit":"A",  "desc":"Continued Discharge Curr"},
    "dchg_ocp_d": {"val":120,    "unit":"s",  "desc":"Discharge OCP Delay"},
    "dchg_ocpr_t":{"val":60,     "unit":"s",  "desc":"Discharge OCPR Time"},
    "chg_ot":     {"val":80.0,   "unit":"C",  "desc":"Charge OTP"},
    "chg_ot_r":   {"val":75.0,   "unit":"C",  "desc":"Charge OTPR"},
    "chg_ut":     {"val":-20.0,  "unit":"C",  "desc":"Charge UTP"},
    "chg_ut_r":   {"val":-10.0,  "unit":"C",  "desc":"Charge UTPR"},
    "dchg_ot":    {"val":80.0,   "unit":"C",  "desc":"Discharge OTP"},
    "dchg_ot_r":  {"val":75.0,   "unit":"C",  "desc":"Discharge OTPR"},
    "mos_ot":     {"val":80.0,   "unit":"C",  "desc":"MOS OTP"},
    "mos_ot_r":   {"val":70.0,   "unit":"C",  "desc":"MOS OTPR"},
    "capacity":   {"val":60.0,   "unit":"Ah", "desc":"Battery Capacity"},
    "soc100":     {"val":4.180,  "unit":"V",  "desc":"SOC-100% Volt"},
    "soc0":       {"val":3.070,  "unit":"V",  "desc":"SOC-0% Volt"},
    "pwr_off":    {"val":3.050,  "unit":"V",  "desc":"Power Off Volt"},
    "rcv_volt":   {"val":4.190,  "unit":"V",  "desc":"Vol. Cell RCV"},
    "rfv_volt":   {"val":4.100,  "unit":"V",  "desc":"Vol. Cell RFV"},
    "sleep_volt": {"val":4.150,  "unit":"V",  "desc":"Vol. Smart Sleep"},
    "sleep_time": {"val":24,     "unit":"h",  "desc":"Time Smart Sleep"},
}

saved_preset = load_json(PRESET_FILE, None)
# Заводські параметри для BMS 150A (з файлу 150bms.jkcfg)
DEFAULT_PRESET_150 = {
    "ovp":        {"val":4.200,  "unit":"V",  "desc":"Cell OVP"},
    "ovpr":       {"val":4.170,  "unit":"V",  "desc":"Cell OVPR"},
    "uvp":        {"val":3.060,  "unit":"V",  "desc":"Cell UVP"},
    "uvpr":       {"val":3.300,  "unit":"V",  "desc":"Cell UVPR"},
    "bal_start":  {"val":3.000,  "unit":"V",  "desc":"Balance Start Volt"},
    "bal_delta":  {"val":0.003,  "unit":"V",  "desc":"Balance Trig. Volt"},
    "soc100":     {"val":4.180,  "unit":"V",  "desc":"SOC-100% Volt"},
    "soc0":       {"val":3.070,  "unit":"V",  "desc":"SOC-0% Volt"},
    "rcv_volt":   {"val":4.190,  "unit":"V",  "desc":"Vol. Cell RCV"},
    "rfv_volt":   {"val":4.100,  "unit":"V",  "desc":"Vol. Cell RFV"},
    "pwr_off":    {"val":3.050,  "unit":"V",  "desc":"Power Off Volt"},
    "sleep_volt": {"val":4.150,  "unit":"V",  "desc":"Vol. Smart Sleep"},
    "sleep_time": {"val":24,     "unit":"h",  "desc":"Time Smart Sleep"},
    "chg_oc":     {"val":70.0,   "unit":"A",  "desc":"Continued Charge Curr"},
    "dchg_oc":    {"val":150.0,  "unit":"A",  "desc":"Continued Discharge Curr"},
    "capacity":   {"val":60.0,   "unit":"Ah", "desc":"Battery Capacity"},
    "chg_ot":     {"val":80.0,   "unit":"C",  "desc":"Charge OTP"},
    "chg_ot_r":   {"val":75.0,   "unit":"C",  "desc":"Charge OTPR"},
    "chg_ut":     {"val":-20.0,  "unit":"C",  "desc":"Charge UTP"},
    "chg_ut_r":   {"val":-10.0,  "unit":"C",  "desc":"Charge UTPR"},
    "dchg_ot":    {"val":80.0,   "unit":"C",  "desc":"Discharge OTP"},
    "dchg_ot_r":  {"val":75.0,   "unit":"C",  "desc":"Discharge OTPR"},
    "mos_ot":     {"val":80.0,   "unit":"C",  "desc":"MOS OTP"},
    "mos_ot_r":   {"val":70.0,   "unit":"C",  "desc":"MOS OTPR"},
}

MY_PRESET = saved_preset if saved_preset else {k: dict(v) for k, v in DEFAULT_PRESET.items()}

PARAMS_ADDR = {
    "ovpr":       (0x00,"uint16mv"),
    "uvp":        (0x01,"uint16mv"),
    "uvpr":       (0x02,"uint16mv"),
    "ovp":        (0x03,"uint16mv"),
    "bal_start":  (0x04,"uint16mv"),
    "bal_delta":  (0x05,"uint16mv"),
    "soc100":     (0x06,"uint16mv"),
    "soc0":       (0x07,"uint16mv"),
    "rcv_volt":   (0x08,"uint16mv"),
    "rfv_volt":   (0x09,"uint16mv"),
    "pwr_off":    (0x0A,"uint16mv"),
    "chg_oc":     (0x0B,"uint16ma"),
    "dchg_oc":    (0x0C,"uint16ma"),
    "chg_ot":     (0x0D,"uint16t"),
    "chg_ot_r":   (0x0E,"uint16t"),
    "chg_ut":     (0x0F,"uint16t"),
    "chg_ut_r":   (0x10,"uint16t"),
    "dchg_ot":    (0x11,"uint16t"),
    "dchg_ot_r":  (0x12,"uint16t"),
    "mos_ot":     (0x13,"uint16t"),
    "mos_ot_r":   (0x14,"uint16t"),
    "sleep_volt": (0x1F,"uint16mv"),
    "sleep_time": (0x20,"uint16"),
}

def format_param_val(p, val):
    """Форматує значення параметру залежно від типу"""
    volt_params = ["ovp","ovpr","uvp","uvpr","soc100","soc0","pwr_off","rcv_volt","rfv_volt","bal_start","bal_delta"]
    temp_params = ["chg_ot","chg_ot_r","chg_ut","chg_ut_r","dchg_ot","dchg_ot_r","mos_ot","mos_ot_r"]
    time_params = ["chg_ocp_d","chg_ocpr_t","dchg_ocp_d","dchg_ocpr_t","sleep_time"]
    if p in volt_params:
        return f"{float(val):.3f}"
    elif p in temp_params:
        return f"{float(val):.1f}"
    elif p in time_params:
        return f"{float(val):.0f}"
    else:
        return f"{float(val):.1f}"

PARAM_GROUPS = {
    "voltage": {"name":"⚡️ Напруга",      "params":["ovp","ovpr","uvp","uvpr","soc100","soc0","pwr_off","rcv_volt","rfv_volt"]},
    "balance": {"name":"⚖️ Балансування", "params":["bal_start","bal_delta","bal_max_cur"]},
    "current": {"name":"🔌 Струм",         "params":["chg_oc","chg_ocp_d","chg_ocpr_t","dchg_oc","dchg_ocp_d","dchg_ocpr_t"]},
    "temp":    {"name":"🌡 Температура",   "params":["chg_ot","chg_ot_r","chg_ut","chg_ut_r","dchg_ot","dchg_ot_r","mos_ot","mos_ot_r"]},
    "basic":   {"name":"📋 Основнi",       "params":["capacity","sleep_volt","sleep_time"]},
}

# ── СТАН ──────────────────────────────────────────────────────
class BotState:
    bms_address = None
    bms_name = None
    client = None
    live_data = {}
    device_params = {}
    device_info_buf = bytearray()  # Буфер для пакету 55 AA EB 90 01
    alert_task = None
    keepalive_task = None
    alerts_enabled = False
    alert_interval = 60
    log = load_audit_log()
    reconnecting = False
    reading = False
    writing = False
    scan_results = []

state = BotState()

class UserSession:
    waiting_for = {}
    temp_data = {}

session = UserSession()

# ── РОЛІ ──────────────────────────────────────────────────────
def get_user(uid):
    if uid == GOD_ID: return {"role":"god","permissions":["all"],"name":"Kaatastroffaa"}
    return users_db.get(str(uid))

def get_role(uid):
    u = get_user(uid)
    return u.get("role","user") if u else None

def get_name(uid):
    u = get_user(uid)
    if not u: return str(uid)
    return u.get("name", str(uid))

def has_perm(uid, perm):
    u = get_user(uid)
    if not u: return False
    p = u.get("permissions",[])
    return "all" in p or perm in p

def is_god(uid): return uid == GOD_ID
def role_icon(r): return {"god":"👑","admin":"🛡","user":"👤"}.get(r,"❓")

# ── AUDIT LOG ─────────────────────────────────────────────────
def get_audit_icon(action):
    a = action.lower()
    if any(w in a for w in ["збережено","збережен","додано","створено","пiдключено","підключено"]):
        return "🟢"
    elif any(w in a for w in ["видалено","видален","скасовано"]):
        return "🔴"
    elif any(w in a for w in ["замiнено","заміне","оновлено","змiнено","змінено"]):
        return "🟡"
    return "⚪"

def audit(uid, action, details=""):
    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    name = get_name(uid)
    role = get_role(uid) or "?"
    icon = get_audit_icon(action)
    entry = f"{icon} {ts} | {role_icon(role)} {name} ({uid}) | {action}"
    if details: entry += f" | {details}"
    state.log.append(entry)
    if len(state.log) > 200: state.log.pop(0)
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception: pass

def add_log(e): audit(GOD_ID, e)

# ── JK BMS ────────────────────────────────────────────────────
def build_write(addr, val, dtype="float"):
    hdr = bytes([0xAA,0x55,0x90,0xEB])
    if dtype == "uint16mv":
        # Напруга: значення × 1000, uint32 Little-Endian (перевірено з .jkcfg файлу)
        vb = struct.pack("<I", int(round(float(val) * 1000)))
    elif dtype == "uint16ma":
        # Струм/Ємність: значення × 1000, uint32 Little-Endian
        vb = struct.pack("<I", int(round(float(val) * 1000)))
    elif dtype == "uint16t":
        # Температура: значення × 10, uint32 Little-Endian
        vb = struct.pack("<I", int(round(float(val) * 10)))
    elif dtype == "uint16":
        vb = struct.pack("<I", int(val))
    elif dtype == "uint8":
        vb = bytes([int(val)]) + bytes(3)
    elif dtype == "float":
        vb = struct.pack(">f", float(val))
    else:
        vb = struct.pack("<I", int(round(float(val) * 1000)))
    # Байт 5 = 0x04 для запису конфігурації (з документації ESP32 JK-BMS)
    length_byte = 0x00 if dtype == "uint8" and val == 0 else 0x04
    pl = bytes([addr, length_byte]) + vb + bytes(9)
    return hdr + pl + bytes([sum(hdr+pl) & 0xFF])

def jk_u16(d, i): return (d[i+1]<<8)|d[i] if i+1<len(d) else 0
def jk_i32(d, i):
    if i+3>=len(d): return 0
    v = (d[i+3]<<24)|(d[i+2]<<16)|(d[i+1]<<8)|d[i]
    return v - 2**32 if v > 2**31 else v

def parse_cell_info(data):
    try:
        result = {}
        cells = []; zero_count = 0
        for i in range(24):
            off = 6 + i*2
            if off+1 >= len(data): break
            v = jk_u16(data, off); mv = v/1000.0
            if mv < 0.5:
                zero_count += 1
                if zero_count >= 2: break
                continue
            zero_count = 0
            if 2.0 <= mv <= 5.0: cells.append(round(mv,3))
        if cells:
            result["cells"] = cells
            result["cell_max"] = max(cells); result["cell_min"] = min(cells)
            result["cell_delta"] = round(max(cells)-min(cells),3)
            result["voltage"] = round(sum(cells),2)
        if 151 < len(data):
            v = jk_u16(data,150)
            if 1000 < v < 100000: result["voltage"] = round(v/1000.0,2)
        if 161 < len(data):
            c = jk_i32(data,158)
            if abs(c) < 3000000: result["current"] = round(c/1000.0,2)
        if "voltage" in result and "current" in result:
            result["power"] = round(result["voltage"]*result["current"],1)
        if 173 < len(data):
            soc = data[173]
            if 0 <= soc <= 100: result["soc"] = soc
        if 144 < len(data):
            raw = data[144]
            if raw > 0:
                t = raw/10.0
                if 0 <= t <= 120: result["temp_mos"] = round(t,1)
        for tname,toff in [("temp1",164),("temp2",162)]:
            if toff < len(data):
                raw = data[toff]
                if raw > 0:
                    t = raw/10.0
                    if 0 <= t <= 120: result[tname] = round(t,1)
        if 185 < len(data):
            cyc = jk_u16(data,182)
            if cyc < 100000: result["cycle_count"] = cyc
        # Статус з офіційного коду esphome-jk-bms
        # offset 166 = Charging MOS, offset 167 = Discharging MOS
        if 167 < len(data):
            result["charging"] = bool(data[166])
            result["discharging"] = bool(data[167])
        # Балансування - offset 76 біт 2
        if 76 < len(data):
            result["balancing"] = bool(data[76] & 0x04)
        if 167 < len(data):
            rc = jk_u16(data,164)
            if 0 < rc < 10000: result["remain_cap"] = round(rc/10.0,1)
        result["proto"] = "JK02_24S"
        return result
    except Exception as e:
        logger.error(f"Parse: {e}"); return None

# ── BLE ──────────────────────────────────────────────────────
async def scan_bms():
    found = []
    try:
        def cb(dev, adv):
            uuids = [str(u).lower() for u in (adv.service_uuids or [])]
            if "ffe0" in " ".join(uuids) and dev not in found:
                found.append(dev)
        sc = BleakScanner(detection_callback=cb)
        await sc.start(); await asyncio.sleep(8.0); await sc.stop()
        if found: return found
    except Exception: pass
    devs = await BleakScanner.discover(timeout=8.0)
    jk = [d for d in devs if any(x in (d.name or "").upper() for x in ["JK","BMS"])]
    if jk: return jk
    return [d for d in devs if d.name and d.name.isdigit()]

async def connect_bms(address):
    # Спочатку відключаємо старе з'єднання
    if state.client:
        try:
            if state.client.is_connected:
                await state.client.disconnect()
        except Exception: pass
        state.client = None
        await asyncio.sleep(1.0)  # Чекаємо щоб BLE звільнився

    for attempt in range(3):
        try:
            state.client = BleakClient(address, timeout=15.0)
            await state.client.connect()
            if state.client.is_connected:
                # Глобальний обробник - зберігає пакет 01 коли приходить
                state.device_info_buf = bytearray()
                state.device_params = {}
                def global_notify(_, data):
                    state.device_info_buf.extend(data)
                    # Парсимо пакет 01 одразу
                    buf = bytes(state.device_info_buf)
                    idx = buf.find(bytes([0x55,0xAA,0xEB,0x90,0x01]))
                    if idx != -1 and not state.device_params:
                        import struct
                        pkt = buf[idx+5:]
                        def u16(off):
                            if off+2 <= len(pkt): return struct.unpack('<H', pkt[off:off+2])[0]
                            return 0
                        params = {
                            "ovpr": u16(0x001)/1000, "uvp": u16(0x005)/1000,
                            "uvpr": u16(0x009)/1000, "ovp": u16(0x00D)/1000,
                            "bal_start": u16(0x011)/1000, "bal_delta": u16(0x015)/1000,
                            "soc100": u16(0x019)/1000, "soc0": u16(0x01D)/1000,
                            "rcv_volt": u16(0x021)/1000, "rfv_volt": u16(0x025)/1000,
                            "pwr_off": u16(0x029)/1000,
                        }
                        if any(v > 0 for v in params.values()):
                            state.device_params = params
                try:
                    await state.client.start_notify(JK_CHAR_UUID, global_notify)
                except: pass
                if state.keepalive_task is None or state.keepalive_task.done():
                    state.keepalive_task = asyncio.create_task(keepalive_loop())
                asyncio.create_task(fetch_device_params())
                return True
        except Exception as e:
            logger.info(f"Спроба {attempt+1}/3: {e}")
            state.client = None
            if attempt < 2: await asyncio.sleep(2)
    return False

async def fetch_device_params():
    """Зчитує параметри налаштувань після підключення і зберігає в state"""
    await asyncio.sleep(1.0)  # Чекаємо поки BMS стабілізується
    try:
        params = await read_device_info_params()
        if params:
            state.device_params = params
            import logging
            logging.getLogger(__name__).info(f"Device params зчитано: {len(params)} параметрів")
    except Exception as e:
        pass

async def keepalive_loop():
    reconnect_attempts = 0
    while True:
        await asyncio.sleep(8)  # Збільшили інтервал для стабільності
        if state.reading: continue
        if state.writing: continue
        if state.client and state.client.is_connected:
            try:
                await state.client.write_gatt_char(JK_CHAR_UUID, CMD_CELL_INFO, response=False)
                reconnect_attempts = 0
            except Exception: pass
        elif state.bms_address and not state.reconnecting:
            if reconnect_attempts >= 3:
                logger.info("BMS недоступна, пауза 60 сек...")
                await asyncio.sleep(60); reconnect_attempts = 0; continue
            state.reconnecting = True; reconnect_attempts += 1
            try:
                if await connect_bms(state.bms_address): reconnect_attempts = 0
                else: await asyncio.sleep(15)
            except Exception: await asyncio.sleep(15)
            finally: state.reconnecting = False

async def read_device_info_params():
    """Зчитує параметри налаштувань з Device Info пакету BMS (uint16 LE / 1000)"""
    # Якщо вже є збережені параметри - повертаємо їх
    if state.device_params:
        return state.device_params
    if not state.client or not state.client.is_connected:
        return {}
    try:
        buf = bytearray()
        def handler(_, data): buf.extend(data)
        await state.client.start_notify(JK_CHAR_UUID, handler)
        # Надсилаємо CMD_DEVICE_INFO і чекаємо пакет 01
        cmd = bytes.fromhex("AA5590EB97000000000000000000000000000011")
        await state.client.write_gatt_char(JK_CHAR_UUID, cmd, response=True)
        await asyncio.sleep(1.0)
        # Також надсилаємо CMD_CELL_INFO - іноді після нього приходить пакет 01
        cmd2 = bytes.fromhex("AA5590EB960000000000000000000000000010")
        await state.client.write_gatt_char(JK_CHAR_UUID, cmd2, response=True)
        await asyncio.sleep(2.0)
        try: await state.client.stop_notify(JK_CHAR_UUID)
        except: pass
        data = bytes(buf)
        idx = data.find(bytes([0x55,0xAA,0xEB,0x90,0x01]))
        if idx == -1: return {}
        pkt = data[idx+5:]  # пропускаємо заголовок
        import struct
        def u16(off):
            if off+2 <= len(pkt): return struct.unpack('<H', pkt[off:off+2])[0]
            return 0
        return {
            "ovpr":     u16(0x001) / 1000,
            "uvp":      u16(0x005) / 1000,
            "uvpr":     u16(0x009) / 1000,
            "ovp":      u16(0x00D) / 1000,
            "bal_start":u16(0x011) / 1000,
            "bal_delta":u16(0x015) / 1000,
            "soc100":   u16(0x019) / 1000,
            "soc0":     u16(0x01D) / 1000,
            "rcv_volt": u16(0x021) / 1000,
            "rfv_volt": u16(0x025) / 1000,
            "pwr_off":  u16(0x029) / 1000,
            "chg_oc":   u16(0x02D) / 1000,
            "dchg_oc":  u16(0x031) / 1000,
            "chg_ot":   u16(0x035) / 10,
            "chg_ot_r": u16(0x037) / 10,
            "chg_ut":   u16(0x039) / 10,
            "chg_ut_r": u16(0x03B) / 10,
            "dchg_ot":  u16(0x03D) / 10,
            "dchg_ot_r":u16(0x03F) / 10,
            "mos_ot":   u16(0x041) / 10,
            "mos_ot_r": u16(0x043) / 10,
        }
    except Exception as e:
        logger.error(f"read_device_info_params: {e}")
        return {}

async def read_bms_data():
    if not state.client or not state.client.is_connected: return None
    state.reading = True
    kl = state.keepalive_task
    if kl and not kl.done():
        kl.cancel(); state.keepalive_task = None
        await asyncio.sleep(0.5)
    try:
        buf = bytearray()
        def handler(_, chunk): buf.extend(chunk)
        await state.client.start_notify(JK_CHAR_UUID, handler)
        # Перша ітерація — очищення
        buf.clear()
        await state.client.write_gatt_char(JK_CHAR_UUID, CMD_DEVICE_INFO, response=True)
        await asyncio.sleep(1.0)
        buf.clear()
        await state.client.write_gatt_char(JK_CHAR_UUID, CMD_CELL_INFO, response=True)
        await asyncio.sleep(1.5)
        buf.clear()
        # Друга ітерація — чисті дані
        await state.client.write_gatt_char(JK_CHAR_UUID, CMD_DEVICE_INFO, response=True)
        await asyncio.sleep(1.0)
        buf.clear()
        await state.client.write_gatt_char(JK_CHAR_UUID, CMD_CELL_INFO, response=True)
        await asyncio.sleep(2.0)
        try: await state.client.stop_notify(JK_CHAR_UUID)
        except Exception: pass
        data = bytes(buf)
        best = None; best_size = 0; ix = 0
        while ix < len(data) - 4:
            if data[ix:ix+4] == bytes([0x55,0xAA,0xEB,0x90]):
                nx = data.find(bytes([0x55,0xAA,0xEB,0x90]), ix+4)
                fr = data[ix:nx] if nx > 0 else data[ix:]
                ft = fr[4] if len(fr) > 4 else 0xFF
                if ft == 0x02 and len(fr) > best_size:
                    best = fr; best_size = len(fr)
                ix = nx if nx > 0 else len(data)
            else: ix += 1
        if not best:
            ix = 0
            while ix < len(data) - 4:
                if data[ix:ix+4] == bytes([0x55,0xAA,0xEB,0x90]):
                    nx = data.find(bytes([0x55,0xAA,0xEB,0x90]), ix+4)
                    fr = data[ix:nx] if nx > 0 else data[ix:]
                    if len(fr) > best_size: best = fr; best_size = len(fr)
                    ix = nx if nx > 0 else len(data)
                else: ix += 1
        if best and len(best) > 50:
            result = parse_cell_info(bytes(best))
            if result: state.live_data = result; return result
        return None
    except Exception as e:
        logger.error(f"Read: {e}"); return None
    finally:
        state.reading = False
        if state.client and state.client.is_connected and state.bms_address:
            if state.keepalive_task is None or state.keepalive_task.done():
                state.keepalive_task = asyncio.create_task(keepalive_loop())

async def write_param(param, value):
    if param not in PARAMS_ADDR or not state.client or not state.client.is_connected: return False
    addr, dtype = PARAMS_ADDR[param]
    try:
        # Зупиняємо keepalive
        state.writing = True
        await asyncio.sleep(0.5)
        cmd = build_write(addr, value, dtype)
        # Надсилаємо 3 рази з паузами
        for _ in range(3):
            await state.client.write_gatt_char(JK_CHAR_UUID, cmd, response=True)
            await asyncio.sleep(1.0)
        # Чекаємо 3 секунди щоб BMS встигла зберегти
        await asyncio.sleep(3.0)
        state.writing = False
        return True
    except Exception as e:
        state.writing = False
        logger.error(f"Write {param}: {e}"); return False

# ── ФОРМАТУВАННЯ ─────────────────────────────────────────────
def make_bar(v, mx, ln=10):
    if mx == 0: return "-"*ln
    p = min(v/mx,1.0); f = int(p*ln)
    return f"[{'='*f}{'-'*(ln-f)}] {int(p*100)}%"

def format_status(d):
    if not d: return "Немає даних вiд BMS"
    lines = ["🔋 BMS Статус\n"]
    if "voltage" in d: lines.append(f"⚡️ Напруга: {d['voltage']} V")
    if "current" in d:
        c = d["current"]
        if c > 0.1: lines.append(f"🔌 Заряд: {c} A")
        elif c < -0.1: lines.append(f"⚡️ Розряд: {abs(c)} A")
        else: lines.append(f"⏸ Струм: 0.0 A")
    if "power" in d and abs(d["power"]) > 1: lines.append(f"💡 Потужнiсть: {d['power']} W")
    if "soc" in d:
        soc = d["soc"]
        icon = "🟢" if soc >= 70 else ("🟡" if soc >= 30 else "🔴")
        lines.append(f"{icon} SOC: {make_bar(soc,100)}")
    if "remain_cap" in d: lines.append(f"🔋 Залишок: {d['remain_cap']} Ah")
    if "temp_mos" in d: lines.append(f"🔧 MOS: {d['temp_mos']}C")
    if "temp1" in d: lines.append(f"🌡 T1: {d['temp1']}C")
    if "temp2" in d: lines.append(f"🌡 T2: {d['temp2']}C")
    if "cell_delta" in d:
        dt = d["cell_delta"]
        icon = "✅" if dt < 0.020 else ("⚠️" if dt < 0.100 else "🔴")
        lines.append(f"{icon} Дельта: {dt:.3f} V")
    if "balancing" in d:
        lines.append(f"⚖️ Балансування: {'🔄 ON' if d['balancing'] else '— OFF'}")
    chg = "✅" if d.get("charging") else "❌"
    dchg = "✅" if d.get("discharging") else "❌"
    lines.append(f"🔌 Заряд: {chg}  Розряд: {dchg}")
    if "cycle_count" in d: lines.append(f"🔄 Цикли: {d['cycle_count']}")
    bms_info = f"\n✅ Пiдключено | {d.get('proto','JK02')}"
    if state.bms_name: bms_info += f"\n🔋 {state.bms_name}"
    lines.append(bms_info)
    return "\n".join(lines)

def format_cells(d):
    if not d or "cells" not in d: return "Немає даних про ячейки"
    cells = d["cells"]
    lines = [f"🔬 Напруга ячейок ({len(cells)} шт.):\n"]
    for i, v in enumerate(cells):
        icon = "🔺" if v == d["cell_max"] else ("🔻" if v == d["cell_min"] else "▪️")
        bar = make_bar(v-3.0, 1.3, 8)
        lines.append(f"{icon} C{i+1:02d}: {v:.3f} V  {bar}")
    lines.append(f"\n⬆️ Макс: {d['cell_max']:.3f} V")
    lines.append(f"⬇️ Мiн: {d['cell_min']:.3f} V")
    lines.append(f"📊 Дельта: {round(d['cell_delta']*1000)} мВ")
    if "voltage" in d: lines.append(f"⚡️ Загальна: {d['voltage']:.2f} V")
    return "\n".join(lines)

def format_preset_group(gk):
    g = PARAM_GROUPS[gk]; text = f"{g['name']}\n\n"
    for p in g["params"]:
        if p in MY_PRESET:
            pr = MY_PRESET[p]
            text += f"• {pr['desc']}: {format_param_val(p, pr['val'])} {pr['unit']}\n"
    return text

def preset_inline_kb(gk):
    g = PARAM_GROUPS[gk]; btns = []
    for p in g["params"]:
        if p in MY_PRESET:
            pr = MY_PRESET[p]
            btns.append([InlineKeyboardButton(f"✏️ {pr['desc']}: {format_param_val(p, pr['val'])} {pr['unit']}", callback_data=f"ep_{p}_{gk}")])
    btns.append([InlineKeyboardButton("💾 Записати в BMS", callback_data=f"wr_{gk}"),
                 InlineKeyboardButton("🔙 Назад", callback_data="bp")])
    return InlineKeyboardMarkup(btns)

def user_perms_inline_kb(target_uid):
    user = users_db.get(str(target_uid), {})
    perms = user.get("permissions", []) if isinstance(user, dict) else []
    has_all = "all" in perms
    if has_all:
        active_set = set(ALL_PERMISSIONS)
    else:
        active_set = set(perms)
    cur_role = user.get("role","user") if isinstance(user, dict) else "user"
    return user_perms_inline_kb_buf(target_uid, active_set, cur_role)

def user_perms_inline_kb_buf(target_uid, buf, role=None):
    """Малює клавiатуру прав на основi буфера (поточних змiн в пам'ятi)."""
    active_set = set(buf)
    btns = []; row = []
    for i, perm in enumerate(ALL_PERMISSIONS):
        active = perm in active_set
        icon = "✅" if active else "❌"
        pname = PERM_NAMES.get(perm, perm)
        row.append(InlineKeyboardButton(f"{icon} {pname}", callback_data=f"tp_{target_uid}_{perm}"))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("👑 Всi права", callback_data=f"tp_{target_uid}_ALL"),
                 InlineKeyboardButton("💾 Зберегти", callback_data=f"tp_{target_uid}_SAVE")])
    # Кнопка змiни ролi — показує поточну роль з буфера
    role_label = role if role else "user"
    role_icon_txt = role_icon(role_label) if role_label else "👤"
    btns.append([InlineKeyboardButton(f"🔄 Роль: {role_icon_txt} {role_label}", callback_data=f"tp_{target_uid}_ROLE")])
    return InlineKeyboardMarkup(btns)

# ── КЛАВІАТУРИ ───────────────────────────────────────────────

def cmd_center_kb():
    return ReplyKeyboardMarkup([
        ["⌨️ Ввести команду","📸 Скріншот"],
        ["🖥 Екран","⚡️ Живлення"],
        ["🤖 Бот","🔐 Пароль"],
        ["🔙 Назад до налаштувань"],
    ], resize_keyboard=True)

def cmd_screen_kb():
    return ReplyKeyboardMarkup([
        ["🔒 Заблокувати комп"],
        ["🔙 CMD центр"],
    ], resize_keyboard=True)

def cmd_power_kb():
    return ReplyKeyboardMarkup([
        ["🔄 Перезапустити ноут","⭕ Вимкнути ноут"],
        ["⏰ Вимкнути через 30хв","⏰ Вимкнути через 1год"],
        ["⏰ Вимкнути через 2год","🚫 Скасувати вимкнення"],
        ["🔙 CMD центр"],
    ], resize_keyboard=True)

def cmd_sound_kb():
    return ReplyKeyboardMarkup([
        ["🔇 Вимкнути звук","🔊 Увімкнути звук"],
        ["🔈 10%","🔉 25%","🔊 50%"],
        ["🔊 75%","🔊 100%"],
        ["🔙 CMD центр"],
    ], resize_keyboard=True)

def cmd_bot_kb():
    return ReplyKeyboardMarkup([
        ["🔄 Перезапустити бота","⏹ Зупинити бота"],
        ["🔙 CMD центр"],
    ], resize_keyboard=True)

def cmd_network_kb():
    return ReplyKeyboardMarkup([
        ["📶 Перевірити інтернет","📡 WiFi мережi поруч"],
        ["🔙 CMD центр"],
    ], resize_keyboard=True)

def cmd_autostart_kb():
    return ReplyKeyboardMarkup([
        ["✅ Увімкнути автозапуск","❌ Вимкнути автозапуск"],
        ["📋 Статус автозапуску"],
        ["🔙 CMD центр"],
    ], resize_keyboard=True)

def main_kb(uid):
    # Кнопки з'являються тiльки якщо є вiдповiдне право (або god).
    # 👥 Працiвники / ⚙️ Налаштування / ❔ Допомога — жорсткi (не в правах).
    god = is_god(uid)
    role = get_role(uid)
    can = lambda p: god or has_perm(uid, p)
    # Збираємо доступнi пункти в плоский список, потiм розкладемо по парах
    items = []
    if can("bms"): items.append("📟 BMS Керування")
    if can("akb"): items.append("📂 База АКБ")
    if can("logs"): items.append("📉 Логи")
    if can("workshop"): items.append("🪪 Робочий кабiнет")
    items.append("❔ Допомога")  # видно завжди
    # Розкладаємо по два в рядок
    b = []
    for i in range(0, len(items), 2):
        b.append(items[i:i+2])
    if role == "admin" or god:
        b.append(["👥 Працівники"])
    if god:
        b.append(["⚙️ Налаштування"])
    return ReplyKeyboardMarkup(b, resize_keyboard=True)

def bms_kb():
    return ReplyKeyboardMarkup([
        ["🔍 Знайти BMS","📡 Статус"],
        ["📈 Стан батареї","🔬 Ячейки"],
        ["🌡 Температура BMS","📋 Пресети"],
        ["🔉 Алерти"],
        ["🔴 Вимкнути батарею","🔌 Вiдключитись"],
        ["🔙 Назад"],
    ], resize_keyboard=True)

def presets_kb(uid):
    if has_perm(uid,"bms") and not is_god(uid) and get_role(uid) == "user":
        return ReplyKeyboardMarkup([
            ["🔋 BMS 100A","🔋 BMS 150A"],
            ["🔙 Назад"],
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        ["🔋 BMS 100A","🔋 BMS 150A"],
        ["🔙 Назад"],
    ], resize_keyboard=True)

def preset_model_kb(model):
    """Підменю для конкретної моделі BMS"""
    kb = [
        ["⚡️ Напруга","⚖️ Балансування"],
        ["🔌 Струм","🌡 Температура"],
        ["🏭 Записати заводськi в BMS"],
    ]
    if model == "150":
        kb.append(["📥 Завод .jkcfg"])
    kb.append(["🔙 Назад до пресетiв"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def akb_kb(uid):
    god = is_god(uid)
    can = lambda p: god or has_perm(uid, p)
    b = []
    # Перший ряд — додавання і пошук
    row1 = []
    if can("akb_add"): row1.append("🔋 Додати АКБ")
    row1.append("🔎 Знайти АКБ")
    b.append(row1)
    b.append(["📁 Всi АКБ"])
    # Звiти / Експорт — за окремими правами
    reports_row = []
    if can("akb_reports"): reports_row.append("📅 Звiти")
    if can("akb_export"): reports_row.append("📑 Експорт Excel")
    if reports_row:
        b.append(reports_row)
    b.append(["🔙 Назад"])
    return ReplyKeyboardMarkup(b, resize_keyboard=True)

def akb_reports_kb():
    return ReplyKeyboardMarkup([
        ["📅 За сьогоднi","📅 За цей мiсяць"],
        ["📅 За минулий мiсяць","📅 За квартал"],
        ["📅 За рiк","📅 Свiй перiод"],
        ["🔙 Назад до АКБ"],
    ], resize_keyboard=True)

def akb_status_kb():
    return ReplyKeyboardMarkup([
        ["✅ Справний"],
        ["❌ Бракований"],
        ["🔍 На перевiрцi"],
        ["🔙 Назад до АКБ"],
    ], resize_keyboard=True)

def settings_kb():
    return ReplyKeyboardMarkup([
        ["👥 Всi юзери","➕ Додати юзера"],
        ["🔬 Дiагностика BMS","💻 CMD центр"],
        ["📋 Audit Log"],
        ["💾 Резервна копiя"],
        ["🔙 Назад"],
    ], resize_keyboard=True)



def get_active_batteries(uid):
    """Повертає список активних батарей юзера (не всі етапи пройдено)"""
    active = []
    for num, wdata in workshop_db.items():
        stages_done = wdata.get("stages", [])
        # Перевіряємо чи є записи цього юзера і чи не всі етапи пройдено
        user_stages = [s for s in stages_done if s.get("user_id") == uid or s.get("user_id") == str(uid) or str(s.get("user_id")) == str(uid)]
        if user_stages and len(user_stages) < len(WORKSHOP_STAGES):
            active.append(num)
        elif not user_stages:
            pass  # Не його батарея
    return active

def get_completed_batteries(target_uid, month=None):
    """Повертає список завершених батарей юзера. Якщо month='MM.YYYY' — тільки за місяць."""
    completed = []
    for num, wdata in workshop_db.items():
        stages = wdata.get("stages", [])
        user_stages = [s for s in stages if str(s.get("user_id","")) == str(target_uid)]
        if len(user_stages) >= len(WORKSHOP_STAGES):
            if month:
                last_date = user_stages[-1].get("date","")
                if len(last_date) >= 10 and last_date[3:10] == month:
                    completed.append(num)
            else:
                completed.append(num)
    return completed

def get_salary_data(target_uid):
    """Повертає дані зарплати юзера з salary_db або дефолт."""
    uid_str = str(target_uid)
    if uid_str not in salary_db:
        salary_db[uid_str] = {"rate": 2150, "records": []}
        save_json(SALARY_FILE, salary_db)
    return salary_db[uid_str]

def get_stats_text(target_uid, name, rate=2150):
    """Формує текст статистики для юзера."""
    from datetime import datetime, timedelta
    now = datetime.now()

    MONTHS_UA = {1:"Сiчень",2:"Лютий",3:"Березень",4:"Квiтень",5:"Травень",6:"Червень",
                 7:"Липень",8:"Серпень",9:"Вересень",10:"Жовтень",11:"Листопад",12:"Грудень"}

    # Цей тиждень
    week_start = now - timedelta(days=now.weekday())
    week_count = 0
    for num, wdata in workshop_db.items():
        stages = wdata.get("stages", [])
        user_stages = [s for s in stages if str(s.get("user_id","")) == str(target_uid)]
        if len(user_stages) >= len(WORKSHOP_STAGES):
            last_date = user_stages[-1].get("date","")
            try:
                d = datetime.strptime(last_date[:10], "%d.%m.%Y")
                if d >= week_start:
                    week_count += 1
            except: pass

    # Цей місяць
    this_month = now.strftime("%m.%Y")
    this_month_name = MONTHS_UA[now.month] + " " + now.strftime("%Y")
    this_month_count = len(get_completed_batteries(target_uid, this_month))

    # Минулий місяць
    first_day = now.replace(day=1)
    last_month_dt = first_day - timedelta(days=1)
    last_month = last_month_dt.strftime("%m.%Y")
    last_month_name = MONTHS_UA[last_month_dt.month] + " " + last_month_dt.strftime("%Y")
    last_month_count = len(get_completed_batteries(target_uid, last_month))

    # Порівняння
    diff = this_month_count - last_month_count
    if diff > 0:
        compare = f" ↑ +{diff} АКБ"
    elif diff < 0:
        compare = f" ↓ {diff} АКБ"
    else:
        compare = " = однаково"

    # Квартал
    quarter_months = []
    for i in range(3):
        if i == 0:
            quarter_months.append(now.strftime("%m.%Y"))
        else:
            d = now.replace(day=1)
            for _ in range(i):
                d = (d - timedelta(days=1)).replace(day=1)
            quarter_months.append(d.strftime("%m.%Y"))
    quarter_count = sum(len(get_completed_batteries(target_uid, m)) for m in quarter_months)

    # Рік
    year = now.strftime("%Y")
    year_count = 0
    for num, wdata in workshop_db.items():
        stages = wdata.get("stages", [])
        user_stages = [s for s in stages if str(s.get("user_id","")) == str(target_uid)]
        if len(user_stages) >= len(WORKSHOP_STAGES):
            last_date = user_stages[-1].get("date","")
            if len(last_date) >= 10 and last_date[6:10] == year:
                year_count += 1

    # Загальний баланс
    sal = get_salary_data(target_uid)
    total_accrued = len(get_completed_batteries(target_uid)) * rate
    total_paid = sum(r["amount"] for r in sal.get("records",[]) if r.get("type") in ["pay","advance"] and r.get("confirmed"))
    total_pending = sum(r["amount"] for r in sal.get("records",[]) if r.get("type") in ["pay","advance"] and not r.get("confirmed"))
    total_fines = sum(r["amount"] for r in sal.get("records",[]) if r.get("type") == "fine")
    total_bonuses = sum(r["amount"] for r in sal.get("records",[]) if r.get("type") == "bonus")
    to_pay = total_accrued - total_paid - total_pending - total_fines + total_bonuses
    pending_line = ""
    if total_pending > 0:
        pending_line = f"⏳ Очiкує пiдтвердження: {total_pending:,} грн\n"

    return (
        f"📊 Моя статистика — {name}\n\n"
        f"📅 Цей тиждень: {week_count} АКБ — {week_count * rate:,} грн\n"
        f"📅 {this_month_name}: {this_month_count} АКБ — {this_month_count * rate:,} грн\n"
        f"📅 {last_month_name}: {last_month_count} АКБ — {last_month_count * rate:,} грн{compare}\n"
        f"📅 Квартал: {quarter_count} АКБ — {quarter_count * rate:,} грн\n"
        f"📅 Рiк {year}: {year_count} АКБ — {year_count * rate:,} грн\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Нараховано: {total_accrued:,} грн\n"
        f"✅ Виплачено: {total_paid:,} грн\n"
        f"{pending_line}"
        f"⚠️ Штрафи: {total_fines:,} грн\n"
        f"🎁 Бонуси: {total_bonuses:,} грн\n"
        f"{'🔴' if to_pay > 0 else '🟢'} До виплати: {to_pay:,} грн"
    )



def calc_salary(target_uid, month=None):
    """Рахує нараховано/виплачено/штрафи/бонуси/до виплати."""
    data = get_salary_data(target_uid)
    rate = data.get("rate", 2150)
    completed = get_completed_batteries(target_uid, month)
    accrued = len(completed) * rate
    records = data.get("records", [])
    paid = sum(r["amount"] for r in records if r["type"] in ["pay","advance"] and r.get("confirmed") and (not month or r.get("date","")[3:10] == month))
    pending = sum(r["amount"] for r in records if r["type"] in ["pay","advance"] and not r.get("confirmed") and (not month or r.get("date","")[3:10] == month))
    fines = sum(r["amount"] for r in records if r["type"] == "fine" and (not month or r.get("date","")[3:10] == month))
    bonuses = sum(r["amount"] for r in records if r["type"] == "bonus" and (not month or r.get("date","")[3:10] == month))
    # До виплати рахуємо ВСI виплати (i пiдтвердженi, i непiдтвердженi)
    # iнакше адмiн може заплатити двiчi
    to_pay = accrued - paid - pending - fines + bonuses
    return {"accrued": accrued, "paid": paid, "pending": pending, "fines": fines, "bonuses": bonuses, "to_pay": to_pay, "count": len(completed), "rate": rate}

def format_salary_text(target_uid, name, month=None):
    """Форматує текст зарплати для відображення."""
    s = calc_salary(target_uid, month)
    month_str = f" ({month})" if month else " (всього)"
    pending_line = ""
    if s.get("pending", 0) > 0:
        pending_line = f"⏳ Очiкує пiдтвердження: {s['pending']} грн\n"
    return (
        f"💰 Зарплата — {name}{month_str}\n\n"
        f"📦 Завершено АКБ: {s['count']} шт\n"
        f"💵 Ставка: {s['rate']} грн/АКБ\n"
        f"💰 Нараховано: {s['accrued']} грн\n"
        f"✅ Виплачено: {s['paid']} грн\n"
        f"{pending_line}"
        f"⚠️ Штрафи: {s['fines']} грн\n"
        f"🎁 Бонуси: {s['bonuses']} грн\n"
        f"━━━━━━━━━━━━━━\n"
        f"💳 До виплати: {s['to_pay']} грн"
    )

def workshop_kb(uid=None, page=0):
    PAGE_SIZE = 5
    b = [["➕ Нова батарея"]]
    if uid:
        active = get_active_batteries(uid)
        total = len(active)
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        for num in active[start:end]:
            stages_done = len([s for s in workshop_db.get(num,{}).get("stages",[]) if s.get("user_id") == uid or s.get("user_id") == str(uid)])
            b.append(["🔋 " + num + " (" + str(stages_done) + "/" + str(len(WORKSHOP_STAGES)) + ")"])
        # Кнопки пагінації якщо батарей більше PAGE_SIZE
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append("◀️ Назад")
            nav.append(str(page + 1) + "/" + str(total_pages))
            if end < total:
                nav.append("▶️ Далі")
            b.append(nav)
    b.append(["🔄 Активнi роботи","📋 Iсторiя робiт"])
    b.append(["💰 Мої нарахування","📊 Моя статистика"])
    b.append(["🔙 Назад"])
    return ReplyKeyboardMarkup(b, resize_keyboard=True)

def make_paginated_inline(items, page, prefix, total_label):
    """Генерує InlineKeyboard з пагінацією. items — список (label, callback_data)"""
    PAGE_SIZE = 10
    total = len(items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    btns = [[InlineKeyboardButton(label, callback_data=cb)] for label, cb in items[start:end]]
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=prefix + str(page - 1)))
        nav.append(InlineKeyboardButton(str(page + 1) + "/" + str(total_pages), callback_data="noop"))
        if end < total:
            nav.append(InlineKeyboardButton("▶️", callback_data=prefix + str(page + 1)))
        btns.append(nav)
    return InlineKeyboardMarkup(btns)

# ── Iсторiя кабiнету: клавiатура з чекбоксами (для адмiн/god) ─
STAGE2_NAME = "2️⃣ Замiр опору"

def make_history_checkbox_kb(items, page, selected):
    """items — список (num, label_text). selected — set вибраних номерiв."""
    PAGE_SIZE = 6
    total = len(items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    btns = []
    for num, label in items[start:end]:
        mark = "☑" if num in selected else "☐"
        btns.append([
            InlineKeyboardButton(mark, callback_data=f"wwkct_{num}_{page}"),
            InlineKeyboardButton(label, callback_data="ws_" + num),
        ])
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data="wwkc_" + str(page - 1)))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if end < total:
            nav.append(InlineKeyboardButton("▶️", callback_data="wwkc_" + str(page + 1)))
        btns.append(nav)
    # Кнопки етапiв — по однiй на рядок
    for idx, sname in enumerate(WORKSHOP_STAGES):
        btns.append([InlineKeyboardButton("📷 " + sname, callback_data=f"wwkcs_{idx}")])
    # Окрема кнопка для опцiонального етапу Контроль якостi
    btns.append([InlineKeyboardButton(QC_STAGE_NAME, callback_data="wwkcs_qc")])
    return InlineKeyboardMarkup(btns)

def workshop_stages_kb(akb_num):
    btns = [[s] for s in WORKSHOP_STAGES]
    btns.append(["🔙 Назад до кабiнету"])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

# Всі кнопки діагностики
DIAG_BUTTONS = [
    "📦 Пакет","🔍 Фрейм","⚡️ Ячейки","🌡 Температури","🔋 Напруга/SOC",
    "📊 Offsets","🔢 Сирi байти","📍 Пошук значення","🔄 Протокол",
    "⚖️ Баланс деталi","🔌 Статус деталi","📈 Ємнiсть Ah",
    "🚨 Аварiйнi коди","🔁 Тест стабiльностi","🕐 Час вiдповiдi",
    "📶 RSSI сигнал","🔩 Опiр проводiв","📡 Перехват пакетiв","📤 Вiдправити HEX",
]

# Категорії діагностики
DIAG_CATEGORIES = [
    "📊 Данi BMS","📡 Мережа i протокол","⚡️ Стан системи","🔧 Iнструменти"
]

def diag_kb():
    """Головне меню діагностики - категорії"""
    return ReplyKeyboardMarkup([
        ["📊 Данi BMS","📡 Мережа i протокол"],
        ["⚡️ Стан системи","🔧 Iнструменти"],
        ["🔙 Назад до налаштувань"],
    ], resize_keyboard=True)

def diag_data_kb():
    """Дані BMS"""
    return ReplyKeyboardMarkup([
        ["📦 Пакет","🔍 Фрейм"],
        ["⚡️ Ячейки","🌡 Температури"],
        ["🔋 Напруга/SOC"],
        ["🔙 Назад до дiагностики"],
    ], resize_keyboard=True)

def diag_network_kb():
    """Мережа і протокол"""
    return ReplyKeyboardMarkup([
        ["🔄 Протокол","📊 Offsets"],
        ["🔢 Сирi байти","📶 RSSI сигнал"],
        ["📡 Перехват пакетiв","📤 Вiдправити HEX"],
        ["🔙 Назад до дiагностики"],
    ], resize_keyboard=True)

def diag_hex_kb():
    """Підменю вибору FFE1/FFE2"""
    return ReplyKeyboardMarkup([
        ["📤 FFE1","📤 FFE2"],
        ["🔙 Назад до дiагностики"],
    ], resize_keyboard=True)

def diag_status_kb():
    """Стан системи"""
    return ReplyKeyboardMarkup([
        ["⚖️ Баланс деталi","🔌 Статус деталi"],
        ["📈 Ємнiсть Ah","🚨 Аварiйнi коди"],
        ["🔙 Назад до дiагностики"],
    ], resize_keyboard=True)

def diag_tools_kb():
    """Інструменти"""
    return ReplyKeyboardMarkup([
        ["📍 Пошук значення","🔩 Опiр проводiв"],
        ["🔁 Тест стабiльностi","🕐 Час вiдповiдi"],
        ["🔎 Знайти адресу"],
        ["🔙 Назад до дiагностики"],
    ], resize_keyboard=True)

# ── АЛЕРТИ ───────────────────────────────────────────────────
async def alert_loop(app, chat_id):
    while state.alerts_enabled:
        await asyncio.sleep(state.alert_interval)
        if not state.client or not state.client.is_connected: continue
        d = await read_bms_data()
        if not d: continue
        alerts = []
        if "cell_max" in d and d["cell_max"] > 4.20: alerts.append(f"🔴 ПЕРЕЗАРЯД! {d['cell_max']}V")
        if "cell_min" in d and d["cell_min"] < 3.00: alerts.append(f"🪫 РОЗРЯД! {d['cell_min']}V")
        if "temp1" in d and d["temp1"] > 80: alerts.append(f"🌡 ПЕРЕГРIВ! {d['temp1']}C")
        if "temp_mos" in d and d["temp_mos"] > 85: alerts.append(f"🔧 MOS! {d['temp_mos']}C")
        if "cell_delta" in d and d["cell_delta"] > 0.1: alerts.append(f"⚠️ Дисбаланс: {d['cell_delta']}V")
        if alerts:
            audit(GOD_ID, f"АЛЕРТ: {alerts[0]}")
            await app.bot.send_message(chat_id=chat_id, text="🚨 УВАГА!\n\n" + "\n".join(alerts))

# ── ДІАГНОСТИКА ──────────────────────────────────────────────
async def run_diag(client, text):
    buf = bytearray()
    def dh(_, d): buf.extend(d)
    await client.start_notify(JK_CHAR_UUID, dh)
    buf.clear()
    await client.write_gatt_char(JK_CHAR_UUID, CMD_DEVICE_INFO, response=True)
    await asyncio.sleep(1.0); buf.clear()
    await client.write_gatt_char(JK_CHAR_UUID, CMD_CELL_INFO, response=True)
    await asyncio.sleep(2.0)
    try: await client.stop_notify(JK_CHAR_UUID)
    except Exception: pass
    data = bytes(buf)
    best = data; best_size = 0; ix = 0
    while ix < len(data) - 4:
        if data[ix:ix+4] == bytes([0x55,0xAA,0xEB,0x90]):
            nx = data.find(bytes([0x55,0xAA,0xEB,0x90]), ix+4)
            fr = data[ix:nx] if nx > 0 else data[ix:]
            if len(fr)>4 and fr[4]==0x02 and len(fr)>best_size:
                best = fr; best_size = len(fr)
            ix = nx if nx > 0 else len(data)
        else: ix += 1
    if best_size == 0:
        ix = 0
        while ix < len(data) - 4:
            if data[ix:ix+4] == bytes([0x55,0xAA,0xEB,0x90]):
                nx = data.find(bytes([0x55,0xAA,0xEB,0x90]), ix+4)
                fr = data[ix:nx] if nx > 0 else data[ix:]
                if len(fr)>best_size: best = fr; best_size = len(fr)
                ix = nx if nx > 0 else len(data)
            else: ix += 1
    out = ""
    if text == "📦 Пакет":
        ftype = ("0x%02X" % best[4]) if len(best)>4 else "?"
        fname = "Cell Data" if (len(best)>4 and best[4]==0x02) else "Device Info"
        out = "📦 Пакет:\nРозмiр: %d байт\nТип: %s (%s)\nЗаголовок: %s\nБайти 6-14: %s\nПротокол: JK02_24S" % (
            len(best), ftype, fname, str(list(best[:6])), str(list(best[6:14])))
    elif text == "🔍 Фрейм":
        out = "🔍 Фрейми:\n"; i2 = 0
        while i2 < len(data) - 4:
            if data[i2:i2+4] == bytes([0x55,0xAA,0xEB,0x90]):
                n2 = data.find(bytes([0x55,0xAA,0xEB,0x90]), i2+4)
                fr2 = data[i2:n2] if n2>0 else data[i2:]
                ft2 = fr2[4] if len(fr2)>4 else 0
                nm = "Cell Data ✅" if ft2==0x02 else ("Device Info" if ft2==0x03 else "0x%02X"%ft2)
                out += "%d байт — %s\n" % (len(fr2), nm)
                i2 = n2 if n2>0 else len(data)
            else: i2 += 1
    elif text == "⚡️ Ячейки":
        out = "⚡️ Ячейки (%d байт):\nБайти 6-30: %s\n\n" % (len(best), str(list(best[6:30])))
        for ci in range(24):
            off = 6 + ci*2
            if off+1 >= len(best): break
            v = (best[off+1]<<8)|best[off]; mv = v/1000.0
            flag = "✅" if 2.5<=mv<=4.5 else ("⏹" if mv==0 else "❌")
            out += "C%d[%d]: %dmВ=%.3fV %s\n" % (ci+1, off, v, mv, flag)
            if ci >= 9: out += "...\n"; break
    elif text == "🌡 Температури":
        out = "🌡 Температури:\n\n"
        for tname, toff in [("MOS",144),("T1",164),("T2",162)]:
            if toff < len(best):
                raw = best[toff]
                out += "%s off=%d: raw=%d -> %.1fC\n" % (tname, toff, raw, raw/10.0)
    elif text == "🔋 Напруга/SOC":
        out = "🔋 Напруга/Струм/SOC:\n\n"
        if 151 < len(best):
            v = (best[151]<<8)|best[150]
            out += "Напруга off=150: %dmВ=%.2fV\n" % (v, v/1000.0)
        if 173 < len(best): out += "SOC off=173: %d%%\n" % best[173]
        if 76 < len(best):
            st = best[76]
            out += "Статус off=76: 0x%02X\n" % st
            out += "Заряд: %s Розряд: %s\n" % ("✅" if st&1 else "❌", "✅" if st&2 else "❌")
    elif text == "📊 Offsets":
        out = "📊 Offsets (%d байт):\n\nЯчейки: 6 (крок 2)\nНапруга: 150\nSOC: 173\nСтатус: 76\nMOS: 144\nT1: 145 T2: 146\nЦикли: 182" % len(best)
    elif text == "🔢 Сирi байти":
        out = "🔢 Байти (%d):\n\n" % len(best)
        for row in range(0, min(len(best),160), 16):
            chunk = best[row:row+16]
            out += "[%03d] %s\n" % (row, " ".join("%02X"%b for b in chunk))
        if len(best) > 160: out += "...(%d/%d)\n" % (160, len(best))
    elif text == "🔄 Протокол":
        out = "🔄 Протокол:\nТип: 0x%02X\nРозмiр: %d байт\nJK02_24S" % (best[4] if len(best)>4 else 0, len(best))
    elif text == "⚖️ Баланс деталi":
        out = "⚖️ Балансування:\n\n"
        if 76 < len(best): out += "Балансування: %s\n" % ("✅ ON" if best[76]&4 else "— OFF")
    elif text == "🔌 Статус деталi":
        out = "🔌 Статус:\n\n"
        if 167 < len(best):
            out += "Заряд off=166: %s\nРозряд off=167: %s\nБаланс off=76: 0x%02X" % (
                "✅ ON" if best[166] else "❌ OFF",
                "✅ ON" if best[167] else "❌ OFF",
                best[76] if 76 < len(best) else 0)
    elif text == "📈 Ємнiсть Ah":
        out = "📈 Ємнiсть:\n\n"
        if 173 < len(best): out += "SOC: %d%%\n" % best[173]
        if "remain_cap" in state.live_data: out += "Залишок: %.1f Ah\n" % state.live_data["remain_cap"]
    elif text == "🚨 Аварiйнi коди":
        out = "🚨 Аварiї:\n\n✅ Немає (offset визначається)"
    elif text == "🔩 Опiр проводiв":
        out = "🔩 Опiр проводiв:\n\n"
        for ci in range(7):
            off3 = 80 + ci*2
            if off3+1 < len(best):
                r = (best[off3+1]<<8)|best[off3]
                out += "C%d: %d мОм %s\n" % (ci+1, r, "✅" if r<100 else "⚠️")
    return out or "Немає даних"

# ── CALLBACK ─────────────────────────────────────────────────
async def callback_handler(update, ctx):
    q = update.callback_query; uid = q.from_user.id; data = q.data
    await q.answer()
    if data.startswith("ep_"):
        rest = data[3:]; gk = rest.rsplit("_",1)[1]; param = rest.rsplit("_",1)[0]
        if param in MY_PRESET:
            pr = MY_PRESET[param]
            session.waiting_for[uid] = "edit_param"
            session.temp_data[uid] = {"param":param,"group":gk}
            await q.message.reply_text(f"✏️ {pr['desc']}\nПоточне: {format_param_val(param, pr['val'])} {pr['unit']}\n\nВведи нове значення:")
    elif data.startswith("wr_"):
        gk = data[3:]
        if not state.client or not state.client.is_connected:
            await q.message.reply_text("❌ Спочатку пiдключись!"); return
        g = PARAM_GROUPS[gk]
        params = {p:MY_PRESET[p]["val"] for p in g["params"] if p in MY_PRESET and p in PARAMS_ADDR}
        msg = await q.message.reply_text(f"⏳ Записую {g['name']}...")
        ok = 0
        for p,v in params.items():
            if await write_param(p,v): ok += 1
            await asyncio.sleep(0.2)
        audit(uid, f"Записано {g['name']}", f"{ok}/{len(params)}")
        await msg.edit_text(f"✅ {g['name']} записано!\n{ok}/{len(params)} параметрiв")
    elif data == "bp":
        await q.message.reply_text("📋 Пресети:", reply_markup=presets_kb(uid))

    elif data.startswith("user_"):
        target = data[5:]
        if target in users_db:
            info = users_db[target]
            r = info.get("role","user") if isinstance(info,dict) else info
            n = info.get("name",target) if isinstance(info,dict) else target
            perms = info.get("permissions",[]) if isinstance(info,dict) else []
            perm_list = [PERM_NAMES.get(p,p) for p in perms if p != "all"]
            perm_text = ", ".join(perm_list) if perm_list else ("Всi права" if "all" in perms else "Немає")
            work_count = sum(1 for num,wdata in workshop_db.items()
                           for s in wdata.get("stages",[]) if str(s.get("user_id","")) == target)
            user_text = role_icon(r) + " " + n + "\n📋 Роль: " + r + "\n🔑 Права: " + perm_text + "\n🏭 Робiт: " + str(work_count)
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Кабiнет", callback_data="uwk_" + target),
                 InlineKeyboardButton("✏️ Права", callback_data="tp_" + target + "_VIEW")],
                [InlineKeyboardButton("🗑 Видалити", callback_data="udel_" + target),
                 InlineKeyboardButton("🔙 Назад", callback_data="users_list")]
            ])
            await q.message.reply_text(user_text, reply_markup=btns)

    elif data == "users_list":
        clean = {k:v for k,v in users_db.items() if k != str(GOD_ID) and k and k != "None"}
        btns = []
        for uid2,info in clean.items():
            r = info.get("role","user") if isinstance(info,dict) else info
            n = info.get("name",uid2) if isinstance(info,dict) else uid2
            btns.append([InlineKeyboardButton(role_icon(r) + " " + n + " — " + r, callback_data="user_" + uid2)])
        await q.message.reply_text("👥 Всi юзери:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("addu_"):
        # addu_{new_uid}_user або addu_{new_uid}_admin або addu_{new_uid}_deny
        parts = data[5:].rsplit("_", 1)
        if len(parts) == 2:
            new_uid, role = parts
            if not is_god(uid): return
            if role == "deny":
                await q.message.edit_text(f"❌ Доступ вiдхилено для ID: {new_uid}")
            elif role in ["user", "admin"]:
                users_db[new_uid] = {"role": role, "permissions": DEFAULT_PERMISSIONS[role], "name": new_uid}
                save_json(USERS_FILE, users_db)
                audit(uid, f"Додано юзера {new_uid}", role)
                await q.message.edit_text(f"✅ {role_icon(role)} {new_uid} — {role} додано!")

    elif data.startswith("udel_"):
        target = data[5:]
        if target in users_db:
            n = users_db[target].get("name",target) if isinstance(users_db[target],dict) else target
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Так", callback_data="udelc_" + target),
                 InlineKeyboardButton("❌ Нi", callback_data="user_" + target)]
            ])
            await q.message.reply_text("⚠️ Видалити " + n + "?", reply_markup=btns)

    elif data.startswith("udelc_"):
        target = data[6:]
        if target in users_db:
            n = users_db[target].get("name",target) if isinstance(users_db[target],dict) else target
            del users_db[target]; save_json(USERS_FILE, users_db)
            audit(uid, "Видалено юзера " + n)
            await q.message.edit_text("✅ Юзера " + n + " видалено")

    elif data.startswith("uwk_"):
        target = data[4:]
        n = users_db.get(target,{}).get("name",target) if target in users_db else ("Kaatastroffaa" if str(target) == str(GOD_ID) else target)
        # Зберігаємо target_uid для навігації
        session.temp_data[uid] = session.temp_data.get(uid, {})
        session.temp_data[uid]["view_worker_uid"] = target
        session.temp_data[uid]["view_worker_name"] = n
        # Чистимо вибiр з попереднього юзера
        session.temp_data[uid]["wwkc_selected"] = set()
        session.temp_data[uid].pop("wwkc_items", None)
        # Активні батареї цього юзера
        active = []
        for num, wdata in workshop_db.items():
            stages = wdata.get("stages", [])
            user_stages = [s for s in stages if str(s.get("user_id","")) == str(target)]
            if user_stages and len(user_stages) < len(WORKSHOP_STAGES):
                active.append((num, len(user_stages)))
        # Завершені батареї
        completed = []
        for num, wdata in workshop_db.items():
            stages = wdata.get("stages", [])
            user_stages = [s for s in stages if str(s.get("user_id","")) == str(target)]
            if len(user_stages) >= len(WORKSHOP_STAGES):
                completed.append(num)
        total = len(active) + len(completed)
        if total == 0:
            await q.message.reply_text(
                "🪪 Кабiнет: " + n + "\n\nЗаписiв ще немає",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))
        else:
            btns = [["🔄 Активнi (" + str(len(active)) + ")", "📋 Iсторiя (" + str(len(completed)) + ")"]]
            if is_god(uid) or get_role(uid) == "admin":
                if is_god(uid) or has_perm(uid,"salary"):
                    btns.append(["💰 Зарплата"])
            btns.append(["🔙 Назад"])
            await q.message.reply_text(
                "🪪 Кабiнет: " + n + "\nАктивних: " + str(len(active)) + " | Завершених: " + str(len(completed)),
                reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))

    elif data.startswith("tp_") and "_VIEW" in data:
        target_uid_view = data[3:data.rfind("_VIEW")]
        if target_uid_view in users_db:
            n = users_db[target_uid_view].get("name",target_uid_view) if isinstance(users_db[target_uid_view],dict) else target_uid_view
            # Скидаємо буфери прав i ролi щоб починати з актуальних
            td = session.temp_data.get(uid, {})
            td.pop("perm_buf_" + str(target_uid_view), None)
            td.pop("role_buf_" + str(target_uid_view), None)
            session.temp_data[uid] = td
            await q.message.reply_text("✏️ Права для " + n + ":",
                reply_markup=user_perms_inline_kb(int(target_uid_view)))

    elif data.startswith("cps_"):
        # Копіювання фото для "Звіт опорів четвірки"
        td = session.temp_data.get(uid, {})
        candidates = td.get("copy_photo_candidates", [])
        selected = td.get("copy_photo_selected", [])

        if data == "cps_skip":
            akb_num = td.get("copy_photo_from","")
            stage = td.get("copy_photo_stage")
            stage_data = td.get("copy_photo_stage_data")
            # Зберігаємо основну батарею
            if akb_num and stage_data:
                if akb_num not in workshop_db:
                    workshop_db[akb_num] = {"akb_num": akb_num, "stages": []}
                workshop_db[akb_num]["stages"].append(stage_data)
                save_json(WORKSHOP_FILE, workshop_db)
                audit(uid, "Робочий кабiнет АКБ " + akb_num + " " + stage)
            session.temp_data.pop(uid, None)
            await q.edit_message_text("✅ Фото збережено тiльки для АКБ " + akb_num)
            if akb_num and akb_num in workshop_db:
                user_stages = [s for s in workshop_db[akb_num]["stages"] if str(s.get("user_id")) == str(uid)]
                next_idx = len(user_stages)
                if next_idx < len(WORKSHOP_STAGES):
                    next_stage = WORKSHOP_STAGES[next_idx]
                    needs_two_next = next_stage in WORKSHOP_TWO_PHOTOS
                    hint = "\n📸 Потрiбно 2 фото!" if needs_two_next else ""
                    session.temp_data[uid] = {"bms_number": akb_num, "akb_num": akb_num}
                    await q.message.reply_text(
                        "📟 АКБ: " + akb_num + "\nНаступний етап:\n" + next_stage + hint,
                        reply_markup=ReplyKeyboardMarkup([[next_stage], ["🔙 Назад до кабiнету"]], resize_keyboard=True))
                else:
                    await q.message.reply_text(
                        "🎉 Всi етапи завершено!\n📟 АКБ: " + akb_num,
                        reply_markup=workshop_kb(uid))
            return

        if data == "cps_confirm":
            if not selected:
                await q.answer("Оберiть хоча б одну батарею або натиснiть ✳️", show_alert=True)
                return
            if len(selected) < 3:
                await q.answer(f"⚠️ Потрiбно вибрати рівно 3 батареї!\nЗараз вибрано: {len(selected)}/3", show_alert=True)
                return
            file_id = td.get("copy_photo_file")
            stage = td.get("copy_photo_stage")
            stage_data = td.get("copy_photo_stage_data")
            akb_num = td.get("copy_photo_from")
            # Зберігаємо основну батарею
            if akb_num:
                if akb_num not in workshop_db:
                    workshop_db[akb_num] = {"akb_num": akb_num, "stages": []}
                if stage_data:
                    workshop_db[akb_num]["stages"].append(stage_data)
            # Копіюємо на вибрані батареї
            copied = []
            for num in selected:
                if num not in workshop_db:
                    workshop_db[num] = {"akb_num":num,"stages":[]}
                existing = [s for s in workshop_db[num]["stages"] if s.get("stage") == stage and str(s.get("user_id")) == str(uid)]
                if not existing:
                    workshop_db[num]["stages"].append({
                        "stage": stage,
                        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
                        "user": get_name(uid),
                        "user_id": uid,
                        "photo": file_id
                    })
                    copied.append(num)
            save_json(WORKSHOP_FILE, workshop_db)
            all_nums = [akb_num] + copied
            audit(uid, f"Копiювання фото етапу {stage}", f"АКБ: {', '.join(all_nums)}")
            session.temp_data.pop(uid, None)
            await q.edit_message_text(f"✅ Фото та етап збережено для:\n{', '.join(all_nums)}")
            # Показуємо наступний етап для основної батареї
            if akb_num and akb_num in workshop_db:
                user_stages = [s for s in workshop_db[akb_num]["stages"] if str(s.get("user_id")) == str(uid)]
                next_idx = len(user_stages)
                if next_idx < len(WORKSHOP_STAGES):
                    next_stage = WORKSHOP_STAGES[next_idx]
                    needs_two_next = next_stage in WORKSHOP_TWO_PHOTOS
                    hint = "\n📸 Потрiбно 2 фото!" if needs_two_next else ""
                    session.temp_data[uid] = {"bms_number": akb_num, "akb_num": akb_num}
                    await q.message.reply_text(
                        "📟 АКБ: " + akb_num + "\nНаступний етап:\n" + next_stage + hint,
                        reply_markup=ReplyKeyboardMarkup([[next_stage], ["🔙 Назад до кабiнету"]], resize_keyboard=True))
                else:
                    await q.message.reply_text(
                        "🎉 Всi етапи завершено!\n📟 АКБ: " + akb_num,
                        reply_markup=workshop_kb(uid))
            return

        # Вибір/зняття вибору батареї
        num = data[4:]
        if num in selected:
            selected.remove(num)
        elif len(selected) < 3:
            selected.append(num)
        else:
            await q.answer("Максимум 3 батареї!", show_alert=True)
            return
        session.temp_data[uid]["copy_photo_selected"] = selected
        # Оновлюємо кнопки
        btns = []
        for n in candidates:
            icon = "✅" if n in selected else "⬜"
            btns.append([InlineKeyboardButton(f"{icon} АКБ {n}", callback_data=f"cps_{n}")])
        confirm_label = f"✅ Пiдтвердити ({len(selected)}/3)" if len(selected) == 3 else f"🔒 Пiдтвердити ({len(selected)}/3)"
        btns.append([InlineKeyboardButton(confirm_label, callback_data="cps_confirm"),
                     InlineKeyboardButton("✳️ Тiльки для " + td.get("copy_photo_from",""), callback_data="cps_skip")])
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btns))
        await q.answer()

    elif data.startswith("akb_"):
        num = data[4:]
        if num in akb_db:
            rec = akb_db[num]
            hist = rec.get("history",[])
            if hist:
                last = hist[-1]
                status = last.get("status","")
                date = last.get("date","")
                comment = last.get("comment","")
                count = len(hist)
            else:
                status = rec.get("status","")
                date = rec.get("date","")
                comment = rec.get("comment","")
                count = len(rec.get("photos",[]))
            icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
            created_by = rec.get("created_by","")
            created_date = rec.get("date","")
            created_str = f"\n👤 Створив: {created_by} | {created_date}" if created_by else ""
            last_user = last.get("user","") if hist else ""
            last_str = f"\n✏️ Останній запис: {last_user} | {date}" if last_user else f"\n📅 {date}"
            t = f"{icon} АКБ: {num}{created_str}{last_str}\n📊 {status}\n💬 {comment}\n📝 Записiв: {count}"
            # Показуємо кнопки дій
            action_btns_rows = [
                [InlineKeyboardButton("📋 Вся iсторiя", callback_data=f"akbh_{num}"),
                 InlineKeyboardButton("➕ Новий запис", callback_data=f"akbn_{num}")],
            ]
            if is_god(uid) or get_role(uid) == "admin":
                action_btns_rows.append([InlineKeyboardButton("🗑 Видалити АКБ", callback_data=f"akbdel_{num}")])
            action_btns_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="akb_list")])
            action_btns = InlineKeyboardMarkup(action_btns_rows)
            await q.message.reply_text(t, reply_markup=action_btns)
            # Показуємо останнє фото
            if hist and hist[-1].get("photos"):
                try: await q.message.reply_photo(hist[-1]["photos"][0]["file_id"])
                except Exception: pass
            elif rec.get("photos"):
                try: await q.message.reply_photo(rec["photos"][0]["file_id"])
                except Exception: pass

    elif data.startswith("ws_"):
        num = data[3:]
        if num in workshop_db:
            stages = workshop_db[num].get("stages",[])
            # Показуємо кнопки по етапах
            btns = []
            for i, stage_name in enumerate(WORKSHOP_STAGES):
                # Знаходимо чи є фото для цього етапу
                stage_data = next((s for s in stages if s.get("stage") == stage_name), None)
                if stage_data:
                    icon = "✅"
                    label = icon + " " + stage_name + " | " + stage_data.get("user","") + " | " + stage_data.get("date","")[:10]
                else:
                    label = stage_name
                btns.append([InlineKeyboardButton(label, callback_data="wss_" + num + "_" + str(i))])
            # Опцiональний етап — Контроль якостi (вiдео)
            qc_stage = next((s for s in stages if s.get("stage","") in QC_STAGE_NAMES_ALL), None)
            qc_label = QC_STAGE_NAME
            if qc_stage:
                qc_label = "✅ " + QC_STAGE_NAME + " | " + qc_stage.get("user","") + " | " + qc_stage.get("date","")[:10]
            btns.append([InlineKeyboardButton(qc_label, callback_data="wsqc_" + num)])
            # Кнопки для God і Admin
            if is_god(uid) or get_role(uid) == "admin":
                btns.append([InlineKeyboardButton("✏️ Змiнити номер БМС", callback_data="wsrenum_" + num)])
            if is_god(uid):
                btns.append([InlineKeyboardButton("🗑 Видалити батарею", callback_data="wsbatdel_" + num)])
            # Етапи "основнi" — тiльки тi що в WORKSHOP_STAGES (5 шт.)
            total_done = len([s for s in stages if s.get("stage") in WORKSHOP_STAGES])
            header = "📋 АКБ №" + num + " (" + str(total_done) + "/" + str(len(WORKSHOP_STAGES)) + " етапiв)"
            # Дата завершення — з останнього з основних етапiв якщо все пройдено
            if total_done >= len(WORKSHOP_STAGES):
                main_stages = [s for s in stages if s.get("stage") in WORKSHOP_STAGES]
                if main_stages:
                    last_date = main_stages[-1].get("date","")
                    if last_date:
                        header += " | " + last_date[:10]
            header += ":"
            await q.message.reply_text(
                header,
                reply_markup=InlineKeyboardMarkup(btns))

    elif data == "ws_back":
        await q.message.reply_text("🪪 Робочий кабiнет:", reply_markup=workshop_kb(uid))

    elif data.startswith("wkp_"):
        # Пагінація списку працівників
        page = int(data[4:])
        items_w = session.temp_data.get(uid, {}).get("workers_items")
        if not items_w:
            clean = {k:v for k,v in users_db.items()
                     if k and k != "None"
                     and (v.get("role","user") if isinstance(v,dict) else v) in ["user","admin"]}
            items_w = []
            god_name = users_db.get(str(GOD_ID), {}).get("name","Kaatastroffaa") if str(GOD_ID) in users_db else "Kaatastroffaa"
            items_w.append(("👤 " + god_name, "uwk_" + str(GOD_ID)))
            for uid2,info in clean.items():
                if uid2 == str(GOD_ID): continue
                n2 = info.get("name",uid2) if isinstance(info,dict) else uid2
                items_w.append(("👤 " + n2, "uwk_" + uid2))
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["workers_items"] = items_w
        await q.message.edit_reply_markup(reply_markup=make_paginated_inline(items_w, page, "wkp_", "працiвникiв"))

    elif data.startswith("sal_"):
        parts = data.split("_", 2)
        action = parts[1] if len(parts) > 1 else ""
        target = parts[2] if len(parts) > 2 else ""
        # confirm i dispute — доступнi всiм (юзеру пiдтверджує свою виплату)
        # iншi дiї — тiльки адмiн/god
        if action not in ("confirm", "dispute"):
            if not (is_god(uid) or get_role(uid) == "admin"):
                await q.answer("❌ Немає прав"); return
        name = users_db.get(target,{}).get("name",target) if target in users_db else ("Kaatastroffaa" if str(target)==str(GOD_ID) else target)

        if action == "pay":
            session.waiting_for[uid] = "salary_input"
            session.temp_data[uid] = session.temp_data.get(uid,{})
            session.temp_data[uid].update({"sal_action":"pay","sal_target":target,"sal_name":name})
            await q.message.reply_text(f"💵 Виплата для {name}\nВведи суму (грн) та коментар через кому:\nНаприклад: 10000, За травень")

        elif action == "adv":
            session.waiting_for[uid] = "salary_input"
            session.temp_data[uid] = session.temp_data.get(uid,{})
            session.temp_data[uid].update({"sal_action":"advance","sal_target":target,"sal_name":name})
            await q.message.reply_text(f"💵 Аванс для {name}\nВведи суму (грн) та коментар через кому:\nНаприклад: 5000, Аванс за травень")

        elif action == "fine":
            session.waiting_for[uid] = "salary_input"
            session.temp_data[uid] = session.temp_data.get(uid,{})
            session.temp_data[uid].update({"sal_action":"fine","sal_target":target,"sal_name":name})
            await q.message.reply_text(f"⚠️ Штраф для {name}\nВведи суму (грн) та причину через кому:\nНаприклад: 500, Брак АКБ 34137")

        elif action == "bonus":
            session.waiting_for[uid] = "salary_input"
            session.temp_data[uid] = session.temp_data.get(uid,{})
            session.temp_data[uid].update({"sal_action":"bonus","sal_target":target,"sal_name":name})
            await q.message.reply_text(f"🎁 Бонус для {name}\nВведи суму (грн) та причину через кому:\nНаприклад: 1000, Перевиконання плану")

        elif action == "rate":
            session.waiting_for[uid] = "salary_input"
            session.temp_data[uid] = session.temp_data.get(uid,{})
            session.temp_data[uid].update({"sal_action":"rate","sal_target":target,"sal_name":name})
            data2 = get_salary_data(target)
            await q.message.reply_text(f"✏️ Змiна ставки для {name}\nПоточна: {data2.get('rate',2150)} грн/АКБ\nВведи нову ставку:")

        elif action == "hist":
            data2 = get_salary_data(target)
            records = data2.get("records",[])
            if not records:
                await q.message.reply_text(f"📋 Iсторiя — {name}\n\nЗаписiв немає")
                return
            icons = {"pay":"💵","advance":"💵","fine":"⚠️","bonus":"🎁"}
            labels = {"pay":"Виплата","advance":"Аванс","fine":"Штраф","bonus":"Бонус"}
            txt = f"📋 Iсторiя — {name}\n\n"
            for r in records[-20:]:
                icon = icons.get(r["type"],"💰")
                label = labels.get(r["type"],r["type"])
                conf = "✅" if r.get("confirmed") else "⏳"
                txt += f"{icon} {r.get('date','')} — {label} {r['amount']} грн\n"
                txt += f"   {r.get('comment','')} | {conf}\n\n"
            await q.message.reply_text(txt)

        elif action == "myhistory":
            # Юзер переглядає свою власну історію
            data2 = get_salary_data(uid)
            records = data2.get("records",[])
            if not records:
                await q.message.reply_text("📋 Моя iсторiя виплат\n\nЗаписiв ще немає")
                return
            icons = {"pay":"💵","advance":"💵","fine":"⚠️","bonus":"🎁"}
            labels = {"pay":"Виплата","advance":"Аванс","fine":"Штраф","bonus":"Бонус"}
            txt = "📋 Моя iсторiя виплат\n\n"
            for r in records[-20:]:
                icon = icons.get(r["type"],"💰")
                label = labels.get(r["type"],r["type"])
                conf = "✅ Пiдтверджено" if r.get("confirmed") else "⏳ Очiкує пiдтвердження"
                txt += f"{icon} {r.get('date','')[:10]} — {label} {r['amount']} грн\n"
                if r.get("comment"):
                    txt += f"   📝 {r['comment']}\n"
                txt += f"   👤 вiд {r.get('by','')} | {conf}\n\n"
            await q.message.reply_text(txt)

        elif action == "confirm":
            # Юзер пiдтверджує отримання виплати — належить йому
            rec_id = target
            sal_uid = str(uid)
            found = False
            if sal_uid in salary_db:
                for r in salary_db[sal_uid].get("records",[]):
                    if r.get("id") == rec_id:
                        if r.get("confirmed"):
                            await q.answer("✅ Вже пiдтверджено", show_alert=True)
                            found = True
                            break
                        r["confirmed"] = True
                        r["confirmed_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
                        save_json(SALARY_FILE, salary_db)
                        sal_name = users_db.get(sal_uid,{}).get("name",sal_uid)
                        try:
                            await q.message.edit_text(f"✅ Виплату пiдтверджено!\nОтримано: {r['amount']} грн")
                        except Exception:
                            await q.message.reply_text(f"✅ Виплату пiдтверджено!\nОтримано: {r['amount']} грн")
                        # Сповiщаємо God i всiх адмiнiв
                        for u_id, info in users_db.items():
                            role = info.get("role") if isinstance(info,dict) else info
                            if role in ["god","admin"] and int(u_id) != uid:
                                try: await ctx.bot.send_message(chat_id=int(u_id), text=f"✅ {sal_name} пiдтвердив отримання {r['amount']} грн")
                                except: pass
                        audit(uid, "Пiдтверджено виплату", f"{r['amount']} грн")
                        found = True
                        break
            if not found:
                await q.answer("❌ Запис не знайдено або не ваш", show_alert=True)

        elif action == "dispute":
            rec_id = target
            sal_uid = str(uid)
            sal_name = users_db.get(sal_uid,{}).get("name",sal_uid)
            # Перевiрка що запис справдi належить юзеру
            found = False
            if sal_uid in salary_db:
                for r in salary_db[sal_uid].get("records",[]):
                    if r.get("id") == rec_id:
                        found = True
                        break
            if not found:
                await q.answer("❌ Запис не знайдено або не ваш", show_alert=True); return
            try:
                await q.message.edit_text(f"⚠️ Оскарження надiслано адмiну")
            except Exception:
                await q.message.reply_text(f"⚠️ Оскарження надiслано адмiну")
            for u_id, info in users_db.items():
                role = info.get("role") if isinstance(info,dict) else info
                if role in ["god","admin"]:
                    try: await ctx.bot.send_message(chat_id=int(u_id), text=f"⚠️ {sal_name} оскаржує виплату!\nРек. ID: {rec_id}\nПотрiбно розiбратись.")
                    except: pass
            audit(uid, "Оскарження виплати", f"rec_id={rec_id}")

    elif data == "noop":
        pass  # Кнопка номера сторінки — нічого не робить

    elif data.startswith("wsa_"):
        # Пагінація активних робіт
        page = int(data[4:])
        active_nums = get_active_batteries(uid)
        items = []
        for num in active_nums:
            stages = workshop_db.get(num,{}).get("stages",[])
            user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
            count = len(user_stages)
            items.append(("🔄 " + num + " (" + str(count) + "/" + str(len(WORKSHOP_STAGES)) + ")", "ws_" + num))
        await q.message.edit_reply_markup(reply_markup=make_paginated_inline(items, page, "wsa_", "активних"))

    elif data.startswith("wwka_"):
        # Пагінація кабінету працівника (активні)
        page = int(data[5:])
        td = session.temp_data.get(uid, {})
        target = td.get("view_worker_uid")
        n = td.get("view_worker_name", "?")
        if not target:
            await q.answer("❌ Сесія втрачена", show_alert=True); return
        items = []
        for num, wdata in workshop_db.items():
            stages = wdata.get("stages", [])
            user_stages = [s for s in stages if str(s.get("user_id","")) == str(target)]
            count = len(user_stages)
            if count > 0 and count < len(WORKSHOP_STAGES):
                items.append(("🔋 " + num + " (" + str(count) + "/" + str(len(WORKSHOP_STAGES)) + ")", "ws_" + num))
        await q.message.edit_reply_markup(reply_markup=make_paginated_inline(items, page, "wwka_", "батарей"))

    elif data.startswith("wwkc_"):
        # Пагінація кабінету працівника (завершені) — з чекбоксами
        page = int(data[5:])
        td = session.temp_data.get(uid, {})
        target = td.get("view_worker_uid")
        if not target:
            await q.answer("❌ Сесія втрачена", show_alert=True); return
        items = td.get("wwkc_items")
        if items is None:
            items = []
            for num, wdata in workshop_db.items():
                stages = wdata.get("stages", [])
                user_stages = [s for s in stages if str(s.get("user_id","")) == str(target)]
                if len(user_stages) >= len(WORKSHOP_STAGES):
                    short_num = ("..." + num[-10:]) if len(num) > 12 else num
                    items.append((num, "✅ " + short_num))
            td["wwkc_items"] = items
            session.temp_data[uid] = td
        selected = td.get("wwkc_selected", set())
        if not isinstance(selected, set): selected = set(selected)
        await q.message.edit_reply_markup(reply_markup=make_history_checkbox_kb(items, page, selected))

    elif data.startswith("wwkct_"):
        # Toggle чекбокса у iсторiї кабiнету
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌", show_alert=True); return
        rest = data[6:]
        parts = rest.rsplit("_", 1)
        num = parts[0]
        page = int(parts[1]) if len(parts) > 1 else 0
        td = session.temp_data.get(uid, {})
        target = td.get("view_worker_uid")
        if not target:
            await q.answer("❌ Сесія втрачена", show_alert=True); return
        selected = td.get("wwkc_selected", set())
        if not isinstance(selected, set): selected = set(selected)
        if num in selected:
            selected.discard(num)
        else:
            selected.add(num)
        td["wwkc_selected"] = selected
        items = td.get("wwkc_items")
        if items is None:
            items = []
            for num2, wdata in workshop_db.items():
                stages = wdata.get("stages", [])
                user_stages = [s for s in stages if str(s.get("user_id","")) == str(target)]
                if len(user_stages) >= len(WORKSHOP_STAGES):
                    short_num = ("..." + num2[-10:]) if len(num2) > 12 else num2
                    items.append((num2, "✅ " + short_num))
            td["wwkc_items"] = items
        session.temp_data[uid] = td
        try:
            await q.message.edit_reply_markup(reply_markup=make_history_checkbox_kb(items, page, selected))
        except Exception:
            pass

    elif data.startswith("wwkcs_"):
        # Надiслати фото/вiдео вибраного етапу з вибраних батарей
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Тiльки для адмiна", show_alert=True); return
        sub = data[6:]
        is_qc = (sub == "qc")
        stage_name = None
        stage_digit = None
        if is_qc:
            stage_name = QC_STAGE_NAME
        else:
            try:
                stage_idx = int(sub)
                stage_name = WORKSHOP_STAGES[stage_idx]
                stage_digit = stage_name[0]  # 1️⃣..5️⃣
            except (ValueError, IndexError):
                await q.answer("❌ Невiрний етап", show_alert=True); return
        td = session.temp_data.get(uid, {})
        target = td.get("view_worker_uid")
        selected = td.get("wwkc_selected", set())
        if not selected:
            await q.answer("❌ Нiчого не вибрано", show_alert=True); return
        if not target:
            await q.answer("❌ Сесія втрачена", show_alert=True); return
        # Збираємо фото i вiдео.
        # Для основних етапiв — за першим символом.
        # Для QC — з усiх альтернативних назв (старi записи з 6️⃣ / 7️⃣ Готовий АКБ
        # i новi з 📹 Контроль якостi).
        from telegram import InputMediaPhoto
        photos = []
        videos = []
        for num in selected:
            wdata = workshop_db.get(num, {})
            stages = wdata.get("stages", [])
            for s in stages:
                sname = s.get("stage","")
                if is_qc:
                    if sname not in QC_STAGE_NAMES_ALL:
                        continue
                else:
                    if not sname.startswith(stage_digit):
                        continue
                    # для основних етапiв — не тягнути QC-стейджi
                    if sname in QC_STAGE_NAMES_ALL:
                        continue
                if str(s.get("user_id","")) != str(target):
                    continue
                for k in ("photo", "photo1", "photo2"):
                    p = s.get(k)
                    if p: photos.append(p)
                v = s.get("video")
                if v: videos.append(v)
        total = len(photos) + len(videos)
        if total == 0:
            await q.answer(f"❌ Немає фото/вiдео", show_alert=True); return
        await q.answer(f"📤 Надсилаю {total}...")
        sent = 0
        # Спочатку фото — альбомами по 10
        for i in range(0, len(photos), 10):
            chunk = photos[i:i+10]
            try:
                if len(chunk) == 1:
                    await ctx.bot.send_photo(chat_id=q.message.chat_id, photo=chunk[0])
                else:
                    media = [InputMediaPhoto(media=fid) for fid in chunk]
                    await ctx.bot.send_media_group(chat_id=q.message.chat_id, media=media)
                sent += len(chunk)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"wwkcs photos chunk failed: {e}")
        # Потiм вiдео — по одному
        for v in videos:
            try:
                await ctx.bot.send_video(chat_id=q.message.chat_id, video=v)
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"wwkcs video failed: {e}")
        audit(uid, f"Надiслано медiа {stage_name}", f"{sent} шт.")

    elif data.startswith("wsh_"):
        # Пагінація історії робіт
        page = int(data[4:])
        role = get_role(uid)
        completed = {}
        for num, wdata in workshop_db.items():
            stages = wdata.get("stages",[])
            done_set = set(s.get("stage","") for s in stages) & set(WORKSHOP_STAGES)
            if len(done_set) >= len(WORKSHOP_STAGES):
                completed[num] = wdata
        items = []
        for num, wdata in completed.items():
            stages = wdata.get("stages",[])
            user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
            if user_stages:
                last = user_stages[-1]
                items.append(("✅ " + num + " | " + last.get("user","") + " | " + last.get("date","")[:10], "ws_" + num))
        await q.message.edit_reply_markup(reply_markup=make_paginated_inline(items, page, "wsh_", "завершених"))

    elif data.startswith("wss_"):
        parts = data[4:].rsplit("_", 1)
        num = parts[0]
        idx = int(parts[1]) if len(parts) > 1 else 0
        if num in workshop_db and idx < len(WORKSHOP_STAGES):
            stage_name = WORKSHOP_STAGES[idx]
            stages = workshop_db[num].get("stages",[])
            stage_data = next((s for s in stages if s.get("stage") == stage_name), None)
            if stage_data:
                caption = "🔧 " + stage_name + "\n👤 " + stage_data.get("user","") + "\n📅 " + stage_data.get("date","")
                if is_god(uid) or get_role(uid) == "admin":
                    edit_btns = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Замінити фото", callback_data="wsreplace_" + num + "_" + str(idx))],
                        [InlineKeyboardButton("🔙 Назад", callback_data="ws_" + num)]
                    ])
                else:
                    edit_btns = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Назад", callback_data="ws_" + num)]
                    ])
                if stage_data.get("video"):
                    # Показуємо список пунктів БЕЗ кнопок
                    cl_text = "\n".join(QUALITY_CHECKLIST)
                    await q.message.reply_text(caption + "\n\nПункти перевiрки:\n" + cl_text)
                    # Відео з кнопками знизу - як і фото в інших етапах
                    if is_god(uid) or get_role(uid) == "admin":
                        video_btns = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 Замiнити вiдео", callback_data="wsreplace_" + num + "_" + str(idx))],
                            [InlineKeyboardButton("🔙 Назад", callback_data="ws_" + num)]
                        ])
                    else:
                        video_btns = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 Назад", callback_data="ws_" + num)]
                        ])
                    try:
                        await q.message.reply_video(stage_data["video"], caption="📹 Вiдео контролю якостi", reply_markup=video_btns)
                    except Exception:
                        await q.message.reply_text("(вiдео недоступне)", reply_markup=video_btns)
                elif stage_data.get("photo2"):
                    try:
                        await q.message.reply_photo(stage_data.get("photo1"), caption=caption + "\n(фото 1/2)")
                        await q.message.reply_photo(stage_data["photo2"], caption="(фото 2/2)", reply_markup=edit_btns)
                    except Exception:
                        await q.message.reply_text(caption, reply_markup=edit_btns)
                elif stage_data.get("photo"):
                    try:
                        await q.message.reply_photo(stage_data["photo"], caption=caption, reply_markup=edit_btns)
                    except Exception:
                        await q.message.reply_text(caption + "\n(фото недоступне)", reply_markup=edit_btns)
                else:
                    await q.message.reply_text(caption, reply_markup=edit_btns)
            else:
                # Непройдений етап — перевіряємо чи це власник батареї
                owner_uid = str(stages[0].get("user_id","")) if stages else str(akb_db.get(num,{}).get("created_by_id",""))
                if str(uid) != owner_uid and not (is_god(uid) or get_role(uid) == "admin"):
                    await q.message.reply_text("❌ Тiльки власник батареї може проходити етапи")
                    return
                if is_god(uid) or get_role(uid) == "admin":
                    await q.message.reply_text("👁 Цей етап ще не пройдено")
                    return
                if has_perm(uid,"workshop"):
                    stages = workshop_db.get(num,{}).get("stages",[])
                    # Перевіряємо чи всі попередні етапи пройдені
                    prev_ok = True
                    for prev_idx in range(idx):
                        prev_stage = WORKSHOP_STAGES[prev_idx]
                        if not next((s for s in stages if s.get("stage") == prev_stage), None):
                            prev_ok = False
                            await q.message.reply_text(
                                "⚠️ Спочатку треба пройти:\n" + prev_stage)
                            break
                    if prev_ok:
                        # Контроль якості - показуємо чеклист
                        if stage_name == "6️⃣ Контроль якостi":
                            session.temp_data[uid] = {"bms_number": num, "akb_num": num}
                            session.waiting_for.pop(uid, None)
                            cl_text = "6️⃣ Контроль якостi батареї №" + num + "\n\nПеревiр кожен пункт перед здачею:\n\n" + "\n".join(QUALITY_CHECKLIST)
                            await q.message.reply_text(
                                cl_text,
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                    "✅ Ознайомився, знiмаю вiдео",
                                    callback_data="qcvideo_" + num)]]))
                        else:
                            needs_two = stage_name in WORKSHOP_TWO_PHOTOS
                            session.temp_data[uid] = {"bms_number": num, "akb_num": num, "stage": stage_name, "photo_num": 1}
                            session.waiting_for[uid] = "workshop_photo"
                            hint = "\n📸 Потрiбно 2 фото!\nНадiшли ПЕРШЕ фото" if needs_two else "\n📸 Надiшли фото"
                            await q.message.reply_text(
                                "Етап: " + stage_name + hint,
                                reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))
                else:
                    await q.message.reply_text("Етап ще не пройдено: " + stage_name)

    elif data.startswith("wsbatdel_"):
        if not is_god(uid):
            await q.answer("❌ Тiльки для God"); return
        num = data[9:]
        # Підтвердження
        await q.message.reply_text(
            "⚠️ Видалити батарею №" + num + " повнiстю?\nВсi фото та етапи будуть втраченi!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Так, видалити", callback_data="wsbatdelc_" + num)],
                [InlineKeyboardButton("❌ Скасувати", callback_data="ws_" + num)]
            ]))

    elif data.startswith("wsbatdelc_"):
        if not is_god(uid):
            await q.answer("❌ Тiльки для God"); return
        num = data[10:]
        if num in workshop_db:
            del workshop_db[num]
            save_json(WORKSHOP_FILE, workshop_db)
            audit(uid, "Видалено батарею з кабiнету: " + num)
            await q.message.edit_text("✅ Батарею №" + num + " видалено з робочого кабiнету!")

    elif data.startswith("wsrenum_"):
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Немає прав"); return
        num = data[8:]
        session.waiting_for[uid] = "ws_renum"
        session.temp_data[uid] = session.temp_data.get(uid, {})
        session.temp_data[uid]["old_num"] = num
        await q.message.reply_text(
            f"✏️ Поточний номер: {num}\nВведи новий номер БМС:",
            reply_markup=ReplyKeyboardMarkup([["❌ Скасувати"]], resize_keyboard=True))

    elif data.startswith("wsqc_"):
        # Контроль якостi у деталях АКБ — переглянути або додати вiдео
        num = data[5:]
        if num not in workshop_db:
            await q.answer("❌ АКБ не знайдено", show_alert=True); return
        stages = workshop_db[num].get("stages", [])
        qc_stage = next((s for s in stages if s.get("stage","") in QC_STAGE_NAMES_ALL), None)
        # Хто є власником АКБ — той хто завершив 5️⃣ Готовий АКБ (останнiй основний етап)
        owner_id = None
        for s in stages:
            if s.get("stage","") == "5️⃣ Готовий АКБ":
                owner_id = str(s.get("user_id",""))
                break
        # Якщо немає 5️⃣ — беремо хоч когось з основних етапiв
        if not owner_id:
            for s in stages:
                if s.get("stage","") in WORKSHOP_STAGES:
                    owner_id = str(s.get("user_id",""))
                    break
        if qc_stage:
            # Показуємо чеклiст + вiдео (видно всiм)
            cl_text = "\n".join(QUALITY_CHECKLIST)
            caption = "📹 Контроль якостi АКБ №" + num + "\n👤 " + qc_stage.get("user","") + "\n📅 " + qc_stage.get("date","")
            await q.message.reply_text(caption + "\n\nПункти перевiрки:\n" + cl_text)
            if is_god(uid) or get_role(uid) == "admin":
                video_btns = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Замiнити вiдео", callback_data="wsqcrep_" + num)],
                    [InlineKeyboardButton("🔙 Назад", callback_data="ws_" + num)]
                ])
            else:
                video_btns = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data="ws_" + num)]
                ])
            vid = qc_stage.get("video")
            if vid:
                try:
                    await q.message.reply_video(vid, caption="📹 Вiдео контролю якостi", reply_markup=video_btns)
                except Exception:
                    await q.message.reply_text("(вiдео недоступне)", reply_markup=video_btns)
            else:
                await q.message.reply_text("(вiдео вiдсутнє)", reply_markup=video_btns)
        else:
            # Контроль ще не додавали — додати може ЛИШЕ власник АКБ.
            # Адмiн/God бачить чеклiст, але не може додати вiдео за юзера.
            if str(uid) != str(owner_id):
                await q.answer()
                await q.message.reply_text(
                    "❌ Контроль якостi може додати тiльки власник АКБ.")
                return
            # Перевiрка що всi 5 основних етапiв пройдено
            done_set = set(s.get("stage","") for s in stages) & set(WORKSHOP_STAGES)
            if len(done_set) < len(WORKSHOP_STAGES):
                await q.answer()
                missing = len(WORKSHOP_STAGES) - len(done_set)
                await q.message.reply_text(
                    f"❌ Спочатку пройди всi {len(WORKSHOP_STAGES)} етапiв.\n"
                    f"Залишилось: {missing} етап(и).")
                return
            cl_text = "\n".join(QUALITY_CHECKLIST)
            await q.message.reply_text(
                "📹 Контроль якостi АКБ №" + num + "\n\nПункти перевiрки:\n" + cl_text +
                "\n\n📹 Надiшли вiдео огляду 30-60 сек")
            td = session.temp_data.get(uid, {})
            td.update({"akb_num": num, "bms_number": num,
                       "stage": QC_STAGE_NAME, "checklist": {}})
            session.temp_data[uid] = td
            session.waiting_for[uid] = "workshop_video"

    elif data.startswith("wsqcrep_"):
        # Замiнити вiдео Контролю якостi (тiльки адмiн/God)
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Немає прав"); return
        num = data[8:]
        if num in workshop_db:
            stages = workshop_db[num].get("stages", [])
            workshop_db[num]["stages"] = [s for s in stages if s.get("stage","") not in QC_STAGE_NAMES_ALL]
            save_json(WORKSHOP_FILE, workshop_db)
            audit(uid, "Замiна вiдео Контролю якостi АКБ " + num)
            td = session.temp_data.get(uid, {})
            td.update({"bms_number": num, "akb_num": num, "stage": QC_STAGE_NAME, "checklist": {}})
            session.temp_data[uid] = td
            session.waiting_for[uid] = "workshop_video"
            await q.message.reply_text(
                "🔄 Старе вiдео видалено.\n📹 Надiшли нове вiдео для Контролю якостi.",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))

    elif data.startswith("wsreplace_"):
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Немає прав"); return
        parts = data[10:].rsplit("_", 1)
        num = parts[0]
        idx = int(parts[1]) if len(parts) > 1 else 0
        if num in workshop_db and idx < len(WORKSHOP_STAGES):
            stage_name = WORKSHOP_STAGES[idx]
            stages = workshop_db[num].get("stages",[])
            workshop_db[num]["stages"] = [s for s in stages if not (s.get("stage") == stage_name and (s.get("user_id") == uid or s.get("user_id") == str(uid)))]
            save_json(WORKSHOP_FILE, workshop_db)
            audit(uid, "Замiна медiа " + stage_name + " АКБ " + num)
            # Якщо контроль якості - чекаємо відео
            if stage_name == "6️⃣ Контроль якостi":
                session.temp_data[uid] = {"bms_number": num, "akb_num": num, "stage": stage_name, "checklist": {}}
                session.waiting_for[uid] = "workshop_video"
                await q.message.reply_text(
                    "🔄 Старе вiдео видалено.\n📹 Надiшли нове вiдео для:\n" + stage_name,
                    reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))
            else:
                needs_two = stage_name in WORKSHOP_TWO_PHOTOS
                session.temp_data[uid] = {"bms_number": num, "akb_num": num, "stage": stage_name, "photo_num": 1}
                session.waiting_for[uid] = "workshop_photo"
                hint = "\n(потрiбно 2 фото)" if needs_two else ""
                await q.message.reply_text(
                    "🔄 Старе фото видалено.\nНадiшли нове фото для:\n" + stage_name + hint,
                    reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))

    elif data.startswith("qc_"):
        # Просто показуємо текстовий список — не потрібно
        pass

    elif data.startswith("qcvideo_"):
        num = data[8:]
        checklist = session.temp_data.get(uid, {}).get("checklist_" + num, {})
        session.temp_data[uid] = {"akb_num": num, "bms_number": num, 
                                   "stage": "6️⃣ Контроль якостi", "checklist": checklist}
        session.waiting_for[uid] = "workshop_video"
        await q.message.reply_text(
            "📹 Надiшли вiдео огляду батареї №" + num + "\n\n" +
            "⏱ Знiмай коротко — 30-60 сек\n" +
            "📋 Пройди по всiх 24 пунктах пiд час зйомки",
            reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))

    elif data.startswith("wsdel_"):
        # Видалення фото етапу (тільки адмін/God)
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Немає прав"); return
        parts = data[6:].rsplit("_", 1)
        num = parts[0]
        idx = int(parts[1]) if len(parts) > 1 else 0
        if num in workshop_db and idx < len(WORKSHOP_STAGES):
            stage_name = WORKSHOP_STAGES[idx]
            stages = workshop_db[num].get("stages",[])
            workshop_db[num]["stages"] = [s for s in stages if not (s.get("stage") == stage_name and (s.get("user_id") == uid or s.get("user_id") == str(uid)))]
            save_json(WORKSHOP_FILE, workshop_db)
            audit(uid, "Видалено фото етапу " + stage_name + " АКБ " + num)
            await q.message.edit_caption("🗑 Фото видалено: " + stage_name)

    elif data.startswith("akbh_"):
        num = data[5:]
        if num in akb_db:
            rec = akb_db[num]
            hist = rec.get("history",[])
            # Старий формат — показуємо як один запис
            if not hist:
                hist = [{
                    "date": rec.get("date",""),
                    "status": rec.get("status",""),
                    "comment": rec.get("comment",""),
                    "photos": rec.get("photos",[]),
                    "user": rec.get("user",""),
                }]
            t = f"📋 Iсторiя АКБ №{num} ({len(hist)} записiв):\n\n"
            await q.message.reply_text(t)
            for i, h in enumerate(hist[-10:], 1):
                icon = "✅" if h.get("status","")=="справний" else ("❌" if h.get("status","")=="бракований" else ("🔍" if h.get("status","")=="на перевiрцi" else "❓"))
                entry = f"{icon} Запис {i} | {h.get('date','')}\n📊 {h.get('status','')}\n💬 {h.get('comment','')}\n👤 {h.get('user','')}"
                await q.message.reply_text(entry)
                for photo in h.get("photos",[]):
                    try: await q.message.reply_photo(photo["file_id"])
                    except Exception: pass

    elif data.startswith("akbn_"):
        num = data[5:]
        session.waiting_for[uid] = "akb_status"
        session.temp_data[uid] = {"bms_number": num}
        await q.message.reply_text(f"➕ Новий запис для АКБ №{num}\n\nОберiть статус:", reply_markup=akb_status_kb())

    elif data == "akb_list":
        if not akb_db:
            await q.message.reply_text("🗄 База порожня")
        else:
            items = []
            for num,rec in list(akb_db.items()):
                hist = rec.get("history",[])
                if hist:
                    last = hist[-1]; status = last.get("status",""); date = last.get("date","")[:10]; count = len(hist)
                else:
                    status = rec.get("status",""); date = rec.get("date","")[:10]; count = len(rec.get("photos",[]))
                icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
                items.append((f"{icon} {num} | {date} | 📸{count}", f"akb_{num}"))
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["akb_list_items"] = items
            session.temp_data[uid]["akb_list_page"] = 0
            await q.message.reply_text(f"📋 Всi АКБ ({len(akb_db)}):", reply_markup=make_paginated_inline(items, 0, "akbl_", "АКБ"))

    elif data.startswith("akbl_"):
        # Пагінація списку всіх АКБ
        page = int(data[5:])
        items = session.temp_data.get(uid, {}).get("akb_list_items")
        if not items:
            # Перебудовуємо якщо втрачено (перезапуск боту)
            items = []
            for num,rec in list(akb_db.items()):
                hist = rec.get("history",[])
                if hist:
                    last = hist[-1]; status = last.get("status",""); date = last.get("date","")[:10]; count = len(hist)
                else:
                    status = rec.get("status",""); date = rec.get("date","")[:10]; count = len(rec.get("photos",[]))
                icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
                items.append((f"{icon} {num} | {date} | 📸{count}", f"akb_{num}"))
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["akb_list_items"] = items
        session.temp_data[uid]["akb_list_page"] = page
        await q.message.edit_reply_markup(reply_markup=make_paginated_inline(items, page, "akbl_", "АКБ"))

    elif data.startswith("akbdel_"):
        num = data[7:]
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Немає прав", show_alert=True); return
        if num not in akb_db:
            await q.answer("❌ АКБ не знайдено", show_alert=True); return
        await q.message.reply_text(
            f"⚠️ Видалити АКБ №{num}?\nЦе незворотньо!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Так, видалити", callback_data=f"akbdelc_{num}"),
                 InlineKeyboardButton("❌ Скасувати", callback_data=f"akb_{num}")]
            ]))

    elif data.startswith("akbdelc_"):
        num = data[8:]
        if not (is_god(uid) or get_role(uid) == "admin"):
            await q.answer("❌ Немає прав", show_alert=True); return
        if num in akb_db:
            del akb_db[num]
            save_json(DB_FILE, akb_db)
            audit(uid, f"Видалено АКБ {num}")
            await q.message.edit_text(f"✅ АКБ №{num} видалено")
        else:
            await q.message.edit_text("❌ АКБ не знайдено")
    elif data.startswith("tp_"):
        # tp_USERID_PERMISSION - розбиваємо правильно
        underscore_positions = [i for i,c in enumerate(data) if c=="_"]
        target_uid = data[underscore_positions[0]+1:underscore_positions[1]]
        action = data[underscore_positions[1]+1:]
        # Буфер змiн прав у пам'ятi сесiї — застосовується тiльки по Зберегти
        td = session.temp_data.get(uid, {})
        buf_key = "perm_buf_" + str(target_uid)
        role_buf_key = "role_buf_" + str(target_uid)
        user_now = users_db.get(str(target_uid), {})
        if buf_key not in td:
            cur_perms = user_now.get("permissions", []) if isinstance(user_now, dict) else []
            if "all" in cur_perms:
                td[buf_key] = list(ALL_PERMISSIONS)
            else:
                td[buf_key] = list(cur_perms)
        if role_buf_key not in td:
            td[role_buf_key] = user_now.get("role","user") if isinstance(user_now, dict) else "user"
        buf = td[buf_key]
        cur_role = td[role_buf_key]

        if action == "SAVE":
            if str(target_uid) in users_db and isinstance(users_db[str(target_uid)], dict):
                old_role = users_db[str(target_uid)].get("role","user")
                users_db[str(target_uid)]["permissions"] = list(buf)
                if cur_role in ("user","admin"):
                    users_db[str(target_uid)]["role"] = cur_role
                save_json(USERS_FILE, users_db)
                details = ", ".join(buf) if buf else "немає"
                if old_role != cur_role:
                    details += f" | роль: {old_role} → {cur_role}"
                audit(uid, "Збережено права для " + str(target_uid), details)
            td.pop(buf_key, None)
            td.pop(role_buf_key, None)
            session.temp_data[uid] = td
            await q.message.edit_reply_markup(reply_markup=None)
            await q.message.reply_text(f"✅ Права збережено для {target_uid}"); return

        if action == "ALL":
            buf[:] = list(ALL_PERMISSIONS)
            session.temp_data[uid] = td
            await q.message.edit_reply_markup(reply_markup=user_perms_inline_kb_buf(int(target_uid), buf, cur_role)); return

        if action == "ROLE":
            # Показуємо меню вибору ролi (только God)
            if not is_god(uid):
                await q.answer("❌ Тiльки God"); return
            role_btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 user", callback_data=f"tp_{target_uid}_R_user"),
                 InlineKeyboardButton("🛡 admin", callback_data=f"tp_{target_uid}_R_admin")],
                [InlineKeyboardButton("🔙 Назад", callback_data=f"tp_{target_uid}_BACK")]
            ])
            await q.message.edit_reply_markup(reply_markup=role_btns); return

        if action == "BACK":
            await q.message.edit_reply_markup(reply_markup=user_perms_inline_kb_buf(int(target_uid), buf, cur_role)); return

        if action in ("R_user", "R_admin"):
            if not is_god(uid):
                await q.answer("❌ Тiльки God"); return
            new_role = "user" if action == "R_user" else "admin"
            td[role_buf_key] = new_role
            session.temp_data[uid] = td
            await q.message.edit_reply_markup(reply_markup=user_perms_inline_kb_buf(int(target_uid), buf, new_role)); return

        # Toggle одного права
        if action in buf:
            buf.remove(action)
        elif action in ALL_PERMISSIONS:
            buf.append(action)
        session.temp_data[uid] = td
        await q.message.edit_reply_markup(reply_markup=user_perms_inline_kb_buf(int(target_uid), buf, cur_role))

# ── ХЕНДЛЕРИ ─────────────────────────────────────────────────
def get_hint(btn):
    hints = {
        "📟 BMS Керування": "Пiдключення та налаштування BMS",
        "📂 База АКБ": "Облiк батарей з фото та iсторiєю",
        "🔉 Алерти": "Авто-сповiщення про проблеми",
        "📉 Логи": "Iсторiя твоїх дiй",
        "🪪 Робочий кабiнет": "Фото етапiв збiрки батарей",
        "👥 Працівники": "Перегляд робiт команди",
        "⚙️ Налаштування": "Керування системою (тiльки God)",
        "🔍 Знайти BMS": "Сканувати та пiдключитись до BMS",
        "📈 Стан батареї": "Напруга, SOC, температура",
        "🔬 Ячейки": "Напруга кожної ячейки",
        "🌡 Температура BMS": "Температура датчикiв",
        "📋 Пресети": "Налаштування параметрiв BMS",
        "🏭 Заводськi параметри — записати": "Записати всi заводськi параметри в BMS",
        "💻 CMD центр": "Керування ноутбуком через Telegram",
    }
    return hints.get(btn, "")

async def cmd_start(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    # Зберігаємо ім'я з Telegram
    tg_name = update.effective_user.first_name or str(uid)
    if str(uid) in users_db and isinstance(users_db[str(uid)], dict):
        if not users_db[str(uid)].get("name") or users_db[str(uid)].get("name") == str(uid):
            users_db[str(uid)]["name"] = tg_name
            save_json(USERS_FILE, users_db)
    if not u and not is_god(uid):
        await update.message.reply_text("❌ Доступ заборонено\nЗверниться до адмiна")
        # Сповіщаємо God
        try:
            tg_username = "@" + update.effective_user.username if update.effective_user.username else "немає"
            await ctx.bot.send_message(
                chat_id=GOD_ID,
                text=f"🔔 Запит доступу до бота\n👤 {tg_name}\n🆔 ID: {uid}\n✈️ Username: {tg_username}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Додати як User", callback_data=f"addu_{uid}_user"),
                     InlineKeyboardButton("🛡 Додати як Admin", callback_data=f"addu_{uid}_admin")],
                    [InlineKeyboardButton("❌ Відмовити", callback_data=f"addu_{uid}_deny")]
                ]))
        except Exception: pass
        return
    role = get_role(uid)
    audit(uid, "Вхiд в бота")
    await update.message.reply_text(
        f"🔋 VoltForge BMS v15.1\n{role_icon(role)} {tg_name} | {role}\n\nОберiть роздiл:",
        reply_markup=main_kb(uid))

async def cmd_scan(update, ctx):
    uid = update.effective_user.id
    if not has_perm(uid,"bms") and not is_god(uid):
        await update.message.reply_text("❌ Немає прав"); return
    msg = await update.message.reply_text("🔍 Сканую Bluetooth... (8 сек)")
    try:
        devs = await scan_bms()
        if not devs:
            await msg.edit_text("❌ BMS не знайдено\n\n- BMS увiмкнена?\n- Bluetooth увiмкнений?")
            return
        state.scan_results = devs
        if len(devs) >= 1:
            text = f"✅ Знайдено {len(devs)} BMS:\n\nОберi до якої пiдключитись:\n"
            btns = []
            for i,d in enumerate(devs):
                text += f"\n{i+1}. {d.name or 'JK BMS'}\n📍 {d.address}\n"
                btns.append([f"🔗 {d.name or 'BMS'} | {d.address}"])
            btns.append(["🔙 Назад"])
            session.waiting_for[uid] = "select_bms"
            await msg.edit_text(text)
            await update.message.reply_text("👇 Обери BMS:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
    except Exception as e:
        await msg.edit_text(f"❌ Помилка сканування: {e}")

async def handle_photo(update, ctx):
    uid = update.effective_user.id
    # Перевіряємо відео
    if update.message and (update.message.video or update.message.video_note):
        action = session.waiting_for.get(uid)
        # Якщо очікується фото - відео не приймаємо
        if action in ("workshop_photo", "workshop_confirm"):
            await update.message.reply_text("❌ Потрiбне ФОТО, а не вiдео!")
            return
        # Вже отримали відео - друге не приймаємо
        if action == "ai_chat":
            if text in ["🔙 CMD центр", "🔙 Назад"]:
                session.waiting_for.pop(uid, None)
                await update.message.reply_text("💻 CMD центр:", reply_markup=cmd_center_kb())
                return
            if not text:
                await update.message.reply_text("❌ Надiшли текстове питання, не вiдео!")
                return
            # Надсилаємо запит до Gemini
            await update.message.reply_text("🧠 Думаю...")
            import aiohttp, json as json_lib
            try:
                payload = {
                    "contents": [{"parts": [{"text": text}]}],
                    "systemInstruction": {"parts": [{"text": "Ти корисний універсальний асистент. Відповідай на будь-які питання — технічні, творчі, побутові, наукові, BMS, батареї і все інше. Відповідай коротко і по суті на мові користувача."}]}
                }
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    ) as resp:
                        data = await resp.json()
                        if "candidates" in data and data["candidates"]:
                            reply = data["candidates"][0]["content"]["parts"][0]["text"]
                        elif "error" in data:
                            reply = f"Помилка API: {data['error'].get('message','невідома')}"
                        else:
                            reply = f"Невідома відповідь: {str(data)[:200]}"
                        await update.message.reply_text(
                            f"🧠 {reply[:4000]}",
                            reply_markup=ReplyKeyboardMarkup([["🔙 CMD центр"]], resize_keyboard=True))
            except Exception as e:
                await update.message.reply_text(f"❌ Помилка AI: {str(e)[:200]}")
            return

        if action == "diag_find_addr_val":
            if text == "🔙 Назад до дiагностики":
                session.waiting_for.pop(uid, None)
                await update.message.reply_text("🔬 Дiагностика BMS", reply_markup=diag_kb())
                return
            try:
                target = int(text.strip())
            except:
                await update.message.reply_text("❌ Введи цiле число! Наприклад: 1234")
                return
            session.waiting_for.pop(uid, None)
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ BMS не пiдключена!")
                return
            msg = await update.message.reply_text(f"🔎 Шукаю адресу для {target}...\n⏳ Перебираю 0x00-0x50 (~2 хв)")
            import struct
            hdr = bytes([0xAA,0x55,0x90,0xEB])
            found = []
            # Зчитуємо початкові параметри - надсилаємо CMD_DEVICE_INFO і чекаємо пакет 01
            init_buf = bytearray()
            def init_h(_, data): init_buf.extend(data)
            await state.client.start_notify(JK_CHAR_UUID, init_h)
            cmd_info = bytes.fromhex("AA5590EB97000000000000000000000000000011")
            await state.client.write_gatt_char(JK_CHAR_UUID, cmd_info, response=True)
            await asyncio.sleep(1.5)
            try: await state.client.stop_notify(JK_CHAR_UUID)
            except: pass
            # Парсимо початкові параметри
            raw0 = bytes(init_buf)
            idx0 = raw0.find(bytes([0x55,0xAA,0xEB,0x90,0x01]))
            before = {}
            if idx0 != -1:
                pkt0 = raw0[idx0+5:]
                def u16b(off):
                    if off+2 <= len(pkt0): return struct.unpack('<H', pkt0[off:off+2])[0]
                    return 0
                before = {
                    "ovpr": u16b(0x001)/1000, "uvp": u16b(0x005)/1000,
                    "uvpr": u16b(0x009)/1000, "ovp": u16b(0x00D)/1000,
                    "soc100": u16b(0x019)/1000, "soc0": u16b(0x01D)/1000,
                    "rcv_volt": u16b(0x021)/1000, "rfv_volt": u16b(0x025)/1000,
                    "pwr_off": u16b(0x029)/1000,
                }
            
            for addr in range(0x00, 0x51):
                try:
                    vb = struct.pack("<I", target)
                    pl = bytes([addr, 0x04]) + vb + bytes(9)
                    cmd = hdr + pl + bytes([sum(hdr+pl) & 0xFF])
                    # Зчитуємо після запису - з коротким буфером
                    resp_buf = bytearray()
                    def resp_h(_, data): resp_buf.extend(data)
                    await state.client.start_notify(JK_CHAR_UUID, resp_h)
                    await state.client.write_gatt_char(JK_CHAR_UUID, cmd, response=True)
                    await asyncio.sleep(0.2)
                    # Запитуємо Device Info щоб отримати пакет 01
                    cmd_info = bytes.fromhex("AA5590EB97000000000000000000000000000011")
                    await state.client.write_gatt_char(JK_CHAR_UUID, cmd_info, response=True)
                    await asyncio.sleep(0.8)
                    try: await state.client.stop_notify(JK_CHAR_UUID)
                    except: pass
                    # Парсимо пакет 01 з відповіді
                    raw = bytes(resp_buf)
                    idx01 = raw.find(bytes([0x55,0xAA,0xEB,0x90,0x01]))
                    if idx01 != -1:
                        pkt = raw[idx01+5:]
                        def u16(off):
                            if off+2 <= len(pkt): return struct.unpack('<H', pkt[off:off+2])[0]
                            return 0
                        after = {
                            "ovpr": u16(0x001)/1000, "uvp": u16(0x005)/1000,
                            "uvpr": u16(0x009)/1000, "ovp": u16(0x00D)/1000,
                            "soc100": u16(0x019)/1000, "soc0": u16(0x01D)/1000,
                            "rcv_volt": u16(0x021)/1000, "rfv_volt": u16(0x025)/1000,
                            "pwr_off": u16(0x029)/1000,
                        }
                        for key, val in after.items():
                            before_val = before.get(key, 0)
                            if abs(val - target/1000) < 0.005 and abs(before_val - target/1000) > 0.005:
                                found.append(f"0x{addr:02X} = {key} (було {before_val:.3f} → стало {val:.3f})")
                                before[key] = val
                except Exception:
                    pass
            out = f"🔎 Результат для {target}:\n"
            if found:
                out += "\n".join(found)
            else:
                out += "❌ Не знайдено — спробуй iнше значення"
            try:
                await msg.edit_text(out, reply_markup=diag_tools_kb())
            except Exception:
                await update.message.reply_text(out, reply_markup=diag_tools_kb())
            return

        if action == "diag_hex_input":
            if text == "🔙 Назад до дiагностики":
                session.waiting_for.pop(uid, None)
                await update.message.reply_text("🔬 Дiагностика BMS", reply_markup=diag_kb())
                return
            hex_str = text.strip().replace(" ","").replace(":","")
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ BMS не пiдключена!")
                session.waiting_for.pop(uid, None)
                return
            # Визначаємо яку характеристику використовувати
            hex_char = session.temp_data.get(uid, {}).get("hex_char", "FFE1")
            write_uuid = JK_CHAR_UUID2 if hex_char == "FFE2" else JK_CHAR_UUID
            try:
                cmd = bytes.fromhex(hex_str)
                resp_buf = bytearray()
                def resp_handler(_, data): resp_buf.extend(data)
                await state.client.start_notify(JK_CHAR_UUID, resp_handler)
                await state.client.write_gatt_char(write_uuid, cmd, response=True)
                await asyncio.sleep(2.0)
                try: await state.client.stop_notify(JK_CHAR_UUID)
                except: pass
                session.waiting_for.pop(uid, None)
                out = f"📤 Вiдправлено [{hex_char}]: {cmd.hex().upper()}\n"
                out += f"📥 Вiдповiдь ({len(resp_buf)} байт):\n"
                if resp_buf:
                    for row in range(0, min(len(resp_buf), 64), 16):
                        chunk = resp_buf[row:row+16]
                        out += f"[{row:03d}] {' '.join('%02X'%b for b in chunk)}\n"
                else:
                    out += "Немає вiдповiдi"
                await update.message.reply_text(out, reply_markup=diag_network_kb())
            except ValueError:
                await update.message.reply_text("❌ Невiрний HEX формат!")
            return

        if action == "workshop_confirm_video":
            await update.message.reply_text("❌ Вiдео вже отримано!\nНатисни ✅ Зберегти або ❌ Скасувати.")
            return
        if action == "workshop_video":
            video_id = update.message.video.file_id if update.message.video else update.message.video_note.file_id
            akb_num = session.temp_data.get(uid, {}).get("akb_num","?")
            checklist = session.temp_data.get(uid, {}).get("checklist", {})
            stage = session.temp_data.get(uid, {}).get("stage", "6️⃣ Контроль якостi")
            if uid not in session.temp_data:
                session.temp_data[uid] = {}
            session.temp_data[uid]["pending_video"] = video_id
            session.temp_data[uid]["checklist"] = checklist
            session.temp_data[uid]["stage"] = stage
            session.temp_data[uid]["akb_num"] = akb_num
            session.waiting_for[uid] = "workshop_confirm_video"
            await update.message.reply_text(
                "📹 Зберегти вiдео для: " + stage + "\nАКБ №" + str(akb_num) + "?",
                reply_markup=ReplyKeyboardMarkup([["✅ Зберегти","❌ Скасувати"]], resize_keyboard=True))
        else:
            # Відео надіслано без активної сесії
            await update.message.reply_text(
                "📹 Вiдео отримано, але зараз не очiкується.\n"
                "Зайди в потрiбний етап i натисни кнопку для вiдео.")
        return
    action = session.waiting_for.get(uid)
    if action == "akb_photo":
        file_id = update.message.photo[-1].file_id
        num = session.temp_data[uid].get("bms_number","?")
        if num not in akb_db:
            akb_db[num] = {"bms_number":num,"date":datetime.now().strftime("%d.%m.%Y %H:%M"),
                           "status":session.temp_data[uid].get("status",""),
                           "comment":session.temp_data[uid].get("comment",""),
                           "created_by":get_name(uid),"created_by_id":uid,"history":[]}
        # Додаємо в історію
        akb_db[num]["history"].append({
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "status": session.temp_data[uid].get("status",""),
            "comment": session.temp_data[uid].get("comment",""),
            "photos": [{"file_id":file_id,"date":datetime.now().strftime("%d.%m.%Y %H:%M")}],
            "user": get_name(uid),
            "user_id": uid
        })
        save_json(DB_FILE, akb_db)
        audit(uid, f"Додано АКБ {num}", session.temp_data[uid].get("status",""))
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        await update.message.reply_text(
            f"✅ АКБ збережено!\n📟 {num}\n📅 {akb_db[num]['history'][-1]['date']}\n📊 {akb_db[num]['history'][-1]['status']}",
            reply_markup=akb_kb(uid))
    elif action == "workshop_photo":
        if not update.message.photo:
            await update.message.reply_text("❌ Потрiбне ФОТО, а не вiдео!")
            return
        file_id = update.message.photo[-1].file_id
        akb_num = session.temp_data[uid].get("akb_num")
        stage = session.temp_data[uid].get("stage")
        photo_num = session.temp_data[uid].get("photo_num", 1)
        needs_two = stage in WORKSHOP_TWO_PHOTOS
        # Блокуємо якщо вже є підтвердження в процесі
        if session.temp_data[uid].get("processing_photo"):
            await update.message.reply_text("❌ Зачекай! Попереднє фото ще обробляється.")
            return
        session.temp_data[uid]["processing_photo"] = True
        if needs_two and photo_num == 1:
            # Перше фото з двох - зберігаємо і одразу просимо друге БЕЗ підтвердження
            session.temp_data[uid]["photo1"] = file_id
            session.temp_data[uid]["photo_num"] = 2
            session.temp_data[uid].pop("processing_photo", None)
            await update.message.reply_photo(
                file_id,
                caption="✅ Фото 1/2 отримано!\n\n📸 Надiшли друге фото для:\n" + stage)
        else:
            # Одне фото або друге з двох - питаємо підтвердження
            session.temp_data[uid]["pending_photo"] = file_id
            session.temp_data[uid].pop("processing_photo", None)
            session.waiting_for[uid] = "workshop_confirm"
            if needs_two and photo_num == 2:
                caption = "📸 Зберегти обидва фото для:\n" + stage + "?\n\nℹ️ Пiсля збереження редагування доступне тiльки адмiнiстратору"
            else:
                caption = "📸 Зберегти фото для:\n" + stage + "?\n\nℹ️ Пiсля збереження редагування доступне тiльки адмiнiстратору"
            await update.message.reply_photo(
                file_id,
                caption=caption,
                reply_markup=ReplyKeyboardMarkup([["✅ Зберегти","❌ Скасувати"]], resize_keyboard=True))

    elif action == "workshop_video":
        # Обробка відео для контролю якості
        if update.message.video:
            video_id = update.message.video.file_id
        elif update.message.video_note:
            video_id = update.message.video_note.file_id
        else:
            await update.message.reply_text("❌ Надiшли вiдео файл")
            return
        akb_num = session.temp_data[uid].get("akb_num")
        stage = session.temp_data[uid].get("stage")
        checklist = session.temp_data[uid].get("checklist", {})
        session.temp_data[uid]["pending_video"] = video_id
        session.waiting_for[uid] = "workshop_confirm_video"
        await update.message.reply_text(
            "📹 Вiдео отримано!\nЗберегти для Контролю якостi батареї №" + akb_num + "?",
            reply_markup=ReplyKeyboardMarkup([["✅ Зберегти","❌ Скасувати"]], resize_keyboard=True))

async def handle_text(update, ctx):
    uid = update.effective_user.id; text = update.message.text
    u = get_user(uid)
    if not u and not is_god(uid):
        await update.message.reply_text("❌ Доступ заборонено"); return

    # Логуємо кожне натискання кнопки
    if text and not text.startswith('/'):
        audit(uid, "Кнопка: " + text[:50])

    # Глобальна обробка кнопок Назад - скидає будь-яку сесію введення
    back_buttons = ["🔙 Назад","🔙 Назад до АКБ","🔙 Назад до налаштувань","🔙 CMD центр"]
    if text in back_buttons and uid in session.waiting_for:
        action_now = session.waiting_for.get(uid,"")
        # Не скидаємо сесії підтвердження і пароля - вони мають свою логіку
        if action_now not in ["confirm_action","cmd_password_first","cmd_password_check"]:
            session.waiting_for.pop(uid,None)
            session.temp_data.pop(uid,None)

    # ── СЕСІЇ ──
    if uid in session.waiting_for:
        action = session.waiting_for[uid]

        if action == "workshop_confirm":
            if text == "✅ Зберегти":
                akb_num = session.temp_data.get(uid, {}).get("akb_num")
                stage = session.temp_data.get(uid, {}).get("stage")
                file_id = session.temp_data.get(uid, {}).get("pending_photo")
                photo_num = session.temp_data.get(uid, {}).get("photo_num", 1)
                needs_two = stage in WORKSHOP_TWO_PHOTOS if stage else False
                session.waiting_for.pop(uid, None)
                if akb_num and stage and file_id:
                    if akb_num not in workshop_db:
                        workshop_db[akb_num] = {"akb_num": akb_num, "stages": []}
                    stage_data = {
                        "stage": stage,
                        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
                        "user": get_name(uid),
                        "user_id": uid,
                        "photo": file_id
                    }
                    if needs_two and photo_num == 2:
                        stage_data["photo1"] = session.temp_data.get(uid, {}).get("photo1")
                        stage_data["photo2"] = file_id
                    # Якщо етап "Звіт опорів четвірки" - питаємо про копіювання
                    # НЕ зберігаємо в базу до підтвердження юзера
                    if stage == "3️⃣ Звiт опорiв четвiрки":
                        active_others = []
                        stage_1 = WORKSHOP_STAGES[0]
                        stage_2 = WORKSHOP_STAGES[1]
                        stage_3 = WORKSHOP_STAGES[2]
                        for num, wdata in workshop_db.items():
                            if num == akb_num: continue
                            num_stages = [s for s in wdata.get("stages",[]) if str(s.get("user_id")) == str(uid)]
                            stage_names = [s.get("stage") for s in num_stages]
                            if stage_1 in stage_names and stage_2 in stage_names and stage_3 not in stage_names:
                                active_others.append(num)
                        if active_others:
                            session.temp_data[uid] = {
                                "copy_photo_file": file_id,
                                "copy_photo_stage": stage,
                                "copy_photo_stage_data": stage_data,
                                "copy_photo_candidates": active_others,
                                "copy_photo_selected": [],
                                "copy_photo_from": akb_num,
                            }
                            session.waiting_for[uid] = "copy_photo_select"
                            btns = [[InlineKeyboardButton(f"⬜ АКБ {n}", callback_data=f"cps_{n}")] for n in active_others]
                            btns.append([InlineKeyboardButton("✅ Пiдтвердити (0/3)", callback_data="cps_confirm"),
                                         InlineKeyboardButton("✳️ Тiльки для " + akb_num, callback_data="cps_skip")])
                            await update.message.reply_text(
                                f"📸 Фото для АКБ {akb_num}\n\n"
                                f"Для проходження 3го етапу оберiть 3 батареї у яких пройдено 1й i 2й етап.\n"
                                f"Або збережи тiльки для цiєї батареї ✳️",
                                reply_markup=InlineKeyboardMarkup(btns))
                            await update.message.reply_text(
                                "↩️ Щоб скасувати — натисни кнопку нижче:",
                                reply_markup=ReplyKeyboardMarkup([["❌ Скасувати"]], resize_keyboard=True))
                            return
                        else:
                            # Немає батарей для вибору — не зберігаємо ще, чекаємо ✳️
                            session.temp_data[uid] = {
                                "copy_photo_file": file_id,
                                "copy_photo_stage": stage,
                                "copy_photo_stage_data": stage_data,
                                "copy_photo_candidates": [],
                                "copy_photo_selected": [],
                                "copy_photo_from": akb_num,
                            }
                            session.waiting_for[uid] = "copy_photo_select"
                            btns = [[InlineKeyboardButton("✳️ Тiльки для " + akb_num, callback_data="cps_skip")]]
                            await update.message.reply_text(
                                f"📸 Фото для АКБ {akb_num}\n\n"
                                f"⚠️ Немає батарей для вибору.\n"
                                f"Iншi батареї ще не пройшли 1й i 2й етап.\n"
                                f"Збережи для цiєї батареї ✳️ або скасуй:",
                                reply_markup=InlineKeyboardMarkup(btns))
                            await update.message.reply_text(
                                "↩️ Щоб скасувати — натисни кнопку нижче:",
                                reply_markup=ReplyKeyboardMarkup([["❌ Скасувати"]], resize_keyboard=True))
                            return
                    else:
                        # Всі інші етапи — зберігаємо одразу
                        workshop_db[akb_num]["stages"].append(stage_data)
                        save_json(WORKSHOP_FILE, workshop_db)
                        audit(uid, "Робочий кабiнет АКБ " + akb_num + " " + stage)
                    user_stages = [s for s in workshop_db[akb_num]["stages"] if str(s.get("user_id")) == str(uid)]
                    session.temp_data.pop(uid, None)
                    next_idx = len(user_stages)
                    if next_idx >= len(WORKSHOP_STAGES):
                        await update.message.reply_text(
                            "✅ Збережено!\n🎉 Всi " + str(len(WORKSHOP_STAGES)) + " етапiв завершено!\n📟 АКБ: " + akb_num,
                            reply_markup=workshop_kb(uid))
                        # Сповіщення всім адмінам
                        for a_id, a_info in users_db.items():
                            a_role = a_info.get("role") if isinstance(a_info, dict) else a_info
                            if a_role == "admin":
                                try:
                                    await ctx.bot.send_message(
                                        chat_id=int(a_id),
                                        text="🏭 Батарея завершена!\n👤 " + get_name(uid) + "\n📟 №" + akb_num + "\n⏱ " + datetime.now().strftime("%d.%m.%Y %H:%M")
                                    )
                                except: pass
                    else:
                        next_stage = WORKSHOP_STAGES[next_idx]
                        needs_two_next = next_stage in WORKSHOP_TWO_PHOTOS
                        hint = "\n📸 Потрiбно 2 фото!" if needs_two_next else ""
                        session.temp_data[uid] = {"bms_number": akb_num, "akb_num": akb_num}
                        await update.message.reply_text(
                            "✅ Збережено!\n📟 АКБ: " + akb_num + "\nНаступний етап:\n" + next_stage + hint,
                            reply_markup=ReplyKeyboardMarkup([[next_stage],["🔙 Назад до кабiнету"]], resize_keyboard=True))
            else:
                session.waiting_for.pop(uid, None)
                session.temp_data.pop(uid, None)
                await update.message.reply_text("Скасовано", reply_markup=workshop_kb(uid))
            return

        if action == "diag_search":
            session.waiting_for.pop(uid,None)
            try: search_val = int(text.strip())
            except ValueError: await update.message.reply_text("❌ Введи цiле число"); return
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Не пiдключено"); return
            msg = await update.message.reply_text("⏳ Шукаю %d..." % search_val)
            try:
                buf = bytearray()
                def sh(_, d): buf.extend(d)
                await state.client.start_notify(JK_CHAR_UUID, sh)
                buf.clear()
                await state.client.write_gatt_char(JK_CHAR_UUID, CMD_DEVICE_INFO, response=True)
                await asyncio.sleep(1.0); buf.clear()
                await state.client.write_gatt_char(JK_CHAR_UUID, CMD_CELL_INFO, response=True)
                await asyncio.sleep(2.0)
                try: await state.client.stop_notify(JK_CHAR_UUID)
                except Exception: pass
                data = bytes(buf); best = data; best_size = 0; ix = 0
                while ix < len(data) - 4:
                    if data[ix:ix+4] == bytes([0x55,0xAA,0xEB,0x90]):
                        nx = data.find(bytes([0x55,0xAA,0xEB,0x90]), ix+4)
                        fr = data[ix:nx] if nx > 0 else data[ix:]
                        if len(fr)>4 and fr[4]==0x02 and len(fr)>best_size:
                            best = fr; best_size = len(fr)
                        ix = nx if nx > 0 else len(data)
                    else: ix += 1
                found = []
                for i in range(len(best)-1):
                    v1 = best[i]; v2 = (best[i+1]<<8)|best[i]
                    if v1 == search_val: found.append("off=%d: uint8=%d" % (i, v1))
                    elif v2 == search_val: found.append("off=%d: uint16=%d" % (i, v2))
                out = "📍 Пошук %d (%d б):\n\n" % (search_val, len(best))
                out += "\n".join(found[:20]) if found else "❌ Не знайдено"
                await msg.edit_text(out[:4000])
            except Exception as e: await msg.edit_text("❌ " + str(e))
            return

        if action == "ws_stage":
            stage_name = text.strip()
            if stage_name not in WORKSHOP_STAGES:
                bms_num = session.temp_data[uid].get("bms_number","?")
                next_idx = len([s for s in workshop_db.get(bms_num,{}).get("stages",[]) if str(s.get("user_id")) == str(uid)])
                if next_idx < len(WORKSHOP_STAGES):
                    next_stage = WORKSHOP_STAGES[next_idx]
                    await update.message.reply_text(
                        "⚠️ Натисни кнопку етапу:",
                        reply_markup=ReplyKeyboardMarkup([[next_stage], ["🔙 Назад до кабiнету"]], resize_keyboard=True))
                return
            session.waiting_for[uid] = "workshop_photo"
            bms_num = session.temp_data[uid].get("bms_number","?")
            session.temp_data[uid]["akb_num"] = bms_num
            session.temp_data[uid]["stage"] = stage_name
            session.temp_data[uid]["photo_num"] = 1
            needs_two = stage_name in WORKSHOP_TWO_PHOTOS
            hint = "\n📸 Потрiбно 2 фото!\nНадiшли ПЕРШЕ фото" if needs_two else "\n📸 Надiшли фото"
            await update.message.reply_text(
                "Етап: " + stage_name + hint,
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))
            return

        if action == "set_password":
            new_pass = text.strip()
            cmd_pass_cfg["password"] = new_pass
            session.waiting_for[uid] = "set_hint"
            save_json(CMD_PASS_FILE, cmd_pass_cfg)
            await update.message.reply_text("✅ Пароль встановлено!\nВведи пiдказку (або '-' щоб пропустити):")
            return

        if action == "set_hint":
            if text.strip() != "-":
                cmd_pass_cfg["hint"] = text.strip()
                save_json(CMD_PASS_FILE, cmd_pass_cfg)
            session.waiting_for.pop(uid, None)
            await update.message.reply_text("✅ Пароль i пiдказка збереженi!", reply_markup=cmd_center_kb())
            return

        if action == "open_website":
            session.waiting_for.pop(uid,None)
            url = text.strip()
            if not url.startswith("http"): url = "https://" + url
            try:
                subprocess.run("start chrome " + url, shell=True, timeout=5)
                audit(uid, "CMD: Відкрито " + url)
                await update.message.reply_text("✅ Відкрито: " + url, reply_markup=cmd_browser_kb())
            except Exception as e: await update.message.reply_text("❌ " + str(e))
            return

        if action == "confirm_action":
            pass  # Обробляється через кнопки ✅/❌

        if action == "cmd_password_first":
            # Перевіряємо Назад спочатку
            if text in ["🔙 Назад","🔙 CMD центр","🔙 Назад до налаштувань"]:
                session.waiting_for.pop(uid, None)
                session.temp_data.pop(uid, None)
                await update.message.reply_text("💻 CMD центр:", reply_markup=cmd_center_kb())
                return
            after = session.temp_data.get(uid, {}).get("after", "cmd_menu")
            if text.strip() == cmd_pass_cfg.get("password"):
                # Правильний пароль - закриваємо сесію
                session.waiting_for.pop(uid, None)
                if uid not in cmd_pass_cfg["verified"]:
                    cmd_pass_cfg["verified"].append(uid)
                if after == "cmd_menu":
                    await update.message.reply_text("✅ Доступ дозволено!", reply_markup=cmd_center_kb())
                elif after == "cmd_command":
                    session.waiting_for[uid] = "cmd_command"
                    await update.message.reply_text("✅ Введи команду:")
                else:
                    session.waiting_for[uid] = after
                    await update.message.reply_text("✅ Пароль вiрний! Пiдтверди дiю:")
            else:
                # Неправильний пароль - НЕ закриваємо сесію, питаємо знову
                hint = cmd_pass_cfg.get("hint","")
                hint_text = "\n💡 Пiдказка: " + hint if hint else ""
                await update.message.reply_text("❌ Невiрний пароль! Спробуй ще раз:" + hint_text)
            return

        if action == "cmd_command":
            # Якщо натиснули Назад - не виконуємо як команду
            if text in ["🔙 Назад","🔙 CMD центр","🔙 Назад до налаштувань"]:
                session.waiting_for.pop(uid,None)
                await update.message.reply_text("💻 CMD центр:", reply_markup=cmd_center_kb())
                return
            session.waiting_for.pop(uid,None)
            if not is_god(uid): await update.message.reply_text("❌ Тiльки God"); return
            # Перевіряємо пароль якщо встановлений
            if cmd_pass_cfg.get("password") and uid not in cmd_pass_cfg.get("verified", []):
                session.waiting_for[uid] = "cmd_password_check"
                session.temp_data[uid] = {"pending_cmd": text}
                await update.message.reply_text("🔐 Введи пароль CMD:")
                return
            msg = await update.message.reply_text("⏳ Виконую...")
            try:
                result = subprocess.run(
                    text,
                    shell=True, capture_output=True,
                    timeout=10)
                out_bytes = result.stdout or result.stderr or b""
                try:
                    result = type('obj', (object,), {'stdout': out_bytes.decode('cp866'), 'stderr': ''})()
                except Exception:
                    try:
                        result = type('obj', (object,), {'stdout': out_bytes.decode('cp1251'), 'stderr': ''})()
                    except Exception:
                        result = type('obj', (object,), {'stdout': out_bytes.decode('utf-8', errors='replace'), 'stderr': ''})()
                out = result.stdout or "Виконано (нема виводу)"
                audit(uid, f"CMD: {text}")
                await msg.edit_text(f"💻 CMD:\n```\n{out[:3500]}\n```", parse_mode="Markdown")
            except subprocess.TimeoutExpired: await msg.edit_text("❌ Таймаут (10 сек)")
            except Exception as e: await msg.edit_text(f"❌ {e}")
            return

        if action == "cmd_password_check":
            pending = session.temp_data[uid].get("pending_cmd","")
            if text.strip() == cmd_pass_cfg.get("password"):
                session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
                if uid not in cmd_pass_cfg["verified"]:
                    cmd_pass_cfg["verified"].append(uid)
                msg = await update.message.reply_text("✅ Пароль вiрний!\n⏳ Виконую...")
                try:
                    result = subprocess.run(
                        "chcp 65001 > nul && " + pending,
                        shell=True, capture_output=True, text=True,
                        timeout=10, encoding='utf-8', errors='replace')
                    out = result.stdout or result.stderr or "Виконано"
                    audit(uid, "CMD: " + pending)
                    await msg.edit_text("💻 CMD:\n```\n" + out[:3500] + "\n```", parse_mode="Markdown")
                except Exception as e: await msg.edit_text("❌ " + str(e))
            else:
                hint = cmd_pass_cfg.get("hint","")
                hint_text = "\n💡 Пiдказка: " + hint if hint else ""
                await update.message.reply_text("❌ Невiрний пароль! Спробуй ще раз:" + hint_text)
                # НЕ видаляємо сесію!
            return

        if action == "cmd_set_password":
            session.waiting_for[uid] = "cmd_set_hint"
            session.temp_data[uid] = {"new_pass": text.strip()}
            await update.message.reply_text("💡 Введи пiдказку для пароля\n(наприклад: 'моя вулиця'):")
            return

        if action == "cmd_set_hint":
            new_pass = session.temp_data[uid].get("new_pass","")
            session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
            cmd_pass_cfg["password"] = new_pass
            cmd_pass_cfg["hint"] = text.strip()
            cmd_pass_cfg["verified"] = []
            save_json(CMD_PASS_FILE, cmd_pass_cfg)
            audit(uid, "Пароль CMD змiнено")
            await update.message.reply_text(f"✅ Пароль CMD встановлено!\n💡 Пiдказка: {text.strip()}", reply_markup=settings_kb())
            return

        if action == "edit_param":
            param = session.temp_data[uid].get("param"); gk = session.temp_data[uid].get("group","voltage")
            session.waiting_for.pop(uid,None)
            try:
                val = float(text.strip().replace(",","."))
                MY_PRESET[param]["val"] = val; pr = MY_PRESET[param]
                if state.client and state.client.is_connected:
                    ok = await write_param(param, val)
                    st = "✅ Записано в BMS!" if ok else "💾 Збережено локально"
                else: st = "💾 Збережено (пiдключись для запису)"
                audit(uid, f"Змiнено параметр {param}", f"{val} {pr['unit']}")
                await update.message.reply_text(f"{st}\n{pr['desc']}: {format_param_val(param, val)} {pr['unit']}\n\n{format_preset_group(gk)}",
                    reply_markup=preset_inline_kb(gk))
            except ValueError: await update.message.reply_text("❌ Введи число: 4.20")
            return

        if action == "edit_factory_param":
            param = session.temp_data[uid].get("param"); gk = session.temp_data[uid].get("group","voltage")
            session.waiting_for.pop(uid,None)
            try:
                val = float(text.strip().replace(",","."))
                MY_PRESET[param]["val"] = val
                save_json(PRESET_FILE, MY_PRESET)
                pr = MY_PRESET[param]
                audit(uid, f"Заводський параметр {param}", f"{val} {pr['unit']}")
                await update.message.reply_text(
                    f"✅ Збережено як заводський!\n{pr['desc']}: {format_param_val(param, val)} {pr['unit']}\n\n{format_preset_group(gk)}",
                    reply_markup=preset_inline_kb(gk))
            except ValueError: await update.message.reply_text("❌ Введи число: 4.20")
            return

        if action == "select_bms":
            session.waiting_for.pop(uid,None)
            import re as re2
            m = re2.search(r'([0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})', text.upper())
            if m:
                addr = m.group(1)
                # Знаходимо назву
                name = "JK BMS"
                for d in state.scan_results:
                    if d.address.upper() == addr.upper():
                        name = d.name or "JK BMS"; break
                msg = await update.message.reply_text(f"🔗 Пiдключаюсь до {name}...")
                ok = await connect_bms(addr)
                if not ok: await asyncio.sleep(3); ok = await connect_bms(addr)
                if ok:
                    state.bms_address = addr; state.bms_name = name
                    audit(uid, "Пiдключено до BMS", addr)
                    await update.message.reply_text(
                        f"📟 BMS Керування\n✅ Пiдключено\n🔋 {name}",
                        reply_markup=bms_kb())
                else: await msg.edit_text("❌ Не вдалось пiдключитись")
            return

        if action == "excel_custom_period":
            session.waiting_for.pop(uid,None)
            parts = text.strip().split()
            if len(parts) == 2:
                date_from, date_to = parts[0], parts[1]
                try:
                    import openpyxl
                    wb = openpyxl.Workbook()
                    ws = wb.active
                    ws.title = date_from + " - " + date_to
                    ws.append(["BMS номер","Дата","Статус","Коментар","Записiв","Хто"])
                    count = 0
                    for num, rec in akb_db.items():
                        hist = rec.get("history",[])
                        date_val = (hist[-1].get("date","") if hist else rec.get("date",""))[:10]
                        try:
                            df = datetime.strptime(date_from, "%d.%m.%Y").date()
                            dt = datetime.strptime(date_to, "%d.%m.%Y").date()
                            dv = datetime.strptime(date_val, "%d.%m.%Y").date()
                            in_range = df <= dv <= dt
                        except: in_range = False
                        if in_range:
                            status = hist[-1].get("status","") if hist else rec.get("status","")
                            comment = hist[-1].get("comment","") if hist else rec.get("comment","")
                            user = hist[-1].get("user","") if hist else ""
                            cnt = len(hist) if hist else len(rec.get("photos",[]))
                            ws.append([num, date_val, status, comment, cnt, user])
                            count += 1
                    ts = datetime.now().strftime("%d%m%Y_%H%M")
                    fname = "akb_export_" + ts + ".xlsx"
                    wb.save(fname)
                    await update.message.reply_document(
                        open(fname,"rb"), filename=fname,
                        caption="📊 Excel: " + date_from + " — " + date_to + " (" + str(count) + " батарей)")
                    if os.path.exists(fname): os.remove(fname)
                except Exception as e:
                    await update.message.reply_text("❌ Помилка: " + str(e))
            else:
                await update.message.reply_text("❌ Формат: 01.04.2026 30.04.2026")
            return

        if action == "salary_input":
            session.waiting_for.pop(uid, None)
            td = session.temp_data.get(uid, {})
            sal_action = td.get("sal_action","")
            sal_target = td.get("sal_target","")
            sal_name = td.get("sal_name", sal_target)
            if text == "❌ Скасувати":
                await update.message.reply_text("❌ Скасовано", reply_markup=main_kb(uid))
                return
            # Парсимо введення: "сума, коментар"
            parts_input = text.split(",", 1)
            try:
                amount = int(parts_input[0].strip())
            except:
                await update.message.reply_text("❌ Невiрна сума. Введи число грн.")
                session.waiting_for[uid] = "salary_input"
                return
            comment = parts_input[1].strip() if len(parts_input) > 1 else ""
            now = datetime.now().strftime("%d.%m.%Y %H:%M")
            import uuid as uuid_lib
            rec_id = str(uuid_lib.uuid4())[:8]

            if sal_action == "rate":
                # Змінюємо ставку
                data2 = get_salary_data(sal_target)
                old_rate = data2.get("rate", 2150)
                salary_db[str(sal_target)]["rate"] = amount
                save_json(SALARY_FILE, salary_db)
                audit(uid, f"Змiнено ставку {sal_name}: {old_rate} → {amount} грн/АКБ")
                await update.message.reply_text(f"✅ Ставку {sal_name} змiнено: {old_rate} → {amount} грн/АКБ", reply_markup=main_kb(uid))
                return

            # Додаємо запис
            record = {"id": rec_id, "type": sal_action, "amount": amount, "comment": comment, "date": now, "by": get_name(uid), "confirmed": False}
            get_salary_data(sal_target)
            salary_db[str(sal_target)]["records"].append(record)
            save_json(SALARY_FILE, salary_db)
            icons = {"pay":"💵","advance":"💵","fine":"⚠️","bonus":"🎁"}
            labels = {"pay":"Виплату","advance":"Аванс","fine":"Штраф","bonus":"Бонус"}
            icon = icons.get(sal_action,"💰")
            label = labels.get(sal_action, sal_action)
            audit(uid, f"Зарплата: {label} {amount} грн для {sal_name}")
            await update.message.reply_text(f"{icon} {label} {amount} грн для {sal_name} зафiксовано!", reply_markup=main_kb(uid))

            # Сповiщаємо юзера з кнопками пiдтвердження (для всiх типiв)
            try:
                conf_btns = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Пiдтвердити", callback_data=f"sal_confirm_{rec_id}"),
                    InlineKeyboardButton("❌ Оскаржити", callback_data=f"sal_dispute_{rec_id}")
                ]])
                await ctx.bot.send_message(
                    chat_id=int(sal_target),
                    text=f"{icon} Тобi нараховано: {label.lower()} {amount} грн\nвiд {get_name(uid)}\nКоментар: {comment or '—'}",
                    reply_markup=conf_btns)
            except: pass
            # Сповiщаємо God якщо адмiн робить нарахування
            if not is_god(uid):
                try: await ctx.bot.send_message(chat_id=GOD_ID, text=f"{icon} Адмiн {get_name(uid)} нарахував {label.lower()} {amount} грн для {sal_name}")
                except: pass
            return

        if action == "ws_renum":
            session.waiting_for.pop(uid, None)
            old_num = session.temp_data.get(uid, {}).get("old_num", "")
            new_num = text.strip()
            # Скасування
            if new_num == "❌ Скасувати" or not old_num:
                await update.message.reply_text("❌ Скасовано", reply_markup=main_kb(uid))
                return
            # Перевірка довжини
            if not (1 <= len(new_num) <= 20):
                await update.message.reply_text("❌ Номер має бути вiд 1 до 20 символiв. Введи ще раз:")
                session.waiting_for[uid] = "ws_renum"
                return
            # Перевірка чи новий номер вже існує
            if new_num in workshop_db or new_num in akb_db:
                await update.message.reply_text(f"❌ Батарея з номером {new_num} вже iснує в базi! Введи iнший номер:")
                session.waiting_for[uid] = "ws_renum"
                return
            # Очищаємо temp_data юзерів що працюють з цією батареєю
            for u_id in list(session.waiting_for.keys()):
                if session.temp_data.get(u_id, {}).get("bms_number") == old_num or \
                   session.temp_data.get(u_id, {}).get("akb_num") == old_num:
                    session.waiting_for.pop(u_id, None)
                    session.temp_data.pop(u_id, None)
            # Перейменовуємо в workshop_db
            if old_num in workshop_db:
                workshop_db[new_num] = workshop_db.pop(old_num)
                save_json(WORKSHOP_FILE, workshop_db)
            # Перейменовуємо в akb_db
            if old_num in akb_db:
                akb_db[new_num] = akb_db.pop(old_num)
                save_json(DB_FILE, akb_db)
            audit(uid, f"Змiнено номер батареї: {old_num} → {new_num}")
            await update.message.reply_text(
                f"✅ Номер змiнено: {old_num} → {new_num}",
                reply_markup=main_kb(uid))
            return

        if action == "akb_custom_period":
            session.waiting_for.pop(uid, None)
            parts = text.strip().split()
            if len(parts) == 2:
                date_from, date_to = parts[0], parts[1]
                res = []
                for num, rec in akb_db.items():
                    hist = rec.get("history",[])
                    date_str = hist[-1].get("date","") if hist else rec.get("date","")
                    d = date_str[:10] if len(date_str) >= 10 else date_str
                    try:
                        df = datetime.strptime(date_from, "%d.%m.%Y").date()
                        dt = datetime.strptime(date_to, "%d.%m.%Y").date()
                        dv = datetime.strptime(d, "%d.%m.%Y").date()
                        in_range = df <= dv <= dt
                    except: in_range = False
                    if in_range:
                        res.append((num, rec))
                if not res:
                    await update.message.reply_text("📅 За цей перiод записiв немає")
                else:
                    btns = []
                    for num, rec in res:
                        hist = rec.get("history",[])
                        last = hist[-1] if hist else {}
                        status = last.get("status","") if hist else rec.get("status","")
                        icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
                        btns.append([InlineKeyboardButton(icon + " " + num, callback_data="akb_" + num)])
                    await update.message.reply_text(
                        "📅 " + date_from + " — " + date_to + " (" + str(len(res)) + "):",
                        reply_markup=InlineKeyboardMarkup(btns))
            else:
                await update.message.reply_text("❌ Формат: 01.04.2026 30.04.2026")
            return

        if action == "ai_chat":
            if text in ["🔙 CMD центр", "🔙 Назад"]:
                session.waiting_for.pop(uid, None)
                await update.message.reply_text("💻 CMD центр:", reply_markup=cmd_center_kb())
                return
            # Надсилаємо запит до Gemini
            await update.message.reply_text("🧠 Думаю...")
            import aiohttp, json as json_lib
            try:
                payload = {
                    "contents": [{"parts": [{"text": text}]}],
                    "systemInstruction": {"parts": [{"text": "Ти корисний універсальний асистент. Відповідай на будь-які питання — технічні, творчі, побутові, наукові, BMS, батареї і все інше. Відповідай коротко і по суті на мові користувача."}]}
                }
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    ) as resp:
                        data = await resp.json()
                        if "candidates" in data and data["candidates"]:
                            reply = data["candidates"][0]["content"]["parts"][0]["text"]
                        elif "error" in data:
                            reply = f"Помилка API: {data['error'].get('message','невідома')}"
                        else:
                            reply = f"Невідома відповідь: {str(data)[:200]}"
                        await update.message.reply_text(
                            f"🧠 {reply[:4000]}",
                            reply_markup=ReplyKeyboardMarkup([["🔙 CMD центр"]], resize_keyboard=True))
            except Exception as e:
                await update.message.reply_text(f"❌ Помилка AI: {str(e)[:200]}")
            return

        if action == "diag_find_addr_val":
            if text == "🔙 Назад до дiагностики":
                session.waiting_for.pop(uid, None)
                await update.message.reply_text("🔬 Дiагностика BMS", reply_markup=diag_kb())
                return
            try:
                target = int(text.strip())
            except:
                await update.message.reply_text("❌ Введи цiле число! Наприклад: 1234")
                return
            session.waiting_for.pop(uid, None)
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ BMS не пiдключена!")
                return
            msg = await update.message.reply_text(f"🔎 Шукаю адресу для {target}...\n⏳ Перебираю 0x00-0x50 (~2 хв)")
            import struct
            hdr = bytes([0xAA,0x55,0x90,0xEB])
            found = []
            # Зчитуємо початкові параметри - надсилаємо CMD_DEVICE_INFO і чекаємо пакет 01
            init_buf = bytearray()
            def init_h(_, data): init_buf.extend(data)
            await state.client.start_notify(JK_CHAR_UUID, init_h)
            cmd_info = bytes.fromhex("AA5590EB97000000000000000000000000000011")
            await state.client.write_gatt_char(JK_CHAR_UUID, cmd_info, response=True)
            await asyncio.sleep(1.5)
            try: await state.client.stop_notify(JK_CHAR_UUID)
            except: pass
            # Парсимо початкові параметри
            raw0 = bytes(init_buf)
            idx0 = raw0.find(bytes([0x55,0xAA,0xEB,0x90,0x01]))
            before = {}
            if idx0 != -1:
                pkt0 = raw0[idx0+5:]
                def u16b(off):
                    if off+2 <= len(pkt0): return struct.unpack('<H', pkt0[off:off+2])[0]
                    return 0
                before = {
                    "ovpr": u16b(0x001)/1000, "uvp": u16b(0x005)/1000,
                    "uvpr": u16b(0x009)/1000, "ovp": u16b(0x00D)/1000,
                    "soc100": u16b(0x019)/1000, "soc0": u16b(0x01D)/1000,
                    "rcv_volt": u16b(0x021)/1000, "rfv_volt": u16b(0x025)/1000,
                    "pwr_off": u16b(0x029)/1000,
                }
            
            for addr in range(0x00, 0x51):
                try:
                    vb = struct.pack("<I", target)
                    pl = bytes([addr, 0x04]) + vb + bytes(9)
                    cmd = hdr + pl + bytes([sum(hdr+pl) & 0xFF])
                    # Зчитуємо після запису - з коротким буфером
                    resp_buf = bytearray()
                    def resp_h(_, data): resp_buf.extend(data)
                    await state.client.start_notify(JK_CHAR_UUID, resp_h)
                    await state.client.write_gatt_char(JK_CHAR_UUID, cmd, response=True)
                    await asyncio.sleep(0.2)
                    # Запитуємо Device Info щоб отримати пакет 01
                    cmd_info = bytes.fromhex("AA5590EB97000000000000000000000000000011")
                    await state.client.write_gatt_char(JK_CHAR_UUID, cmd_info, response=True)
                    await asyncio.sleep(0.8)
                    try: await state.client.stop_notify(JK_CHAR_UUID)
                    except: pass
                    # Парсимо пакет 01 з відповіді
                    raw = bytes(resp_buf)
                    idx01 = raw.find(bytes([0x55,0xAA,0xEB,0x90,0x01]))
                    if idx01 != -1:
                        pkt = raw[idx01+5:]
                        def u16(off):
                            if off+2 <= len(pkt): return struct.unpack('<H', pkt[off:off+2])[0]
                            return 0
                        after = {
                            "ovpr": u16(0x001)/1000, "uvp": u16(0x005)/1000,
                            "uvpr": u16(0x009)/1000, "ovp": u16(0x00D)/1000,
                            "soc100": u16(0x019)/1000, "soc0": u16(0x01D)/1000,
                            "rcv_volt": u16(0x021)/1000, "rfv_volt": u16(0x025)/1000,
                            "pwr_off": u16(0x029)/1000,
                        }
                        for key, val in after.items():
                            before_val = before.get(key, 0)
                            if abs(val - target/1000) < 0.005 and abs(before_val - target/1000) > 0.005:
                                found.append(f"0x{addr:02X} = {key} (було {before_val:.3f} → стало {val:.3f})")
                                before[key] = val
                except Exception:
                    pass
            out = f"🔎 Результат для {target}:\n"
            if found:
                out += "\n".join(found)
            else:
                out += "❌ Не знайдено — спробуй iнше значення"
            try:
                await msg.edit_text(out, reply_markup=diag_tools_kb())
            except Exception:
                await update.message.reply_text(out, reply_markup=diag_tools_kb())
            return

        if action == "diag_hex_input":
            if text == "🔙 Назад до дiагностики":
                session.waiting_for.pop(uid, None)
                await update.message.reply_text("🔬 Дiагностика BMS", reply_markup=diag_kb())
                return
            hex_str = text.strip().replace(" ","").replace(":","")
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ BMS не пiдключена!")
                session.waiting_for.pop(uid, None)
                return
            # Визначаємо яку характеристику використовувати
            hex_char = session.temp_data.get(uid, {}).get("hex_char", "FFE1")
            write_uuid = JK_CHAR_UUID2 if hex_char == "FFE2" else JK_CHAR_UUID
            try:
                cmd = bytes.fromhex(hex_str)
                resp_buf = bytearray()
                def resp_handler(_, data): resp_buf.extend(data)
                await state.client.start_notify(JK_CHAR_UUID, resp_handler)
                await state.client.write_gatt_char(write_uuid, cmd, response=True)
                await asyncio.sleep(2.0)
                try: await state.client.stop_notify(JK_CHAR_UUID)
                except: pass
                session.waiting_for.pop(uid, None)
                out = f"📤 Вiдправлено [{hex_char}]: {cmd.hex().upper()}\n"
                out += f"📥 Вiдповiдь ({len(resp_buf)} байт):\n"
                if resp_buf:
                    for row in range(0, min(len(resp_buf), 64), 16):
                        chunk = resp_buf[row:row+16]
                        out += f"[{row:03d}] {' '.join('%02X'%b for b in chunk)}\n"
                else:
                    out += "Немає вiдповiдi"
                await update.message.reply_text(out, reply_markup=diag_network_kb())
            except ValueError:
                await update.message.reply_text("❌ Невiрний HEX формат!")
            return

        if action == "workshop_confirm_video":
            if text == "✅ Зберегти":
                akb_num = session.temp_data.get(uid, {}).get("akb_num")
                stage = session.temp_data.get(uid, {}).get("stage", "6️⃣ Контроль якостi")
                video_id = session.temp_data.get(uid, {}).get("pending_video")
                checklist = session.temp_data.get(uid, {}).get("checklist", {})
                session.waiting_for.pop(uid, None)
                if akb_num and video_id:
                    if akb_num not in workshop_db:
                        workshop_db[akb_num] = {"akb_num": akb_num, "stages": []}
                    workshop_db[akb_num]["stages"].append({
                        "stage": stage,
                        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
                        "user": get_name(uid),
                        "user_id": uid,
                        "video": video_id,
                        "checklist": checklist
                    })
                    save_json(WORKSHOP_FILE, workshop_db)
                    audit(uid, "Збережено вiдео КЯ АКБ " + akb_num)
                    session.temp_data.pop(uid, None)
                    user_stages = [s for s in workshop_db[akb_num]["stages"] if s.get("user_id") == uid or s.get("user_id") == str(uid)]
                    if len(user_stages) >= len(WORKSHOP_STAGES):
                        await update.message.reply_text(
                            "🎉 Батарея №" + akb_num + " зiбрана!\n✅ Всi " + str(len(WORKSHOP_STAGES)) + " етапiв пройдено!\n\nПереходить в Iсторiю робiт.",
                            reply_markup=ReplyKeyboardMarkup([["📋 Iсторiя робiт","🔙 Назад"]], resize_keyboard=True))
                        # Сповіщення всім адмінам
                        for a_id, a_info in users_db.items():
                            a_role = a_info.get("role") if isinstance(a_info, dict) else a_info
                            if a_role == "admin":
                                try:
                                    await ctx.bot.send_message(
                                        chat_id=int(a_id),
                                        text="🏭 Батарея завершена!\n👤 " + get_name(uid) + "\n📟 №" + akb_num + "\n⏱ " + datetime.now().strftime("%d.%m.%Y %H:%M")
                                    )
                                except: pass
                    else:
                        await update.message.reply_text("✅ Контроль якостi збережено!", reply_markup=workshop_kb(uid))
                else:
                    await update.message.reply_text("❌ Помилка збереження", reply_markup=workshop_kb(uid))
            else:
                session.waiting_for.pop(uid, None)
                session.temp_data.pop(uid, None)
                await update.message.reply_text("Скасовано", reply_markup=workshop_kb(uid))
            return

        if action == "ws_bms_number":
            ok, result = validate_akb_num(text)
            if not ok:
                await update.message.reply_text(result + "\n\nВведи ще раз:")
                return
            num = result
            session.waiting_for.pop(uid, None)
            # Якщо батарея вже існує - відкриваємо її
            if num in workshop_db:
                stages = workshop_db[num].get("stages",[])
                user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
                # Перевіряємо чи не зайнята іншим юзером
                other_stages = [s for s in stages if s.get("user_id") != uid and s.get("user_id") != str(uid)]
                if other_stages and not user_stages and len(stages) < len(WORKSHOP_STAGES):
                    other_user = other_stages[-1].get("user","невiдомий")
                    await update.message.reply_text(
                        "⚠️ Батарея №" + num + " вже в роботi у: " + other_user + "\nЗверниться до адмiнiстратора.",
                        reply_markup=workshop_kb(uid))
                    return
                if len(user_stages) >= len(WORKSHOP_STAGES):
                    await update.message.reply_text(
                        "✅ АКБ №" + num + " вже завершена!\nВсi " + str(len(WORKSHOP_STAGES)) + " етапiв пройдено.",
                        reply_markup=workshop_kb(uid))
                    return
                # Показуємо наступний етап як меню
                session.temp_data[uid] = {"bms_number": num, "akb_num": num}
                next_idx = len(user_stages)
                if next_idx < len(WORKSHOP_STAGES):
                    next_stage = WORKSHOP_STAGES[next_idx]
                    needs_two = next_stage in WORKSHOP_TWO_PHOTOS
                    hint = " (потрiбно 2 фото)" if needs_two else ""
                    session.waiting_for[uid] = "workshop_stage"
                    await update.message.reply_text(
                        "📟 АКБ №" + num + " (" + str(next_idx) + "/" + str(len(WORKSHOP_STAGES)) + ")\nНаступний етап:\n" + next_stage + hint,
                        reply_markup=ReplyKeyboardMarkup([[next_stage], ["🔙 Назад до кабiнету"]], resize_keyboard=True))
            else:
                # Нова батарея
                session.temp_data[uid] = {"bms_number": num, "akb_num": num}
                session.waiting_for[uid] = "workshop_stage"
                workshop_db[num] = {"akb_num": num, "stages": []}
                save_json(WORKSHOP_FILE, workshop_db)
                await update.message.reply_text(
                    "📟 АКБ: " + num + "\n\nОберiть перший етап збiрки:",
                    reply_markup=workshop_stages_kb(num))
            return

        if action == "akb_bms_number":
            ok, result = validate_akb_num(text)
            if not ok:
                await update.message.reply_text(result + "\n\nВведи ще раз:")
                return
            num = result
            # Перевіряємо чи вже є в базі
            if num in akb_db:
                session.temp_data[uid]["bms_number"] = num
                rec = akb_db[num]
                hist = rec.get("history", [])
                t = f"⚠️ АКБ №{num} вже є в базi!\n\n"
                t += f"Записiв: {len(hist)}\n"
                if hist:
                    last = hist[-1]
                    t += f"Останнiй: {last.get('date','')} — {last.get('status','')}\n"
                t += "\nЩо зробити?"
                btns = ReplyKeyboardMarkup([
                    ["📖 Переглянути iсторiю"],
                    ["➕ Додати новий запис"],
                    ["🔙 Назад"]
                ], resize_keyboard=True)
                session.waiting_for[uid] = "akb_exists_action"
                await update.message.reply_text(t, reply_markup=btns)
            else:
                session.temp_data[uid]["bms_number"] = num
                session.waiting_for[uid] = "akb_status"
                await update.message.reply_text(f"📟 {num}\n\nОберiть статус:", reply_markup=akb_status_kb())
            return

        if action == "akb_exists_action":
            num = session.temp_data[uid].get("bms_number")
            if text == "📖 Переглянути iсторiю":
                session.waiting_for.pop(uid,None)
                rec = akb_db.get(num, {})
                hist = rec.get("history", [])
                t = f"📋 Iсторiя АКБ №{num}:\n\n"
                for i, h in enumerate(hist[-10:]):
                    icon = "✅" if h.get("status","")=="справний" else ("❌" if h.get("status","")=="бракований" else ("🔍" if h.get("status","")=="на перевiрцi" else "❓"))
                    t += f"{icon} {h.get('date','')} — {h.get('status','')} ({h.get('user','')})\n"
                await update.message.reply_text(t, reply_markup=akb_kb(uid))
            elif text == "➕ Додати новий запис":
                session.waiting_for[uid] = "akb_status"
                await update.message.reply_text("Оберiть статус:", reply_markup=akb_status_kb())
            else:
                session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
                await update.message.reply_text("🔙 Скасовано", reply_markup=akb_kb(uid))
            return

        if action == "akb_status":
            status_map = {
                "✅ справний": "справний",
                "❌ бракований": "бракований",
                "🔍 на перевiрцi": "на перевiрцi",
            }
            status_val = status_map.get(text.strip().lower())
            if not status_val:
                await update.message.reply_text(
                    "⚠️ Оберiть статус з кнопок:",
                    reply_markup=akb_status_kb())
                return
            session.temp_data[uid]["status"] = status_val
            session.waiting_for[uid] = "akb_comment"
            await update.message.reply_text(
                "Коментар (або 'нi'):",
                reply_markup=ReplyKeyboardMarkup([["нi"], ["🔙 Назад до АКБ"]], resize_keyboard=True))
            return
        if action == "akb_comment":
            session.temp_data[uid]["comment"] = "" if text.strip().lower() in ["нi","ni","нет","no"] else text.strip()
            session.waiting_for[uid] = "akb_photo"
            await update.message.reply_text("📸 Надiшли фото АКБ!"); return
        if action == "akb_search":
            q = text.strip().lower()
            res = [(n,r) for n,r in akb_db.items() if q in n.lower()]
            session.waiting_for.pop(uid,None)
            if not res: await update.message.reply_text(f"❌ '{q}' не знайдено"); return
            for num,rec in res[:5]:
                # Підтримка старого і нового формату
                hist = rec.get("history",[])
                if hist:
                    last = hist[-1]
                    status = last.get("status","")
                    comment = last.get("comment","")
                    date = last.get("date","")
                    photos = last.get("photos",[])
                else:
                    # Старий формат
                    status = rec.get("status","")
                    comment = rec.get("comment","")
                    date = rec.get("date","")
                    photos = rec.get("photos",[])
                icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
                t = f"{icon} АКБ: {num}\n📅 {date}\n📊 {status}\n💬 {comment}\n📝 Записiв: {max(len(hist),1)}"
                await update.message.reply_text(t)
                for photo in photos:
                    try: await update.message.reply_photo(photo["file_id"])
                    except Exception: pass
            return
        if action == "workshop_akb_num":
            num = text.strip()
            session.temp_data[uid]["akb_num"] = num
            session.waiting_for[uid] = "workshop_stage"
            await update.message.reply_text(f"📟 АКБ: {num}\n\nОберiть етап збiрки:", reply_markup=workshop_stages_kb(num))
            return
        if action == "workshop_stage":
            stage = text.strip()
            if stage in ["🔙 Назад до кабiнету", "🔙 Назад"]:
                session.waiting_for.pop(uid,None)
                await update.message.reply_text("🪪 Робочий кабiнет", reply_markup=workshop_kb(uid)); return
            if stage in WORKSHOP_STAGES:
                # Перевіряємо порядок - всі попередні мають бути пройдені
                num = session.temp_data.get(uid, {}).get("akb_num", "")
                stage_idx = WORKSHOP_STAGES.index(stage)
                stages_done = workshop_db.get(num, {}).get("stages", [])
                for prev_idx in range(stage_idx):
                    prev_stage = WORKSHOP_STAGES[prev_idx]
                    if not next((s for s in stages_done if s.get("stage") == prev_stage), None):
                        await update.message.reply_text(
                            "⚠️ Спочатку треба пройти:\n" + prev_stage,
                            reply_markup=workshop_stages_kb(num))
                        return
                # Контроль якості - показуємо чеклист і просимо відео
                if stage == "6️⃣ Контроль якостi":
                    num = session.temp_data[uid].get("akb_num","")
                    cl_text = "6️⃣ Контроль якостi батареї №" + num + "\n\nПеревiр кожен пункт перед здачею:\n\n" + "\n".join(QUALITY_CHECKLIST)
                    await update.message.reply_text(
                        cl_text,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "✅ Ознайомився, знiмаю вiдео",
                            callback_data="qcvideo_" + num)]]))
                else:
                    needs_two = stage in WORKSHOP_TWO_PHOTOS
                    session.temp_data[uid]["stage"] = stage
                    session.temp_data[uid]["photo_num"] = 1
                    session.waiting_for[uid] = "workshop_photo"
                    hint = "\n📸 Потрiбно 2 фото!\nНадiшли ПЕРШЕ фото (лицьова сторона)" if needs_two else "\n📸 Надiшли фото"
                    await update.message.reply_text(
                        "Етап: " + stage + hint,
                        reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))
            return
        if action == "workshop_search":
            num = text.strip(); session.waiting_for.pop(uid,None)
            if num in workshop_db:
                stages = workshop_db[num].get("stages",[])
                t = f"📋 Iсторiя збiрки АКБ №{num}:\n\n"
                for s in stages:
                    t += f"🔧 {s.get('stage','')}\n👤 {s.get('user','')}\n📅 {s.get('date','')}\n\n"
                await update.message.reply_text(t[:4000])
                for s in stages[-3:]:
                    if s.get("photo"):
                        try: await update.message.reply_photo(s["photo"], caption=s.get("stage",""))
                        except Exception: pass
            else: await update.message.reply_text(f"❌ АКБ {num} не знайдено в робочому журналi")
            return
        if action == "add_user":
            new_uid = text.strip()
            session.waiting_for.pop(uid, None)
            if not new_uid.lstrip("-").isdigit():
                await update.message.reply_text("❌ Невiрний ID. Введи числовий Telegram ID:")
                session.waiting_for[uid] = "add_user"
                return
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["new_user_id"] = new_uid
            await update.message.reply_text(
                f"👤 ID: {new_uid}\nОберiть роль:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 User", callback_data=f"addu_{new_uid}_user"),
                     InlineKeyboardButton("🛡 Admin", callback_data=f"addu_{new_uid}_admin")]
                ]))
            return
        if action == "del_user":
            uid2 = text.strip(); session.waiting_for.pop(uid,None)
            if uid2 in users_db:
                del users_db[uid2]; save_json(USERS_FILE,users_db)
                audit(uid, f"Видалено юзера {uid2}")
                await update.message.reply_text(f"✅ {uid2} видалено")
            return

    # ── КНОПКИ НАЗАД ──
    if text in ["🔙 Назад", "🚫 Скасувати сканування"]:
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        await update.message.reply_text("🏠 Головне меню", reply_markup=main_kb(uid)); return
    if text == "🔙 Назад до налаштувань":
        await update.message.reply_text("⚙️ Налаштування", reply_markup=settings_kb()); return
    if text == "🔙 Назад до кабiнету":
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        await update.message.reply_text("🪪 Робочий кабiнет", reply_markup=workshop_kb(uid)); return

    # ── ГОЛОВНЕ МЕНЮ ──
    if text == "📟 BMS Керування":
        if has_perm(uid,"bms") or is_god(uid):
            conn = "✅ Пiдключено" if (state.client and state.client.is_connected) else "❌ Не пiдключено"
            info = ""
            if state.bms_address and state.client and state.client.is_connected:
                info = "\n🔋 " + (state.bms_name or "JK BMS") + "\n📍 " + state.bms_address
            await update.message.reply_text(
                "📍 Роздiл: BMS Керування\n\n" + conn + info,
                reply_markup=bms_kb())
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "📂 База АКБ":
        audit(uid, "Вiдкрив базу АКБ")
        await update.message.reply_text(
            "📍 Роздiл: База АКБ\n\n🗄 Записiв: " + str(len(akb_db)),
            reply_markup=akb_kb(uid))

    elif text == "🔉 Алерти":
        if not has_perm(uid,"bms") and not is_god(uid):
            await update.message.reply_text("❌ Немає прав"); return
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Спочатку пiдключись!"); return
        if state.alerts_enabled:
            state.alerts_enabled = False
            if state.alert_task: state.alert_task.cancel()
            await update.message.reply_text("🔕 Алерти вимкнено")
        else:
            state.alerts_enabled = True
            state.alert_task = asyncio.create_task(alert_loop(ctx.application, update.effective_chat.id))
            await update.message.reply_text("🔔 Алерти увiмкнено!\n🔴>4.20V | 🪫<3.00V\n🌡>80C | ⚠️>0.1V дельта")


    elif text == "📋 Audit Log":
        if is_god(uid):
            logs = state.log[-30:]
            await update.message.reply_text("📋 Audit Log:\n\n" + "\n".join(logs) if logs else "📋 Лог порожнiй")
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "💾 Резервна копiя":
        if is_god(uid):
            import zipfile
            msg = await update.message.reply_text("⏳ Створюю резервну копiю...")
            try:
                backups_dir = "backups"
                if not os.path.exists(backups_dir):
                    os.makedirs(backups_dir)
                ts = datetime.now().strftime("%d%m%Y_%H%M")
                zip_name = "VoltForge_backup_" + ts + ".zip"
                zip_path = os.path.join(backups_dir, zip_name)
                files = ["users.json","akb_database.json","bms_registry.json",
                         "preset.json","workshop.json","audit.log","cmd_password.json"]
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname in files:
                        if os.path.exists(fname):
                            zf.write(fname)
                audit(uid, "Резервна копiя: " + zip_name)
                await msg.delete()
                await update.message.reply_text(
                    "✅ Резервна копiя збережена!\n"
                    "📁 BMSBOT\\backups\\" + zip_name + "\n📅 " + ts)
            except Exception as e:
                await update.message.reply_text("❌ Помилка: " + str(e))
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "📉 Логи":
        if has_perm(uid,"logs") or is_god(uid):
            role = get_role(uid)
            if is_god(uid):
                # God бачить всіх
                logs = state.log[-30:]
            elif role == "admin":
                # Адмін бачить юзерів але не God і не інших адмінів
                logs = []
                for entry in state.log[-50:]:
                    if str(GOD_ID) not in entry:
                        logs.append(entry)
                logs = logs[-30:]
            else:
                # Юзер бачить тільки свої
                name = get_name(uid)
                logs = [l for l in state.log if str(uid) in l or name in l][-20:]
            await update.message.reply_text("📋 Логи:\n\n" + "\n".join(logs) if logs else "📋 Лог порожнiй")
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "📋 Audit Log":
        if is_god(uid):
            logs = state.log[-50:]
            await update.message.reply_text("📋 Audit Log:\n\n" + "\n".join(logs) if logs else "📋 Лог порожнiй")
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "💾 Резервна копiя":
        if is_god(uid):
            import zipfile
            msg = await update.message.reply_text("⏳ Створюю резервну копiю...")
            try:
                backups_dir = "backups"
                if not os.path.exists(backups_dir):
                    os.makedirs(backups_dir)
                ts = datetime.now().strftime("%d%m%Y_%H%M")
                zip_name = "VoltForge_backup_" + ts + ".zip"
                zip_path = os.path.join(backups_dir, zip_name)
                files = ["users.json","akb_database.json","bms_registry.json",
                         "preset.json","workshop.json","audit.log","cmd_password.json"]
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname in files:
                        if os.path.exists(fname):
                            zf.write(fname)
                audit(uid, "Резервна копiя: " + zip_name)
                await msg.delete()
                await update.message.reply_text(
                    "✅ Резервна копiя збережена!\n"
                    "📁 BMSBOT\\backups\\" + zip_name + "\n📅 " + ts)
            except Exception as e:
                await update.message.reply_text("❌ Помилка: " + str(e))
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "👥 Працівники":
        if get_role(uid) == "admin" or is_god(uid):
            clean = {k:v for k,v in users_db.items()
                     if k and k != "None"
                     and (v.get("role","user") if isinstance(v,dict) else v) in ["user","admin"]}
            items_w = []
            god_name = users_db.get(str(GOD_ID), {}).get("name","Kaatastroffaa") if str(GOD_ID) in users_db else "Kaatastroffaa"
            items_w.append(("👤 " + god_name, "uwk_" + str(GOD_ID)))
            for uid2,info in clean.items():
                if uid2 == str(GOD_ID): continue
                n2 = info.get("name",uid2) if isinstance(info,dict) else uid2
                items_w.append(("👤 " + n2, "uwk_" + uid2))
            if not items_w:
                await update.message.reply_text("Немає працiвникiв")
            else:
                session.temp_data[uid] = session.temp_data.get(uid, {})
                session.temp_data[uid]["workers_items"] = items_w
                await update.message.reply_text(
                    "👤 Працiвники (" + str(len(items_w)) + ") — оберiть працiвника:",
                    reply_markup=make_paginated_inline(items_w, 0, "wkp_", "працiвникiв"))

    elif text.startswith("🔄 Активнi (") or text.startswith("📋 Iсторiя ("):
        if is_god(uid) or get_role(uid) == "admin":
            td = session.temp_data.get(uid, {})
            target = td.get("view_worker_uid")
            n = td.get("view_worker_name", "?")
            if not target:
                await update.message.reply_text("❌ Оберiть працiвника знову")
                return
            is_active = text.startswith("🔄 Активнi")
            items = []
            items_history = []  # для клавiатури з чекбоксами: (num, label)
            for num, wdata in workshop_db.items():
                stages = wdata.get("stages", [])
                user_stages = [s for s in stages if str(s.get("user_id","")) == str(target)]
                count = len(user_stages)
                if is_active and count > 0 and count < len(WORKSHOP_STAGES):
                    items.append(("🔋 " + num + " (" + str(count) + "/" + str(len(WORKSHOP_STAGES)) + ")", "ws_" + num))
                elif not is_active and count >= len(WORKSHOP_STAGES):
                    last = user_stages[-1]
                    lbl = "✅ " + num + " | " + last.get("date","")[:10]
                    items.append((lbl, "ws_" + num))
                    # Для чекбокс-iсторiї — скороченi номери щоб вмiщались
                    short_num = ("..." + num[-10:]) if len(num) > 12 else num
                    items_history.append((num, "✅ " + short_num))
            if not items:
                label = "активних робiт" if is_active else "завершених батарей"
                await update.message.reply_text("📋 " + n + " — немає " + label)
            else:
                label = "🔄 Активнi роботи" if is_active else "📋 Iсторiя робiт"
                prefix = "wwka_" if is_active else "wwkc_"
                session.temp_data[uid]["wwk_items"] = items
                session.temp_data[uid]["wwk_is_active"] = is_active
                audit(uid, ("Переглянув активнi роботи: " if is_active else "Переглянув iсторiю робiт: ") + n)
                if not is_active:
                    # Iсторiя — з чекбоксами для надсилання фото Етапу 2
                    session.temp_data[uid]["wwkc_selected"] = set()
                    session.temp_data[uid]["wwkc_items"] = items_history
                    await update.message.reply_text(
                        label + " — " + n + " (" + str(len(items_history)) + ")\n\n"
                        "ℹ️ Вiдмiть батареї ☑ i натисни етап знизу —\n"
                        "отримаєш медiа з цього етапу обраних батарей.",
                        reply_markup=make_history_checkbox_kb(items_history, 0, set()))
                else:
                    await update.message.reply_text(
                        label + " — " + n + " (" + str(len(items)) + "):",
                        reply_markup=make_paginated_inline(items, 0, prefix, "батарей"))

    elif text == "🪪 Робочий кабiнет":
        if has_perm(uid,"workshop") or is_god(uid):
            session.waiting_for.pop(uid, None)
            session.temp_data.pop(uid, None)
            audit(uid, "Вiдкрив робочий кабiнет")
            name = get_name(uid)
            await update.message.reply_text(
                "📍 Роздiл: Робочий кабiнет\n👤 " + name + "\n\nФiксуй етапи збiрки батарей:",
                reply_markup=workshop_kb(uid, page=0))
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "💰 Мої нарахування":
        if has_perm(uid,"workshop") or is_god(uid):
            name = get_name(uid)
            txt = format_salary_text(uid, name)
            await update.message.reply_text(txt,
                reply_markup=ReplyKeyboardMarkup([
                    ["📋 Моя iсторiя виплат"],
                    ["🔙 Назад до кабiнету"]
                ], resize_keyboard=True))
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "📊 Моя статистика":
        if has_perm(uid,"workshop") or is_god(uid):
            name = get_name(uid)
            sal = get_salary_data(uid)
            rate = sal.get("rate", 2150)
            txt = get_stats_text(uid, name, rate)
            await update.message.reply_text(txt)
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "📋 Моя iсторiя виплат":
        if has_perm(uid,"workshop") or is_god(uid):
            data2 = get_salary_data(uid)
            records = data2.get("records",[])
            if not records:
                await update.message.reply_text("📋 Моя iсторiя виплат\n\nЗаписiв ще немає")
                return
            icons = {"pay":"💵","advance":"💵","fine":"⚠️","bonus":"🎁"}
            labels = {"pay":"Виплата","advance":"Аванс","fine":"Штраф","bonus":"Бонус"}
            for r in records[-20:]:
                icon = icons.get(r["type"],"💰")
                label = labels.get(r["type"],r["type"])
                conf = "✅ Пiдтверджено" if r.get("confirmed") else "⏳ Очiкує пiдтвердження"
                txt = f"{icon} {r.get('date','')[:10]} — {label} {r['amount']} грн\n"
                if r.get("comment"):
                    txt += f"📝 {r['comment']}\n"
                txt += f"👤 вiд {r.get('by','')} | {conf}"
                # Для непiдтверджених — кнопки пiдтвердження/оскарження (всi типи)
                if not r.get("confirmed"):
                    btns = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Пiдтвердити", callback_data=f"sal_confirm_{r['id']}"),
                        InlineKeyboardButton("❌ Оскаржити", callback_data=f"sal_dispute_{r['id']}")
                    ]])
                    await update.message.reply_text(txt, reply_markup=btns)
                else:
                    await update.message.reply_text(txt)
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "💰 Зарплата":
        if not (is_god(uid) or (get_role(uid) == "admin" and has_perm(uid,"salary"))):
            await update.message.reply_text("❌ Немає прав"); return
        td = session.temp_data.get(uid, {})
        target = td.get("view_worker_uid")
        if not target:
            await update.message.reply_text("❌ Спочатку зайди в кабiнет працiвника"); return
        name = td.get("view_worker_name", target)
        session.temp_data[uid]["salary_target"] = target
        session.temp_data[uid]["salary_name"] = name
        txt = format_salary_text(target, name)
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("💵 Виплата", callback_data="sal_pay_" + target),
             InlineKeyboardButton("💵 Аванс", callback_data="sal_adv_" + target)],
            [InlineKeyboardButton("⚠️ Штраф", callback_data="sal_fine_" + target),
             InlineKeyboardButton("🎁 Бонус", callback_data="sal_bonus_" + target)],
            [InlineKeyboardButton("✏️ Змiнити ставку", callback_data="sal_rate_" + target)],
            [InlineKeyboardButton("📋 Iсторiя", callback_data="sal_hist_" + target)],
        ])
        await update.message.reply_text(txt, reply_markup=btns)

    elif text in ["◀️ Назад", "▶️ Далі"] and (has_perm(uid,"workshop") or is_god(uid)):
        # Пагінація головного меню кабінету
        td = session.temp_data.get(uid, {})
        page = td.get("ws_page", 0)
        if text == "◀️ Назад":
            page = max(0, page - 1)
        else:
            page += 1
        session.temp_data[uid] = td
        session.temp_data[uid]["ws_page"] = page
        await update.message.reply_text(
            "📍 Робочий кабiнет:",
            reply_markup=workshop_kb(uid, page=page))

    elif text == "❔ Допомога":
        help_text = (
            "🔋 VoltForge BMS v15.1\n\n"
            "📚 ЯК ПРАЦЮВАТИ:\n\n"
            "🪪 Робочий кабiнет\n"
            "  → ➕ Нова батарея — введи серiйний номер\n"
            "     (повний номер з самої БМС)\n"
            "  → Пройди 5 етапiв послiдовно:\n\n"
            "  1️⃣ Прийом елементiв (2 фото)\n"
            "     Перевiрка елементiв перед збiркою\n\n"
            "  2️⃣ Замiр опору\n"
            "     Замiр опору всiх спаяних елементiв\n"
            "     з пiдключеною БМС\n\n"
            "  3️⃣ Звiт опорiв четвiрки\n"
            "     Об'єднуєш 4 АКБ у четвiрку. У кожнiй з 4\n"
            "     мають бути пройденi 2 етапи. Потiм у однiй\n"
            "     АКБ додаєш фото i вибираєш ще 3 батареї —\n"
            "     фото копiюється на всi i етап завершується\n"
            "     у всiх 4 одразу.\n\n"
            "  4️⃣ Дата та запакування\n"
            "     Фото з датою i упаковкою\n\n"
            "  5️⃣ Готовий АКБ\n"
            "     Фiнальне фото готової батареї\n\n"
            "  → Пiсля 5-го етапу батарея завершена\n"
            "  → 🔄 Активнi — твої АКБ в процесi\n"
            "  → 📋 Iсторiя — завершенi АКБ\n\n"
            "📹 Контроль якостi (опцiональний)\n"
            "  → Зайди в Iсторiю → батарея → \"📹 Контроль якостi\"\n"
            "  → Прочитай чеклiст з 24 пунктiв\n"
            "  → Надiшли вiдео огляду 30-60 сек\n"
            "  → Додати може тiльки той хто завершив АКБ\n\n"
            "💰 Мої нарахування\n"
            "  → 📊 Моя статистика — за тиждень/мiсяць/рiк\n"
            "  → 📋 Iсторiя виплат — всi записи\n"
            "  → ⏳ ✅ Пiдтверджуй коли отримав:\n"
            "        виплату, аванс, бонус або штраф\n\n"
            "👥 Працiвники (для адмiна)\n"
            "  → Список усiх юзерiв\n"
            "  → Тап на юзера → його Кабiнет:\n"
            "     • 🔄 Активнi роботи / 📋 Iсторiя\n"
            "     • 💰 Зарплата\n\n"
            "📋 Iсторiя в кабiнетi працiвника (для адмiна)\n"
            "  → Чекбокси ☐/☑ бiля кожної АКБ\n"
            "  → Видiли потрiбнi, натисни кнопку етапу:\n"
            "       📷 1-5 — фото етапу\n"
            "       📹 Контроль якостi — вiдео + чеклiст\n"
            "  → Отримаєш медiа альбомами в чат\n\n"
            "💰 Зарплата у кабiнетi юзера (адмiн, за правом)\n"
            "  → 💵 Виплата / 💵 Аванс / 🎁 Бонус / ⚠️ Штраф\n"
            "  → ✏️ Змiна ставки за АКБ\n"
            "  → 📋 Iсторiя нарахувань юзера\n\n"
            "📂 База АКБ\n"
            "  → 🔋 Додати АКБ — фото + номер + статус\n"
            "  → 🔎 Знайти АКБ — пошук по номеру\n"
            "  → 📁 Всi АКБ — список усiх\n"
            "  → 📅 Звiти за перiод (за правом)\n"
            "  → 📑 Експорт Excel (за правом)\n\n"
            "❓ З питань звертайтесь до системного адмiнiстратора"
        )
        try:
            photo_path = "jkbms.jpg"
            if os.path.exists(photo_path):
                # Текст довший за лiмiт caption (1024) — шлемо фото i текст окремо
                await update.message.reply_photo(open(photo_path, "rb"))
            await update.message.reply_text(help_text)
        except Exception:
            await update.message.reply_text(help_text)

    elif text == "⚙️ Налаштування":
        if is_god(uid):
            await update.message.reply_text(
                "📍 Роздiл: Налаштування\n👑 God\n\nКористувачiв: " + str(len(users_db)),
                reply_markup=settings_kb())

    elif text == "💻 CMD центр":
        if is_god(uid):
            if cmd_pass_cfg.get("password") and uid not in cmd_pass_cfg.get("verified",[]):
                session.waiting_for[uid] = "cmd_password_first"
                session.temp_data[uid] = {"after": "cmd_menu"}
                await update.message.reply_text("🔐 Введи пароль для доступу до CMD центру:")
            else:
                await update.message.reply_text(
                    "📍 Роздiл: CMD центр\n\n💻 Керування ноутбуком:",
                    reply_markup=cmd_center_kb())
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text in DIAG_CATEGORIES:
        # Вхід в підкатегорію діагностики
        if text == "📊 Данi BMS":
            await update.message.reply_text("📊 Данi BMS — оберiть:", reply_markup=diag_data_kb())
        elif text == "📡 Мережа i протокол":
            await update.message.reply_text("📡 Мережа i протокол — оберiть:", reply_markup=diag_network_kb())
        elif text == "⚡️ Стан системи":
            await update.message.reply_text("⚡️ Стан системи — оберiть:", reply_markup=diag_status_kb())
        elif text == "🔧 Iнструменти":
            await update.message.reply_text("🔧 Iнструменти — оберiть:", reply_markup=diag_tools_kb())

    elif text == "🔙 Назад до дiагностики":
        await update.message.reply_text("🔬 Дiагностика BMS", reply_markup=diag_kb())

    elif text in DIAG_BUTTONS or text in ["📤 FFE1","📤 FFE2","🔎 Знайти адресу"]:
        # Перехват пакетів
        if text == "📡 Перехват пакетiв":
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            await update.message.reply_text("📡 Перехоплення пакетiв запущено...\n⏱ Збираю данi 5 сек...")
            buf = bytearray()
            log_lines = []
            import time
            def capture_handler(_, data):
                ts = time.strftime("%H:%M:%S")
                log_lines.append(f"[{ts}] <- {data.hex().upper()}")
                buf.extend(data)
            await state.client.start_notify(JK_CHAR_UUID, capture_handler)
            await asyncio.sleep(10.0)
            try: await state.client.stop_notify(JK_CHAR_UUID)
            except: pass
            # Визначаємо яка клавіатура активна
            cur_kb = diag_network_kb()
            if log_lines:
                out = f"📡 Перехоплено {len(log_lines)} пакетiв:\n\n"
                out += "\n".join(log_lines[:10])
                if len(log_lines) > 10:
                    out += f"\n...ще {len(log_lines)-10} пакетiв"
                out += f"\n\n📊 Всього байт: {len(buf)}"
            else:
                out = "📡 Пакетiв не отримано"
            await update.message.reply_text(out, reply_markup=cur_kb)
        # Відправити HEX - показує підменю FFE1/FFE2
        elif text == "📤 Вiдправити HEX":
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            await update.message.reply_text(
                "📤 Оберiть характеристику для вiдправки:",
                reply_markup=diag_hex_kb())
        # Вибір FFE1
        elif text == "📤 FFE1":
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            session.waiting_for[uid] = "diag_hex_input"
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["hex_char"] = "FFE1"
            await update.message.reply_text(
                "📤 FFE1 — введи HEX команду:\n\nПриклад: AA5590EB96000000000000000000000000010",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад до дiагностики"]], resize_keyboard=True))
        # Знайти адресу - показує підменю діапазонів
        elif text == "🔎 Знайти адресу":
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            session.waiting_for[uid] = "diag_find_addr_val"
            await update.message.reply_text(
                "🔎 Введи значення для пошуку адреси:\n\nНаприклад: 7 (Cell Count)\n60000 (60Ah ємнiсть)\n70000 (70A струм)",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад до дiагностики"]], resize_keyboard=True))

        # Вибір діапазону пошуку
        elif text in ["🔎 0x00-0x10","🔎 0x10-0x20","🔎 0x20-0x30","🔎 0x30-0x40","🔎 0x40-0x50","🔎 0x50-0x60"]:
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            target = session.temp_data.get(uid, {}).get("find_addr_val")
            if not target:
                await update.message.reply_text("❌ Спочатку введи значення через 🔎 Знайти адресу!")
                return
            ranges = {"🔎 0x00-0x10":(0x00,0x10),"🔎 0x10-0x20":(0x10,0x20),
                      "🔎 0x20-0x30":(0x20,0x30),"🔎 0x30-0x40":(0x30,0x40),
                      "🔎 0x40-0x50":(0x40,0x50),"🔎 0x50-0x60":(0x50,0x60)}
            start, end = ranges[text]
            msg = await update.message.reply_text(f"🔎 Шукаю {target} в {text}...")
            import struct
            hdr = bytes([0xAA,0x55,0x90,0xEB])
            found = []
            for addr in range(start, end):
                try:
                    vb = struct.pack("<I", target)
                    pl = bytes([addr, 0x04]) + vb + bytes(9)
                    cmd = hdr + pl + bytes([sum(hdr+pl) & 0xFF])
                    resp_buf = bytearray()
                    def resp_h(_, data): resp_buf.extend(data)
                    await state.client.start_notify(JK_CHAR_UUID, resp_h)
                    await state.client.write_gatt_char(JK_CHAR_UUID, cmd, response=True)
                    await asyncio.sleep(0.3)
                    try: await state.client.stop_notify(JK_CHAR_UUID)
                    except: pass
                    if bytes([0xC8, 0x01]) in bytes(resp_buf):
                        found.append(f"0x{addr:02X}")
                except Exception:
                    pass
            out = f"🔎 Результат {text} для значення {target}:\n"
            out += f"✅ Знайдено: {', '.join(found)}" if found else "❌ Не знайдено в цьому дiапазонi"
            await msg.edit_text(out, reply_markup=diag_addr_kb())

        # Вибір FFE2
        elif text == "📤 FFE2":
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            session.waiting_for[uid] = "diag_hex_input"
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["hex_char"] = "FFE2"
            await update.message.reply_text(
                "📤 FFE2 — введи HEX команду:\n\nПриклад: AA5590EB039A1000000000000000000000000027",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад до дiагностики"]], resize_keyboard=True))
        # Всі інші кнопки діагностики
        else:
            if not state.client or not state.client.is_connected:
                await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
                return
            await update.message.reply_text("⏳ Зчитую данi з BMS...")
            out = await run_diag(state.client, text)
            # Повертаємо правильну клавіатуру підкатегорії
            if text in ["📦 Пакет","🔍 Фрейм","⚡️ Ячейки","🌡 Температури","🔋 Напруга/SOC"]:
                kb = diag_data_kb()
            elif text in ["🔄 Протокол","📊 Offsets","🔢 Сирi байти","📶 RSSI сигнал"]:
                kb = diag_network_kb()
            elif text in ["⚖️ Баланс деталi","🔌 Статус деталi","📈 Ємнiсть Ah","🚨 Аварiйнi коди"]:
                kb = diag_status_kb()
            else:
                kb = diag_tools_kb()
            await update.message.reply_text(out[:4000], reply_markup=kb)

    elif text == "🔙 Назад до налаштувань":
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        await update.message.reply_text("⚙️ Налаштування", reply_markup=settings_kb())

    elif text == "🔙 CMD центр":
        session.waiting_for.pop(uid,None)
        await update.message.reply_text("💻 CMD центр:", reply_markup=cmd_center_kb())

    elif text == "🖥 Екран":
        if is_god(uid): await update.message.reply_text("🖥 Екран:", reply_markup=cmd_screen_kb())
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "⚡️ Живлення":
        if is_god(uid): await update.message.reply_text("⚡️ Живлення:", reply_markup=cmd_power_kb())
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "🤖 Бот":
        if is_god(uid): await update.message.reply_text("🤖 Бот:", reply_markup=cmd_bot_kb())
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "⌨️ Ввести команду":
        if is_god(uid):
            if cmd_pass_cfg.get("password") and uid not in cmd_pass_cfg.get("verified",[]):
                session.waiting_for[uid] = "cmd_password_first"
                session.temp_data[uid] = {"after": "cmd_command"}
                await update.message.reply_text("🔐 Введи пароль:")
            else:
                session.waiting_for[uid] = "cmd_command"
                await update.message.reply_text("💻 Введи команду Windows:\n⚠️ Будь обережний!")
        else: await update.message.reply_text("❌ Тiльки для God")

    elif text == "📸 Скріншот":
        if not is_god(uid): await update.message.reply_text("❌ Тiльки для God"); return
        msg = await update.message.reply_text("⏳ Роблю скріншот...")
        try:
            sc_script = "screenshot.ps1"
            ps_lines = [
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing",
                "$s=[System.Windows.Forms.Screen]::PrimaryScreen",
                "$b=New-Object System.Drawing.Bitmap($s.Bounds.Width,$s.Bounds.Height)",
                "$g=[System.Drawing.Graphics]::FromImage($b)",
                "$g.CopyFromScreen(0,0,0,0,$b.Size)",
                "$b.Save('screenshot.png')",
            ]
            with open(sc_script, "w") as scf:
                scf.write("\n".join(ps_lines))
            subprocess.run("powershell -ExecutionPolicy Bypass -File " + sc_script, shell=True, timeout=8)
            if os.path.exists("screenshot.png"):
                await msg.delete()
                await update.message.reply_photo(open("screenshot.png","rb"), caption="📸 Екран зараз")
                os.remove("screenshot.png")
            else:
                await msg.edit_text("❌ Не вдалось зробити скріншот")
            if os.path.exists(sc_script): os.remove(sc_script)
        except Exception as e: await msg.edit_text("❌ " + str(e))

    elif text in ["🔒 Заблокувати комп",
                  "📶 Перевірити інтернет","📡 WiFi мережi поруч"]:
        if not is_god(uid): await update.message.reply_text("❌ Тiльки для God"); return
        msg = await update.message.reply_text("⏳ Виконую...")
        cmd_map = {
            "🔒 Заблокувати комп": "rundll32.exe user32.dll,LockWorkStation",
            "📶 Перевірити інтернет": "ping google.com -n 3",
            "📡 WiFi мережi поруч": "netsh wlan show networks",
        }
        cmd = cmd_map.get(text, "echo ok")
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
            try: out = result.stdout.decode("cp866")
            except Exception:
                try: out = result.stdout.decode("cp1251")
                except Exception: out = result.stdout.decode("utf-8", errors="replace")
            audit(uid, "CMD: " + text)
            response = "✅ " + text + " виконано!"
            if out.strip(): response += "\n\n" + out[:300]
            await msg.edit_text(response)
        except Exception as e: await msg.edit_text("❌ " + str(e))

    elif text in ["🔄 Перезапустити ноут","⭕ Вимкнути ноут",
                  "⏰ Вимкнути через 30хв","⏰ Вимкнути через 1год",
                  "⏰ Вимкнути через 2год","🚫 Скасувати вимкнення",
                  "🔄 Перезапустити бота","⏹ Зупинити бота"]:
        if not is_god(uid): await update.message.reply_text("❌ Тiльки для God"); return
        if cmd_pass_cfg.get("password") and uid not in cmd_pass_cfg.get("verified",[]):
            session.waiting_for[uid] = "cmd_password_first"
            session.temp_data[uid] = {"after": "cmd_menu", "pending_action": text}
            await update.message.reply_text("🔐 Введи пароль:")
        else:
            session.waiting_for[uid] = "confirm_action"
            session.temp_data[uid] = {"pending_action": text}
            await update.message.reply_text(
                "⚠️ Пiдтверди: " + text,
                reply_markup=ReplyKeyboardMarkup([["✅ Так","❌ Скасувати"]], resize_keyboard=True))

    elif text == "✅ Так" and session.waiting_for.get(uid) == "confirm_action":
        pending = session.temp_data.get(uid,{}).get("pending_action","")
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        msg = await update.message.reply_text("⏳ Виконую...", reply_markup=cmd_center_kb())
        action_cmds = {
            "🔄 Перезапустити ноут": "shutdown /r /t 30",
            "⭕ Вимкнути ноут": "shutdown /s /t 30",
            "⏰ Вимкнути через 30хв": "shutdown /s /t 1800",
            "⏰ Вимкнути через 1год": "shutdown /s /t 3600",
            "⏰ Вимкнути через 2год": "shutdown /s /t 7200",
            "🚫 Скасувати вимкнення": "shutdown /a",
            "🔄 Перезапустити бота": "start cmd /c taskkill /f /im python.exe & timeout /t 2 & cd /d C:\\Users\\user\\Desktop\\BMSBOT && py jkbms_bot.py",
            "⏹ Зупинити бота": "taskkill /f /im python.exe",
        }
        if pending in action_cmds:
            try:
                subprocess.run(action_cmds[pending], shell=True, timeout=10)
                audit(uid, pending)
                await msg.edit_text("✅ " + pending + " виконано!")
            except Exception as e: await msg.edit_text("❌ " + str(e))

    elif text == "❌ Скасувати" and session.waiting_for.get(uid) == "confirm_action":
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        await update.message.reply_text("Скасовано", reply_markup=cmd_center_kb())

    elif text == "❌ Скасувати" and session.waiting_for.get(uid) == "copy_photo_select":
        # Скасування вибору батарей для копіювання — повертаємо на 3й етап
        td = session.temp_data.get(uid, {})
        akb_num = td.get("copy_photo_from", "")
        stage = WORKSHOP_STAGES[2]  # 3й етап
        session.waiting_for.pop(uid, None)
        session.temp_data.pop(uid, None)
        if akb_num:
            session.temp_data[uid] = {"bms_number": akb_num, "akb_num": akb_num}
            await update.message.reply_text(
                "↩️ Скасовано. Фото не збережено.\n\n📟 АКБ: " + akb_num + "\nЕтап:\n" + stage,
                reply_markup=ReplyKeyboardMarkup([[stage], ["🔙 Назад"]], resize_keyboard=True))
        else:
            await update.message.reply_text("↩️ Скасовано", reply_markup=workshop_kb(uid))

    elif text == "📋 Статус автозапуску":
        if is_god(uid):
            try:
                r = subprocess.run("schtasks /query /tn VoltForgeBMS", shell=True, capture_output=True, timeout=5)
                if r.returncode == 0:
                    await update.message.reply_text("✅ Автозапуск увiмкнено")
                else:
                    await update.message.reply_text("❌ Автозапуск вимкнено")
            except Exception: await update.message.reply_text("❌ Помилка перевiрки")

    elif text == "👥 Всi юзери":
        if is_god(uid):
            clean = {k:v for k,v in users_db.items()
                     if k != str(GOD_ID) and k and k != "None"}
            if not clean: await update.message.reply_text("Немає юзерiв")
            else:
                btns = []
                for uid2,info in clean.items():
                    r = info.get("role","user") if isinstance(info,dict) else info
                    n = info.get("name","") if isinstance(info,dict) else ""
                    if not n or n == uid2: n = uid2
                    btns.append([InlineKeyboardButton(
                        role_icon(r) + " " + n + " — " + r,
                        callback_data="user_" + uid2)])
                await update.message.reply_text(
                    "👥 Всi юзери (" + str(len(clean)) + "):",
                    reply_markup=InlineKeyboardMarkup(btns))

    elif text == "🔐 Пароль":
        if is_god(uid):
            current = "✅ Встановлено" if cmd_pass_cfg.get("password") else "❌ Не встановлено"
            hint = "\n💡 Пiдказка: " + cmd_pass_cfg.get("hint","") if cmd_pass_cfg.get("hint") else ""
            await update.message.reply_text(
                "🔐 Пароль:\n" + current + hint,
                reply_markup=ReplyKeyboardMarkup([
                    ["🔑 Встановити/змiнити пароль"],
                    ["🔓 Скинути пароль"],
                    ["🔙 Назад до налаштувань"]
                ], resize_keyboard=True))

    elif text == "➕ Додати юзера":
        if is_god(uid):
            session.waiting_for[uid] = "add_user"
            await update.message.reply_text("Введи Telegram ID нового юзера:")

    elif text == "❌ Видалити юзера":
        if is_god(uid):
            session.waiting_for[uid] = "del_user"
            await update.message.reply_text("Введи Telegram ID юзера для видалення:")

    elif text == "🔬 Дiагностика BMS":
        if is_god(uid):
            await update.message.reply_text(
                "🔬 Дiагностика BMS — оберiть категорiю:",
                reply_markup=diag_kb())

    elif text == "🔄 Активнi роботи":
        if has_perm(uid,"workshop") or is_god(uid):
            active_nums = get_active_batteries(uid)
            if not active_nums:
                await update.message.reply_text("🔄 Немає активних робiт")
            else:
                items = []
                for num in active_nums:
                    stages = workshop_db.get(num,{}).get("stages",[])
                    user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
                    count = len(user_stages)
                    items.append(("🔄 " + num + " (" + str(count) + "/" + str(len(WORKSHOP_STAGES)) + ")", "ws_" + num))
                session.temp_data[uid] = session.temp_data.get(uid, {})
                session.temp_data[uid]["active_page"] = 0
                await update.message.reply_text(
                    "🔄 Активнi роботи (" + str(len(active_nums)) + "):",
                    reply_markup=make_paginated_inline(items, 0, "wsa_", "активних"))
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "📋 Iсторiя робiт":
        role = get_role(uid)
        completed = {}
        for num, wdata in workshop_db.items():
            stages = wdata.get("stages",[])
            if len(stages) >= len(WORKSHOP_STAGES):
                completed[num] = wdata
        if is_god(uid) or role == "admin":
            my_completed_admin = {}
            for num, wdata in completed.items():
                stages = wdata.get("stages",[])
                user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
                if user_stages:
                    my_completed_admin[num] = wdata
            if not my_completed_admin:
                await update.message.reply_text("📋 Завершених батарей ще немає")
            else:
                items = []
                for num, wdata in my_completed_admin.items():
                    stages = wdata.get("stages",[])
                    user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
                    last = user_stages[-1] if user_stages else {}
                    items.append(("✅ " + num + " | " + last.get("user","") + " | " + last.get("date","")[:10], "ws_" + num))
                session.temp_data[uid] = session.temp_data.get(uid, {})
                session.temp_data[uid]["history_page"] = 0
                await update.message.reply_text(
                    "📋 Завершенi батареї (" + str(len(my_completed_admin)) + "):",
                    reply_markup=make_paginated_inline(items, 0, "wsh_", "завершених"))
        else:
            name = get_name(uid)
            my_completed = {}
            for num, wdata in completed.items():
                stages = wdata.get("stages",[])
                user_stages = [s for s in stages if s.get("user_id") == uid or s.get("user_id") == str(uid)]
                if user_stages:
                    my_completed[num] = wdata
            if my_completed:
                items = []
                for num, wdata in my_completed.items():
                    stages = wdata.get("stages",[])
                    last = stages[-1] if stages else {}
                    items.append(("✅ " + num + " | " + last.get("date","")[:10], "ws_" + num))
                session.temp_data[uid] = session.temp_data.get(uid, {})
                session.temp_data[uid]["history_page"] = 0
                await update.message.reply_text(
                    "📋 Моя iсторiя (" + name + ") — " + str(len(my_completed)) + " батарей:",
                    reply_markup=make_paginated_inline(items, 0, "wsh_", "завершених"))
            else:
                await update.message.reply_text("📋 " + name + ", завершених батарей ще немає")

    elif text in WORKSHOP_STAGES:
        # Натиснули кнопку етапу без активної сесії - шукаємо активну батарею
        if has_perm(uid,"workshop") or is_god(uid):
            active = get_active_batteries(uid)
            if active:
                # Беремо тільки з сесії — щоб не підтягнути чужу батарею
                num = session.temp_data.get(uid, {}).get("bms_number")
                if not num or num not in workshop_db:
                    await update.message.reply_text("❌ Зайди в батарею через кабiнет i натисни етап звiдти")
                    return
                stages = workshop_db.get(num,{}).get("stages",[])
                stage_idx = WORKSHOP_STAGES.index(text)
                # Перевіряємо порядок
                prev_ok = True
                for prev_idx in range(stage_idx):
                    prev_stage = WORKSHOP_STAGES[prev_idx]
                    if not next((s for s in stages if s.get("stage") == prev_stage), None):
                        prev_ok = False
                        await update.message.reply_text("⚠️ Спочатку треба пройти:\n" + prev_stage)
                        break
                if prev_ok:
                    session.temp_data[uid] = {"bms_number": num, "akb_num": num}
                    if text == "6️⃣ Контроль якостi":
                        session.waiting_for.pop(uid, None)
                        cl_text = "6️⃣ Контроль якостi батареї №" + num + "\n\nПеревiр кожен пункт перед здачею:\n\n" + "\n".join(QUALITY_CHECKLIST)
                        await update.message.reply_text(
                            cl_text,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                "✅ Ознайомився, знiмаю вiдео",
                                callback_data="qcvideo_" + num)]]))
                    else:
                        needs_two = text in WORKSHOP_TWO_PHOTOS
                        session.temp_data[uid]["stage"] = text
                        session.temp_data[uid]["photo_num"] = 1
                        session.waiting_for[uid] = "workshop_photo"
                        hint = "\n📸 Потрiбно 2 фото!\nНадiшли ПЕРШЕ фото" if needs_two else "\n📸 Надiшли фото"
                        await update.message.reply_text(
                            "Етап: " + text + hint,
                            reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True))
            else:
                await update.message.reply_text("❌ Немає активних батарей")

    elif text.startswith("🔋 ") and "(" in text and "/" in text:
        # Натиснули на активну батарею
        if has_perm(uid,"workshop") or is_god(uid):
            num = text.split(" ")[1]  # "🔋 34137 (2/6)" -> "34137"
            # Перевірка що номер існує в БД
            if not num or num not in workshop_db:
                await update.message.reply_text(
                    "❌ Батарея не знайдена!\nСпочатку введи номер BMS через ➕ Нова батарея")
                return
            stages_done = workshop_db.get(num,{}).get("stages",[])
            user_stages = [s for s in stages_done if s.get("user_id") == uid]
            next_idx = len(user_stages)
            if next_idx >= len(WORKSHOP_STAGES):
                await update.message.reply_text("✅ Всi етапи завершено для АКБ " + num)
            else:
                next_stage = WORKSHOP_STAGES[next_idx]
                session.temp_data[uid] = {"bms_number": num, "akb_num": num}
                # Якщо наступний етап - Контроль якості, одразу показуємо чеклист
                if next_stage == "6️⃣ Контроль якостi":
                    session.waiting_for.pop(uid, None)
                    cl_text = "6️⃣ Контроль якостi батареї №" + num + "\n\nПеревiр кожен пункт перед здачею:\n\n" + "\n".join(QUALITY_CHECKLIST)
                    await update.message.reply_text(
                        cl_text,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "✅ Ознайомився, знiмаю вiдео",
                            callback_data="qcvideo_" + num)]]))
                else:
                    session.waiting_for[uid] = "workshop_stage"
                    needs_two = next_stage in WORKSHOP_TWO_PHOTOS
                    hint = " (потрiбно 2 фото)" if needs_two else ""
                    await update.message.reply_text(
                        "📟 АКБ: " + num + "\nНаступний етап:\n" + next_stage + hint,
                        reply_markup=ReplyKeyboardMarkup([[next_stage],["🔙 Назад до кабiнету"]], resize_keyboard=True))

    elif text == "🔎 Знайти батарею":
        if has_perm(uid,"workshop") or is_god(uid):
            # Показуємо тільки АКТИВНІ (незавершені) батареї
            active = {num: wdata for num, wdata in workshop_db.items()
                      if len(wdata.get("stages",[])) < len(WORKSHOP_STAGES)}
            if not active:
                await update.message.reply_text("📋 Немає активних батарей в роботi")
            else:
                btns = []
                for num, wdata in active.items():
                    stages = wdata.get("stages",[])
                    total = len(stages)
                    last_user = stages[-1].get("user","") if stages else ""
                    btns.append([InlineKeyboardButton(
                        "🔄 " + num + " | " + str(total) + "/" + str(len(WORKSHOP_STAGES)) + " | " + last_user,
                        callback_data="ws_" + num)])
                await update.message.reply_text(
                    "🔎 Активнi батареї (" + str(len(active)) + "):",
                    reply_markup=InlineKeyboardMarkup(btns))
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "➕ Нова батарея":
        if has_perm(uid,"workshop") or is_god(uid):
            session.waiting_for[uid] = "ws_bms_number"
            await update.message.reply_text("📟 Введи номер BMS батареї:")
        else: await update.message.reply_text("❌ Немає прав")

    # ── BMS ──
    elif text == "🔍 Знайти BMS": await cmd_scan(update, ctx)

    elif text == "📡 Статус":
        conn = "✅ Пiдключено" if (state.client and state.client.is_connected) else "❌ Не пiдключено"
        kl = "🔄 Keepalive активний" if (state.keepalive_task and not state.keepalive_task.done()) else "⚠️ Keepalive вимкнено"
        info = f"\n🔋 {state.bms_name}" if state.bms_name else ""
        info += f"\n📍 {state.bms_address}" if state.bms_address else ""
        await update.message.reply_text(f"{conn}{info}\n{kl}")

    elif text == "📈 Стан батареї":
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Не пiдключено"); return
        msg = await update.message.reply_text("⏳ Читаю данi...")
        d = await read_bms_data()
        if d:
            audit(uid, "Читав стан батареї")
            await msg.edit_text(format_status(d))
        else: await msg.edit_text("❌ Не вдалось прочитати данi")

    elif text == "🔬 Ячейки":
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Не пiдключено"); return
        msg = await update.message.reply_text("⏳ Читаю ячейки...")
        d = await read_bms_data()
        if d:
            audit(uid, "Читав ячейки")
            await msg.edit_text(format_cells(d))
        else: await msg.edit_text("❌ Не вдалось прочитати данi")

    elif text == "🌡 Температура BMS":
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Не пiдключено"); return
        msg = await update.message.reply_text("⏳ Читаю температуру...")
        d = await read_bms_data()
        if d:
            t = "🌡 Температура:\n\n"
            if "temp_mos" in d: t += f"🔧 MOS: {d['temp_mos']}C\n"
            if "temp1" in d: t += f"Датчик 1: {d['temp1']}C\n"
            if "temp2" in d: t += f"Датчик 2: {d['temp2']}C\n"
            await msg.edit_text(t)
        else: await msg.edit_text("❌ Не вдалось")

    elif text == "🔴 Вимкнути батарею":
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Не пiдключено"); return
        if not (has_perm(uid,"bms") or is_god(uid)):
            await update.message.reply_text("❌ Немає прав"); return
        btns = ReplyKeyboardMarkup([["✅ Так, вимкнути","❌ Скасувати"]], resize_keyboard=True)
        session.waiting_for[uid] = "confirm_power_off"
        await update.message.reply_text("⚠️ Впевнений що хочеш вимкнути батарею?\n\nПiсля вимкнення увiмкнути можна тiльки фiзично!", reply_markup=btns)

    elif text == "✅ Так, вимкнути" and session.waiting_for.get(uid) == "confirm_power_off":
        session.waiting_for.pop(uid,None)
        msg = await update.message.reply_text("⏳ Вимикаю батарею...", reply_markup=bms_kb())
        try:
            # Команда вимкнення MOS
            off_cmd = build_write(0x9D, 0, "uint8")
            await state.client.write_gatt_char(JK_CHAR_UUID, off_cmd, response=True)
            audit(uid, "Вимкнув батарею", state.bms_address or "")
            await msg.edit_text("✅ Батарею вимкнено!\n\n⚠️ Для увiмкнення натисни кнопку на батареї фiзично")
        except Exception as e:
            await msg.edit_text(f"❌ Помилка: {e}")

    elif text == "❌ Скасувати" and session.waiting_for.get(uid) == "confirm_power_off":
        session.waiting_for.pop(uid,None)
        await update.message.reply_text("Скасовано", reply_markup=bms_kb())

    elif text == "🔌 Вiдключитись":
        if state.keepalive_task: state.keepalive_task.cancel(); state.keepalive_task = None
        if state.client and state.client.is_connected:
            await state.client.disconnect(); state.bms_address = None; state.bms_name = None
            audit(uid, "Вiдключився вiд BMS")
            await update.message.reply_text("🔌 Вiдключено вiд BMS")
        else: await update.message.reply_text("BMS не пiдключена")

    # ── ПРЕСЕТИ ──
    elif text == "📋 Пресети":
        await update.message.reply_text("📋 Пресети та налаштування:", reply_markup=presets_kb(uid))

    elif text in ["🔋 BMS 100A","🔋 BMS 150A"]:
        model = "100" if text == "🔋 BMS 100A" else "150"
        session.temp_data[uid] = session.temp_data.get(uid, {})
        session.temp_data[uid]["bms_model"] = model
        await update.message.reply_text(
            f"🔋 BMS {model}A — оберiть дiю:",
            reply_markup=preset_model_kb(model))

    elif text == "🔙 Назад до пресетiв":
        session.temp_data[uid] = {}
        await update.message.reply_text("📋 Пресети та налаштування:", reply_markup=presets_kb(uid))



    elif text == "📥 Завод .jkcfg":
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
            return
        cfg = load_factory_jkcfg()
        if not cfg:
            await update.message.reply_text("❌ Файл 150bms.jkcfg не знайдено!")
            return
        msg = await update.message.reply_text("📥 Вiдправляю файл в BMS...")
        try:
            chunk_size = 20
            total = len(cfg)
            sent = 0
            for i in range(0, total, chunk_size):
                chunk = cfg[i:i+chunk_size]
                await state.client.write_gatt_char(JK_CHAR_UUID, chunk, response=True)
                sent += len(chunk)
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.0)
            now = datetime.now()
            yr = now.year - 2000
            hdr = bytes([0xAA,0x55,0x90,0xEB])
            time_pl = bytes([0x6B, 0x04, yr, now.month, now.day,
                             now.hour, now.minute, now.second,
                             0,0,0,0,0,0])
            time_cmd = hdr + time_pl + bytes([sum(hdr+time_pl) & 0xFF])
            await state.client.write_gatt_char(JK_CHAR_UUID, time_cmd, response=True)
            audit(uid, "Завантажено 150bms.jkcfg в BMS")
            await msg.edit_text(f"✅ Файл вiдправлено {sent}/{total} байт\n🕐 {now.strftime('%d.%m.%Y %H:%M:%S')}")
        except Exception as e:
            await msg.edit_text(f"❌ Помилка: {str(e)[:200]}")

    elif text == "🏭 Записати заводськi в BMS":
        model = session.temp_data.get(uid, {}).get("bms_model", "100")
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Спочатку пiдключись до BMS!")
            return
        preset = DEFAULT_PRESET_150 if model == "150" else DEFAULT_PRESET
        msg = await update.message.reply_text(f"⚙️ Записую заводськi параметри BMS {model}A...")
        # Використовуємо PARAM_MAP для запису
        # Адреси перевірені з реального пакету 55 AA EB 90 01 (06.05.2026)
        PARAM_WRITE_MAP = [
            ("ovpr",      0x00, "uint16mv"),  # Cell OVPR
            ("uvp",       0x01, "uint16mv"),  # Cell UVP
            ("uvpr",      0x02, "uint16mv"),  # Cell UVPR
            ("ovp",       0x03, "uint16mv"),  # Cell OVP
            ("bal_start", 0x04, "uint16mv"),  # Balance Start Volt
            ("bal_delta", 0x05, "uint16mv"),  # Balance Trig. Volt
            ("soc100",    0x06, "uint16mv"),  # SOC 100%
            ("soc0",      0x07, "uint16mv"),  # SOC 0%
            ("rcv_volt",  0x08, "uint16mv"),  # Vol Cell RCV
            ("rfv_volt",  0x09, "uint16mv"),  # Vol Cell RFV
            ("pwr_off",   0x0A, "uint16mv"),  # Power Off Volt
            ("chg_oc",    0x0B, "uint16ma"),  # Continued Charge Curr
            ("dchg_oc",   0x0C, "uint16ma"),  # Continued Discharge Curr
            ("chg_ot",    0x0D, "uint16t"),   # Charge OTP
            ("chg_ot_r",  0x0E, "uint16t"),   # Charge OTPR
            ("chg_ut",    0x0F, "uint16t"),   # Charge UTP
            ("chg_ut_r",  0x10, "uint16t"),   # Charge UTPR
            ("dchg_ot",   0x11, "uint16t"),   # Discharge OTP
            ("dchg_ot_r", 0x12, "uint16t"),   # Discharge OTPR
            ("mos_ot",    0x13, "uint16t"),   # MOS OTP
            ("mos_ot_r",  0x14, "uint16t"),   # MOS OTPR
            ("sleep_volt",0x1F, "uint16mv"),  # Vol Smart Sleep
            ("sleep_time",0x20, "uint16"),    # Time Smart Sleep
        ]
        import struct
        hdr = bytes([0xAA,0x55,0x90,0xEB])
        ok = 0; total = len(PARAM_WRITE_MAP)
        for pkey, addr, dtype in PARAM_WRITE_MAP:
            if pkey in preset:
                raw = preset[pkey]["val"]
                if dtype == "uint16mv":
                    vb = struct.pack("<I", int(round(float(raw) * 1000)))
                elif dtype == "uint16ma":
                    vb = struct.pack("<I", int(round(float(raw) * 1000)))
                elif dtype == "uint16t":
                    vb = struct.pack("<I", int(round(float(raw) * 10)) & 0xFFFFFFFF)
                elif dtype == "uint16":
                    vb = struct.pack("<I", int(raw))
                else:
                    vb = struct.pack("<I", int(round(float(raw) * 1000)))
                pl = bytes([addr, 0x04]) + vb + bytes(9)
                cmd = hdr + pl + bytes([sum(hdr+pl) & 0xFF])
                try:
                    await state.client.write_gatt_char(JK_CHAR_UUID, cmd, response=True)
                    await asyncio.sleep(0.3)
                    ok += 1
                except: pass
        # OK команда
        ok_pl = bytes([0x68,0,0,0,0,0,0,0,0,0,0,0,0,0,0])
        ok_cmd = hdr + ok_pl + bytes([sum(hdr+ok_pl) & 0xFF])
        await state.client.write_gatt_char(JK_CHAR_UUID, ok_cmd, response=True)
        state.device_params = {}  # Скидаємо кеш щоб наступного разу перечитало
        audit(uid, f"Записано заводськi BMS {model}A", f"{ok}/{total}")
        await msg.edit_text(
            f"✅ Записано {ok}/{total} параметрiв BMS {model}A!",
            reply_markup=None)
        await update.message.reply_text(
            f"🔋 BMS {model}A:", reply_markup=preset_model_kb(model))

    elif text == "🏭 Заводськi параметри — записати":
        if not state.client or not state.client.is_connected:
            await update.message.reply_text("❌ Спочатку пiдключись до BMS!"); return
        if not (has_perm(uid,"bms") or is_god(uid)):
            await update.message.reply_text("❌ Немає прав"); return
        msg = await update.message.reply_text("⚙️ Записую заводськi параметри...")
        ok = total = 0
        for p,info in MY_PRESET.items():
            if p in PARAMS_ADDR:
                total += 1
                if await write_param(p, info["val"]): ok += 1
                await asyncio.sleep(0.2)
        audit(uid, "Записано заводськi параметри", f"{ok}/{total}")
        await msg.edit_text(
            f"✅ Заводськi параметри записано!\n{ok}/{total} параметрiв\n\n"
            "🔋 NMC 7S 60Ah\n"
            "⚡️ OVP:%.3fV | UVP:%.3fV\n"
            "🔌 Заряд:%.0fA | Розряд:%.0fA" % (
                MY_PRESET["ovp"]["val"], MY_PRESET["uvp"]["val"],
                MY_PRESET["chg_oc"]["val"], MY_PRESET["dchg_oc"]["val"]))

    elif text == "✏️ Змiнити заводськi параметри":
        if not (has_perm(uid,"bms") or is_god(uid)) or get_role(uid) == "user":
            await update.message.reply_text("❌ Тiльки адмiн або God"); return
        # Зчитуємо реальні дані з BMS якщо підключено
        bms_vals = {}
        if state.client and state.client.is_connected:
            try:
                d = await read_bms_data()
                if d:
                    bms_vals = d
            except: pass
        # Формуємо кнопки з реальними значеннями
        def bms_val(key, default):
            if key in bms_vals: return f"{bms_vals[key]:.3f}"
            return str(MY_PRESET.get(key,{}).get("val",default))
        await update.message.reply_text("✏️ Змiна заводських параметрiв\nОберiть групу:",
            reply_markup=ReplyKeyboardMarkup([
                ["⚡️ Напруга","⚖️ Балансування"],
                ["🔌 Струм","🌡 Температура"],
                ["📋 Основнi пресету","🔙 Назад"],
            ], resize_keyboard=True))

    elif text in ["⚡️ Напруга","⚖️ Балансування","🔌 Струм","🌡 Температура","📋 Основнi пресету"]:
        gmap = {"⚡️ Напруга":"voltage","⚖️ Балансування":"balance","🔌 Струм":"current","🌡 Температура":"temp","📋 Основнi пресету":"basic"}
        gk = gmap[text]; session.temp_data[uid] = {"group":gk}
        g = PARAM_GROUPS[gk]
        params = g["params"]
        # Зчитуємо реальні дані з BMS
        msg = await update.message.reply_text("⏳ Зчитую реальнi даннi з BMS...")
        real_vals = await read_device_info_params()
        # Будуємо кнопки з реальними значеннями
        btns = []
        for p in params:
            if p in MY_PRESET:
                pr = MY_PRESET[p]
                val_str = format_param_val(p, real_vals[p]) if p in real_vals else format_param_val(p, pr['val'])
                btns.append([InlineKeyboardButton(
                    f"✏️ {pr['desc']}: {val_str} {pr['unit']}",
                    callback_data=f"ep_{p}_{gk}")])
        btns.append([InlineKeyboardButton("💾 Записати в BMS", callback_data=f"wr_{gk}"),
                     InlineKeyboardButton("🔙 Назад", callback_data="bp")])
        title = f"✏️ {g['name']} — реальнi данi з BMS:" if real_vals else f"✏️ {g['name']} (з пресету):"
        await msg.edit_text(title, reply_markup=InlineKeyboardMarkup(btns))

    # ── БАЗА АКБ ──
    elif text == "🔋 Додати АКБ":
        if not (has_perm(uid,"akb_add") or has_perm(uid,"akb") or is_god(uid)):
            await update.message.reply_text("❌ Немає прав"); return
        session.waiting_for[uid] = "akb_bms_number"; session.temp_data[uid] = {}
        await update.message.reply_text("📸 Введи номер BMS:")
    elif text == "📅 Звiти":
        if has_perm(uid,"akb_reports") or is_god(uid):
            await update.message.reply_text("📅 Звiти по базi АКБ:", reply_markup=akb_reports_kb())
        else: await update.message.reply_text("❌ Немає прав")

    elif text == "🔙 Назад до АКБ":
        await update.message.reply_text(
            "📍 Роздiл: База АКБ\n\n🗄 Записiв: " + str(len(akb_db)),
            reply_markup=akb_kb(uid))

    elif text == "📅 За сьогоднi":
        today = datetime.now().strftime("%d.%m.%Y")
        res = []
        for num,rec in akb_db.items():
            hist = rec.get("history",[])
            date_val = (hist[-1].get("date","") if hist else rec.get("date",""))[:10]
            if date_val == today:
                res.append((num,rec))
        if not res:
            await update.message.reply_text("📅 Сьогодні записів немає")
        else:
            btns = []
            for num,rec in res:
                hist = rec.get("history",[])
                last_status = hist[-1].get("status","") if hist else rec.get("status","")
                icon = "✅" if last_status=="справний" else ("❌" if last_status=="бракований" else ("🔍" if last_status=="на перевiрцi" else "❓"))
                btns.append([InlineKeyboardButton(f"{icon} {num}", callback_data=f"akb_{num}")])
            await update.message.reply_text(f"📅 Сьогоднi ({len(res)}):", reply_markup=InlineKeyboardMarkup(btns))

    elif text in ["📅 За цей мiсяць","📅 За минулий мiсяць","📅 За квартал","📅 За рiк"]:
        from datetime import date, timedelta
        today = date.today()
        def get_date_str(rec):
            hist = rec.get("history",[])
            return (hist[-1].get("date","") if hist else rec.get("date",""))[:10]
        if text == "📅 За цей мiсяць":
            label = "Цей мiсяць " + today.strftime("%m.%Y")
            month_str = today.strftime("%m.%Y")
            filter_fn = lambda d: d[3:10] == month_str
        elif text == "📅 За минулий мiсяць":
            first = today.replace(day=1)
            last_month = first - timedelta(days=1)
            label = "Минулий мiсяць " + last_month.strftime("%m.%Y")
            month_str = last_month.strftime("%m.%Y")
            filter_fn = lambda d: d[3:10] == month_str
        elif text == "📅 За квартал":
            q_start = today.month - (today.month - 1) % 3
            months = [str(m).zfill(2) + "." + str(today.year) for m in range(q_start, min(q_start+3, 13))]
            label = "Квартал Q" + str((today.month-1)//3+1) + " " + str(today.year)
            filter_fn = lambda d: any(d[3:10] == m for m in months)
        elif text == "📅 За рiк":
            label = "Рiк " + str(today.year)
            year_str = str(today.year)
            filter_fn = lambda d: d[6:10] == year_str
        res = []
        for num, rec in akb_db.items():
            hist = rec.get("history",[])
            date_str = (hist[-1].get("date","") if hist else rec.get("date",""))[:10]
            if filter_fn(date_str):
                res.append((num, rec))
        if not res:
            await update.message.reply_text("📅 " + label + ": записiв немає")
        else:
            btns = []
            for num, rec in res:
                hist = rec.get("history",[])
                last = hist[-1] if hist else {}
                status = last.get("status","") if hist else rec.get("status","")
                icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
                btns.append([InlineKeyboardButton(icon + " " + num, callback_data="akb_" + num)])
            await update.message.reply_text(
                "📅 " + label + " (" + str(len(res)) + "):",
                reply_markup=InlineKeyboardMarkup(btns))

    elif text == "📅 Свiй перiод":
        session.waiting_for[uid] = "akb_custom_period"
        await update.message.reply_text(
            "📅 Введи перiод у форматi:\nвiд до\nНаприклад: 01.04.2026 30.04.2026")

    elif text == "🔎 Знайти АКБ":
        await update.message.reply_text(
            "🔎 Пошук АКБ:",
            reply_markup=ReplyKeyboardMarkup([
                ["🔍 По номеру"],
                ["✅ Тiльки справнi","❌ Тiльки брак."],
                ["🔙 Назад"],
            ], resize_keyboard=True))

    elif text == "🔍 По номеру":
        session.waiting_for[uid] = "akb_search"
        await update.message.reply_text("🔎 Введи номер БМС:")

    elif text in ["✅ Тiльки справнi","❌ Тiльки брак."]:
        status_filter = "справний" if text == "✅ Тiльки справнi" else "бракований"
        res = []
        for num,rec in akb_db.items():
            hist = rec.get("history",[])
            last_status = hist[-1].get("status","") if hist else rec.get("status","")
            if last_status == status_filter:
                res.append((num,rec))
        if not res:
            await update.message.reply_text(f"❌ Немає батарей зi статусом '{status_filter}'")
        else:
            btns = []
            for num,rec in res[-20:]:
                hist = rec.get("history",[])
                date = hist[-1].get("date","")[:10] if hist else rec.get("date","")[:10]
                count = len(hist) if hist else len(rec.get("photos",[]))
                icon = "✅" if status_filter=="справний" else "❌"
                btns.append([InlineKeyboardButton(f"{icon} {num} | {date} | 📸{count}", callback_data=f"akb_{num}")])
            await update.message.reply_text(
                f"{'✅ Справнi' if status_filter=='справний' else '❌ Брак.'} ({len(res)}):",
                reply_markup=InlineKeyboardMarkup(btns))
    elif text == "📁 Всi АКБ":
        session.waiting_for.pop(uid,None); session.temp_data.pop(uid,None)
        if not akb_db: await update.message.reply_text("🗄 База порожня")
        else:
            items = []
            for num,rec in list(akb_db.items()):
                hist = rec.get("history",[])
                if hist:
                    last = hist[-1]
                    status = last.get("status","")
                    date = last.get("date","")[:10]
                    count = len(hist)
                else:
                    status = rec.get("status","")
                    date = rec.get("date","")[:10]
                    count = len(rec.get("photos",[]))
                icon = "✅" if status=="справний" else ("❌" if status=="бракований" else ("🔍" if status=="на перевiрцi" else "❓"))
                items.append((f"{icon} {num} | {date} | 📸{count}", f"akb_{num}"))
            session.temp_data[uid] = session.temp_data.get(uid, {})
            session.temp_data[uid]["akb_list_items"] = items
            session.temp_data[uid]["akb_list_page"] = 0
            await update.message.reply_text(
                f"📋 Всi АКБ ({len(akb_db)}):",
                reply_markup=make_paginated_inline(items, 0, "akbl_", "АКБ"))
    elif text == "📑 Експорт Excel":
        if has_perm(uid,"akb_export") or is_god(uid):
            await update.message.reply_text(
                "📊 Експорт Excel — оберi перiод:",
                reply_markup=ReplyKeyboardMarkup([
                    ["📊 Excel за тиждень","📊 Excel за мiсяць"],
                    ["📊 Excel за минулий мiсяць","📊 Excel за квартал"],
                    ["📊 Excel за рiк","📊 Excel весь"],
                    ["📊 Excel свiй перiод","🔙 Назад до АКБ"],
                ], resize_keyboard=True))
        else: await update.message.reply_text("❌ Немає прав")

    elif text in ["📊 Excel за тиждень","📊 Excel за мiсяць","📊 Excel за минулий мiсяць",
                  "📊 Excel за квартал","📊 Excel за рiк","📊 Excel весь"]:
        if not (has_perm(uid,"akb_export") or is_god(uid)):
            await update.message.reply_text("❌ Тiльки адмiн або God"); return
        from datetime import date, timedelta
        today = date.today()
        if text == "📊 Excel за тиждень":
            label = "Тиждень"
            week_ago = today - timedelta(days=7)
            filter_fn = lambda d: datetime.strptime(d[:10], "%d.%m.%Y").date() >= week_ago if len(d) >= 10 else False
        elif text == "📊 Excel за мiсяць":
            label = "Мiсяць " + today.strftime("%m.%Y")
            m = today.strftime("%m.%Y")
            filter_fn = lambda d: d[3:10] == m
        elif text == "📊 Excel за минулий мiсяць":
            from datetime import timedelta
            first = today.replace(day=1)
            last_month = first - timedelta(days=1)
            label = "Минулий мiсяць " + last_month.strftime("%m.%Y")
            m_prev = last_month.strftime("%m.%Y")
            filter_fn = lambda d: d[3:10] == m_prev
        elif text == "📊 Excel за квартал":
            label = "Квартал"
            q_start = today.month - (today.month-1)%3
            months = [str(m).zfill(2)+"."+str(today.year) for m in range(q_start, min(q_start+3,13))]
            filter_fn = lambda d: any(d[3:10]==m for m in months)
        elif text == "📊 Excel за рiк":
            label = "Рiк " + str(today.year)
            filter_fn = lambda d: d[6:10] == str(today.year)
        else:
            label = "Весь перiод"
            filter_fn = lambda d: True
        # Генеруємо Excel
        if not (has_perm(uid,"akb") or is_god(uid)): return
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = label
            ws.append(["BMS номер","Дата","Статус","Коментар","Записiв","Хто"])
            count = 0
            for num, rec in akb_db.items():
                hist = rec.get("history",[])
                if hist:
                    last = hist[-1]
                    date_val = last.get("date","")[:10]
                    status = last.get("status","")
                    comment = last.get("comment","")
                    user = last.get("user","")
                    cnt = len(hist)
                else:
                    date_val = rec.get("date","")[:10]
                    status = rec.get("status","")
                    comment = rec.get("comment","")
                    user = ""
                    cnt = len(rec.get("photos",[]))
                if filter_fn(date_val):
                    ws.append([num, date_val, status, comment, cnt, user])
                    count += 1
            ts = datetime.now().strftime("%d%m%Y_%H%M")
            fname = "akb_export_" + ts + ".xlsx"
            wb.save(fname)
            audit(uid, "Експорт Excel: " + label + " (" + str(count) + " записiв)")
            await update.message.reply_document(
                open(fname,"rb"),
                filename=fname,
                caption="📊 Excel: " + label + " — " + str(count) + " батарей")
            if os.path.exists(fname): os.remove(fname)
        except Exception as e:
            await update.message.reply_text("❌ Помилка: " + str(e))

    elif text == "📊 Excel свiй перiод":
        if has_perm(uid,"akb_export") or is_god(uid):
            session.waiting_for[uid] = "excel_custom_period"
            await update.message.reply_text("📊 Введи перiод:\nНаприклад: 01.04.2026 30.04.2026")

async def cmd_unknown(update, ctx):
    await update.message.reply_text("Не зрозумiв. Натисни ❔ Допомога", reply_markup=main_kb(update.effective_user.id))

async def daily_reminder_loop(app):
    """Щоденне нагадування о 9:00 про непідтверджені виплати."""
    import asyncio
    from datetime import timedelta
    while True:
        now = datetime.now()
        # Рахуємо скільки секунд до наступної 9:00
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        await asyncio.sleep(wait_sec)
        # Відправляємо нагадування всім юзерам з непідтвердженими виплатами
        try:
            for u_id, sdata in salary_db.items():
                records = sdata.get("records", [])
                pending = [r for r in records if not r.get("confirmed")]
                if pending:
                    total_pending = sum(r["amount"] for r in pending)
                    try:
                        await app.bot.send_message(
                            chat_id=int(u_id),
                            text=f"💰 Нагадування!\n\nУ тебе є непiдтвердженi нарахування ({len(pending)}) на суму {total_pending} грн.\nПiдтвердь в роздiлi 💰 Мої нарахування → 📋 Моя iсторiя виплат"
                        )
                    except: pass
        except: pass

async def web_server():
    from aiohttp import web
    app = web.Application()
    async def handle(request):
        return web.Response(text="OK")
    app.router.add_get("/", handle)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server started on port {port}")

def main():
    print("VoltForge BMS v15.1 zapushen!")
    from telegram.ext import ApplicationBuilder
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_photo))
    app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & ~filters.PHOTO, cmd_unknown))

    async def post_init(application):
        import asyncio
        asyncio.create_task(daily_reminder_loop(application))
        asyncio.create_task(web_server())

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
