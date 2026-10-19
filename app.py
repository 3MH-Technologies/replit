# -*- coding: utf-8 -*-
"""
HostBot — لوحة بسيطة لرفع وتشغيل ملفات Python.

الفكرة كلها:
    دخول أدمن  →  ارفع ملف .py  →  شغّل  →  تابع السجلات.

كل شيء يُحفظ في مجلد BOTS/ المجاور لهذا الملف:
    BOTS/<اسم البوت>/meta.json    الملف الرئيسي + التشغيل التلقائي
    BOTS/<اسم البوت>/server.log   سجل التشغيل
    BOTS/<اسم البوت>/...          ملفات البوت التي رفعتها

متغيرات البيئة:
    ADMIN_USER   اسم الأدمن (افتراضي: admin)
    ADMIN_PASS   كلمة المرور (افتراضي: admin — عيّنها قبل النشر!)
    PORT         المنفذ (افتراضي: 7860)
    SECRET_KEY   مفتاح جلسات اختياري (يُشتق تلقائيًا إن لم يوجد)

    --- الاستمرارية وتوزيع الضغط ---
    BOT_NICE            أولوية البوتات (افتراضي 10 — أقل أولوية من اللوحة)
    MONITOR_INTERVAL    كل كم ثانية يفحص المراقب البوتات (افتراضي 15)
    RESTART_DELAY_BASE  أول مهلة انتظار قبل إعادة تشغيل بوت خرج سريعًا (افتراضي 3)
    RESTART_DELAY_MAX   أقصى مهلة بين محاولات إعادة التشغيل (افتراضي 60)
    KEEPALIVE_URL       رابط عام اختياري (مثل رابط Space) لنبضة الحياة
    KEEPALIVE_INTERVAL  كل كم ثانية تُرسل النبضة (افتراضي 240)
    MAX_BOT_MEM_MB      حد ذاكرة لكل بوت بالميغابايت (0 = بلا حد)
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from functools import wraps

import psutil
from flask import (Flask, jsonify, redirect, request, send_from_directory,
                   session)

# ---------------------------------------------------------------- الإعدادات

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR = os.path.join(BASE_DIR, "BOTS")
RUNNER = os.path.join(BASE_DIR, "runner.py")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
PORT = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT") or 7860)

# --- الاستمرارية وتوزيع الضغط ---
BOT_NICE = int(os.environ.get("BOT_NICE", "10"))                  # أولوية البوتات (أقل = أهم)
MONITOR_INTERVAL = max(3, int(os.environ.get("MONITOR_INTERVAL", "15")))  # فحص كل كم ثانية
STABLE_SECONDS = int(os.environ.get("STABLE_SECONDS", "120"))     # اعتبار البوت مستقرًا بعد
FAIL_WINDOW = 600                                                 # نافذة حساب الأعطال (10 دقائق)
RESTART_DELAY_BASE = max(1, int(os.environ.get("RESTART_DELAY_BASE", "3")))   # أول مهلة قبل إعادة تشغيل بوت خرج سريعًا
RESTART_DELAY_MAX = max(10, int(os.environ.get("RESTART_DELAY_MAX", "60")))   # أقصى مهلة بين محاولات الإعادة
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL", "").strip()       # رابط عام اختياري للنكزة
KEEPALIVE_INTERVAL = max(60, int(os.environ.get("KEEPALIVE_INTERVAL", "240")))

MAX_LOG_BYTES = 2 * 1024 * 1024     # يُقصّ السجل عند تجاوز هذا الحجم
LOG_TAIL_BYTES = 128 * 1024         # ما يُعرض في الكونسول
MAX_EDIT_BYTES = 256 * 1024         # أقصى حجم ملف قابل للتحرير
LOGIN_LIMIT = 7                     # محاولات دخول مسموحة
LOGIN_WINDOW = 300                  # خلال هذه الثواني

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hostbot")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    f"hostbot::{ADMIN_USER}::{ADMIN_PASS}".encode()
).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=7 * 24 * 3600,
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,
)
os.makedirs(BOTS_DIR, exist_ok=True)

PROCS = {}           # اسم البوت -> {"proc": Popen, "started": timestamp}
PROCS_LOCK = threading.Lock()
BOT_OP_LOCK = threading.RLock()   # يمنع تشغيل/إيقاف نفس البوت من خيطين في نفس اللحظة
EXITS = {}           # اسم البوت -> {"at", "started", "code"} آخر خروج
LOGIN_LOCK = threading.Lock()
LOGIN_FAILS = {}     # IP -> [أوقات المحاولات الخاطئة]


# ---------------------------------------------------------------- أدوات عامة

def fail(message, code=400):
    return jsonify({"success": False, "message": message}), code


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return fail("يجب تسجيل الدخول", 401)
        return fn(*args, **kwargs)
    return wrapper


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def sanitize_name(name):
    """اسم البوت → مجلد آمن."""
    name = (name or "").strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name)
    name = name.strip("-_")
    return name[:40]


def bot_dir(name):
    """يرجع مسار مجلد البوت إن كان صالحًا وموجودًا، وإلا None."""
    clean = sanitize_name(name)
    if not clean or clean != (name or "").strip():
        return None
    path = os.path.join(BOTS_DIR, clean)
    return path if os.path.isdir(path) else None


def safe_path(bot_path, rel):
    """يمنع أي خروج عن مجلد البوت (path traversal)."""
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    root = os.path.abspath(bot_path)
    joined = os.path.abspath(os.path.join(root, rel))
    if joined != root and not joined.startswith(root + os.sep):
        raise ValueError("مسار غير صالح")
    return joined


def read_meta(name):
    path = os.path.join(BOTS_DIR, name, "meta.json")
    meta = {"name": name, "main": "", "autorun": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f) or {}
        if isinstance(loaded, dict):
            meta.update(loaded)         # نحتفظ بحقول إضافية مثل عدّاد الأعطال
    except Exception:
        pass
    meta["name"] = name
    meta["main"] = str(meta.get("main") or "")
    meta["autorun"] = bool(meta.get("autorun"))
    return meta


def write_meta(name, meta):
    path = os.path.join(BOTS_DIR, name, "meta.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def log_append(name, text):
    try:
        with open(os.path.join(BOTS_DIR, name, "server.log"), "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _pid_path(name):
    return os.path.join(BOTS_DIR, name, ".pid")


def write_pid(name, pid):
    try:
        with open(_pid_path(name), "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        pass


def clear_pid(name):
    try:
        os.remove(_pid_path(name))
    except OSError:
        pass


class AdoptedProcess:
    """عملية بوت ما زالت تعمل من جلسة سابقة (أُعيد تشغيل اللوحة وحدها دون الحاوية)."""

    def __init__(self, proc):
        self._proc = proc
        self.pid = proc.pid

    def poll(self):
        try:
            if self._proc.is_running() and self._proc.status() != psutil.STATUS_ZOMBIE:
                return None
        except psutil.Error:
            pass
        return 0


def adopt_running_bot(name):
    """يعيد تبنّي بوت يعمل فعلًا (حسب ملف .pid) حتى لا يُشغَّل مرتين."""
    try:
        with open(_pid_path(name), "r", encoding="utf-8") as f:
            pid = int((f.read() or "").strip() or 0)
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        proc = psutil.Process(pid)
        if not any(RUNNER in str(part) for part in proc.cmdline()):
            return None
        bot_path = os.path.abspath(os.path.join(BOTS_DIR, name))
        if os.path.abspath(proc.cwd()) != bot_path:
            return None
        started = proc.create_time()
    except (psutil.Error, OSError):
        return None
    logger.info("adopted running bot '%s' (pid=%s)", name, pid)
    return {"proc": AdoptedProcess(proc), "started": started}


def human_size(num):
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


# ------------------------------------------------- تشغيل / إيقاف العمليات

def proc_info(name):
    """يرجع معلومات العملية إن كانت تعمل فعلًا (ويتبنّى بوتًا نجا من إعادة تشغيل اللوحة)."""
    with PROCS_LOCK:
        info = PROCS.get(name)
    if info:
        code = info["proc"].poll()
        if code is not None:
            with PROCS_LOCK:
                PROCS.pop(name, None)
            clear_pid(name)
            EXITS[name] = {"at": time.time(), "started": info.get("started", 0), "code": code}
        else:
            return info
    adopted = adopt_running_bot(name)
    if adopted:
        with PROCS_LOCK:
            PROCS[name] = adopted
        return adopted
    return None


def is_running(name):
    return proc_info(name) is not None


def kill_tree(proc):
    """إنهاء العملية وكل أبنائها (يقتل عمليات pip أيضًا)."""
    children, parent = [], None
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except psutil.Error:
        pass
    for p in children:
        try:
            p.kill()
        except psutil.Error:
            pass
    if parent is not None:
        try:
            parent.kill()
        except psutil.Error:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def stop_bot(name, set_autorun=False):
    with BOT_OP_LOCK:
        with PROCS_LOCK:
            info = PROCS.pop(name, None)
        if info:
            kill_tree(info["proc"])
        clear_pid(name)
        meta = read_meta(name)
        if meta.get("autorun") and set_autorun:
            meta["autorun"] = False
            write_meta(name, meta)


def start_bot(name, only_if_stopped=False):
    """(يشغّل) يرجع (ok, message). only_if_stopped يمنع إعادة تشغيل بوت يعمل بالفعل."""
    with BOT_OP_LOCK:
        if only_if_stopped and proc_info(name):
            return True, ""
        return _start_bot_locked(name)


def _start_bot_locked(name):
    path = os.path.join(BOTS_DIR, name)
    meta = read_meta(name)
    main = (meta.get("main") or "").strip()
    if not main:
        return False, "حدّد الملف الرئيسي (main) من تبويب الملفات أولًا"
    try:
        main_path = safe_path(path, main)
    except ValueError:
        return False, "مسار الملف الرئيسي غير صالح"
    if not os.path.isfile(main_path):
        return False, f"الملف الرئيسي غير موجود: {main}"
    if not main.lower().endswith(".py"):
        return False, "الملف الرئيسي يجب أن يكون ملف .py"

    # فحص سريع قبل التشغيل: ملف فارغ أو ناقص = لن يعمل
    try:
        size = os.path.getsize(main_path)
        with open(main_path, "r", encoding="utf-8", errors="replace") as f:
            content_head = f.read()
    except OSError as exc:
        return False, f"تعذر قراءة الملف الرئيسي: {exc}"
    lines = content_head.count("\n") + (1 if content_head and not content_head.endswith("\n") else 0)
    if size == 0 or not content_head.strip():
        return False, "الملف الرئيسي فارغ — ارفع الملف كاملًا أو الصق محتواه ثم احفظه"
    code_lines = [ln for ln in content_head.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    if not code_lines:
        return False, "الملف الرئيسي يحتوي تعليقات فقط بدون أي كود"

    stop_bot(name)

    log_path = os.path.join(path, "server.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[SYSTEM] بدء تشغيل جديد — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"[SYSTEM] الملف الرئيسي: {main} ({human_size(size)}) — {lines} سطر\n")
    except OSError:
        pass

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    cmd = [sys.executable, "-u", RUNNER, main]
    if os.name == "posix" and BOT_NICE > 0:
        # البوتات بأولوية أقل حتى تبقى اللوحة مستجيبة تحت الضغط
        nice_bin = shutil.which("nice")
        if nice_bin:
            cmd = [nice_bin, "-n", str(BOT_NICE)] + cmd

    try:
        logf = open(log_path, "ab")
        proc = subprocess.Popen(
            cmd,
            cwd=path,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            env=env,
        )
        logf.close()
    except Exception as exc:
        return False, f"فشل التشغيل: {exc}"

    with PROCS_LOCK:
        PROCS[name] = {"proc": proc, "started": time.time()}
    write_pid(name, proc.pid)
    EXITS.pop(name, None)
    meta["autorun"] = True
    meta["next_restart"] = 0     # تشغيل جديد يلغي أي موعد إعادة معلّق
    write_meta(name, meta)
    logger.info("started bot '%s' (pid=%s)", name, proc.pid)
    return True, ""


def read_tail(path, max_bytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def shrink_log_if_big(path):
    """لو كبر السجل كثيرًا نحتفظ بآخر جزء فقط."""
    try:
        if os.path.getsize(path) <= MAX_LOG_BYTES:
            return
        tail = read_tail(path, MAX_LOG_BYTES // 2)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write("[SYSTEM] تم قص السجل الطويل — آخر الجزء فقط\n")
            f.write(tail)
    except OSError:
        pass


def count_running():
    with PROCS_LOCK:
        names = list(PROCS.keys())
    return sum(1 for n in names if is_running(n))


def autorun_bots():
    """يعيد تشغيل البوتات التي كانت تعمل قبل إعادة تشغيل اللوحة."""
    time.sleep(2)
    try:
        names = sorted(os.listdir(BOTS_DIR))
    except OSError:
        return
    for name in names:
        if not os.path.isdir(os.path.join(BOTS_DIR, name)):
            continue
        if is_running(name):
            continue
        meta = read_meta(name)
        if meta.get("autorun"):
            ok, msg = start_bot(name, only_if_stopped=True)
            logger.info("autorun '%s': %s %s", name, "ok" if ok else "skip", msg)
            time.sleep(0.7)     # توزيع حمل الإقلاع بدل تشغيل كل البوتات دفعة واحدة


def _monitor_once():
    """جولة مراقبة: يعيد تشغيل أي بوت متوقف، ويحمي من حلقات الأعطال."""
    try:
        names = sorted(os.listdir(BOTS_DIR))
    except OSError:
        return
    now = time.time()
    for name in names:
        if not os.path.isdir(os.path.join(BOTS_DIR, name)):
            continue
        meta = read_meta(name)
        if not meta.get("autorun"):
            continue
        info = proc_info(name)
        if info:
            fails = meta.get("fails") or {}
            if fails.get("count") and now - info["started"] >= STABLE_SECONDS:
                meta["fails"] = {"count": 0, "first": 0, "last": 0}
                meta["restart_delay"] = 0
                meta["next_restart"] = 0
                write_meta(name, meta)
            continue

        # البوت مطلوب تشغيله لكنه متوقف → نعيده دائمًا (سلوك باقي المنصات):
        # أي خروج — حتى برمز 0 — يعود له البوت، مع مهلة متصاعدة للملف الذي
        # ينتهي فورًا حتى لا ندور بلا فائدة، والإيقاف النهائي يبقى يدويًا فقط.
        exit_info = EXITS.get(name) or {}
        code = exit_info.get("code")
        ran_for = max(0, exit_info.get("at", 0) - exit_info.get("started", 0)) if exit_info else 0

        if ran_for >= STABLE_SECONDS:
            # عمل فترة كاملة ثم انتهى → استمرارية: نصفّر التأخير ونشغّله فورًا
            meta["fails"] = {"count": 0, "first": 0, "last": 0}
            meta["restart_delay"] = 0
            meta["next_restart"] = 0
            write_meta(name, meta)
            why = "طبيعيًا" if code == 0 else f"برمز خروج {code}"
            log_append(
                name,
                f"[SYSTEM] انتهى البوت {why} بعد {int(ran_for)} ثانية عمل — "
                "إعادة تشغيل تلقائية.\n",
            )
            ok, msg = start_bot(name, only_if_stopped=True)
            if not ok:
                log_append(name, f"[SYSTEM] فشل إعادة التشغيل: {msg}\n")
            logger.info("monitor: restart '%s' -> %s %s", name, ok, msg)
            time.sleep(0.7)
            continue

        next_at = float(meta.get("next_restart") or 0)
        if now < next_at:
            continue             # ما زلنا داخل مهلة الانتظار قبل الإعادة المجدولة

        if next_at:
            # حان موعد الإعادة المقررة → شغّله الآن (الشغيل يلغي الموعد نفسه)
            meta["next_restart"] = 0
            write_meta(name, meta)
            ok, msg = start_bot(name, only_if_stopped=True)
            if not ok:
                log_append(name, f"[SYSTEM] فشل إعادة التشغيل: {msg}\n")
            logger.info("monitor: scheduled restart '%s' -> %s %s", name, ok, msg)
            time.sleep(0.7)
            continue

        # خروج سريع لم تُجدول له مهلة بعد → مهلة متصاعدة 3 → 6 → 12 → 24 → 48 → 60ث
        fails = meta.get("fails") or {}
        if not fails.get("first") or now - fails.get("first", 0) > FAIL_WINDOW:
            fails = {"count": 0, "first": now, "last": 0}
        fails["count"] = fails.get("count", 0) + 1
        fails["last"] = now

        prev_delay = int(meta.get("restart_delay") or 0)
        delay = min(RESTART_DELAY_MAX,
                    RESTART_DELAY_BASE if not prev_delay else prev_delay * 2)
        meta["fails"] = fails
        meta["restart_delay"] = delay
        meta["next_restart"] = now + delay
        write_meta(name, meta)
        why = (f"برمز خروج 0 بعد {int(ran_for)} ثانية"
               if code == 0 else f"برمز خروج {code} بعد {int(ran_for)} ثانية")
        log_append(
            name,
            f"[SYSTEM] انتهى البوت {why} — إعادة تشغيل تلقائية بعد {delay} ثانية "
            f"(محاولة {fails['count']}). الإيقاف النهائي يكون بزر «إيقاف» فقط.\n",
        )
        logger.info("monitor: quick exit '%s' — next restart in %ss", name, delay)


def monitor_loop():
    """مراقب دائم: البوتات لا تبقى متوقفة."""
    time.sleep(5)
    while True:
        try:
            _monitor_once()
        except Exception as exc:      # لا نسمح للمراقب نفسه بالتوقف
            logger.error("monitor error: %s", exc)
        time.sleep(MONITOR_INTERVAL)


def keepalive_loop():
    """نبضة حياة دورية تمنع الاستضافات التي تنام عند الخمول من إيقاف اللوحة."""
    time.sleep(15)
    targets = [f"http://127.0.0.1:{PORT}/health"]
    if KEEPALIVE_URL:
        targets.append(KEEPALIVE_URL)
    while True:
        for target in targets:
            try:
                urllib.request.urlopen(target, timeout=15).read()
            except Exception:
                pass
        time.sleep(KEEPALIVE_INTERVAL)


_booted = False
_boot_lock = threading.Lock()


@app.before_request
def boot_once():
    global _booted
    if _booted:
        return
    with _boot_lock:
        if _booted:
            return
        _booted = True
        threading.Thread(target=autorun_bots, daemon=True).start()
        threading.Thread(target=monitor_loop, daemon=True).start()
        threading.Thread(target=keepalive_loop, daemon=True).start()


# ---------------------------------------------------------------- الصفحات

@app.route("/")
def page_index():
    if not session.get("admin"):
        return redirect("/login")
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/login")
def page_login():
    if session.get("admin"):
        return redirect("/")
    return send_from_directory(BASE_DIR, "login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "bots": count_running(), "time": time.time()})


@app.route("/api/login", methods=["POST"])
def api_login():
    ip = client_ip()
    now = time.time()
    with LOGIN_LOCK:
        hits = [t for t in LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
        LOGIN_FAILS[ip] = hits
        blocked = len(hits) >= LOGIN_LIMIT
    if blocked:
        return fail("محاولات كثيرة خاطئة — انتظر 5 دقائق ثم أعد المحاولة", 429)

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ok = (
        hashlib.sha256(username.encode()).hexdigest() == hashlib.sha256(ADMIN_USER.encode()).hexdigest()
        and hashlib.sha256(password.encode()).hexdigest() == hashlib.sha256(ADMIN_PASS.encode()).hexdigest()
    )
    if not ok:
        with LOGIN_LOCK:
            LOGIN_FAILS.setdefault(ip, []).append(now)
        return fail("اسم المستخدم أو كلمة المرور غير صحيحة", 401)

    session.permanent = True
    session["admin"] = True
    with LOGIN_LOCK:
        LOGIN_FAILS.pop(ip, None)
    return jsonify({"success": True})


# ---------------------------------------------------------------- واجهة API

@app.route("/api/system")
@admin_required
def api_system():
    try:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(BASE_DIR)
        return jsonify({
            "success": True,
            "cpu": psutil.cpu_percent(interval=None),
            "mem_used": round(mem.used / 1048576),
            "mem_total": round(mem.total / 1048576),
            "mem_percent": mem.percent,
            "disk_percent": disk.percent,
            "uptime": int(time.time() - _app_start),
            "bots_total": len([n for n in os.listdir(BOTS_DIR) if os.path.isdir(os.path.join(BOTS_DIR, n))]),
            "bots_running": count_running(),
            "default_password": ADMIN_PASS == "admin",
        })
    except Exception as exc:
        return fail(f"خطأ: {exc}", 500)


@app.route("/api/bots")
@admin_required
def api_bots():
    bots = []
    try:
        names = sorted(os.listdir(BOTS_DIR))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(BOTS_DIR, name)
        if not os.path.isdir(path):
            continue
        meta = read_meta(name)
        info = proc_info(name)
        cpu = mem = None
        uptime = 0
        if info:
            try:
                p = psutil.Process(info["proc"].pid)
                mem = round(p.memory_info().rss / 1048576, 1)
                cpu = p.cpu_percent(interval=None)
                uptime = int(time.time() - info["started"])
            except psutil.Error:
                pass
        bots.append({
            "name": name,
            "running": info is not None,
            "main": meta.get("main", ""),
            "autorun": bool(meta.get("autorun")),
            "cpu": cpu,
            "mem": mem,
            "uptime": uptime,
            "exit_code": None if info else (EXITS.get(name) or {}).get("code"),
            "exit_at": None if info else int((EXITS.get(name) or {}).get("at", 0)) or None,
        })
    return jsonify({"success": True, "bots": bots})


@app.route("/api/bots", methods=["POST"])
@admin_required
def api_create_bot():
    data = request.get_json(silent=True) or {}
    name = sanitize_name(data.get("name"))
    if not name:
        return fail("اسم غير صالح — استخدم حروفًا إنجليزية وأرقامًا وشرطات")
    path = os.path.join(BOTS_DIR, name)
    if os.path.exists(path):
        return fail("يوجد بوت بهذا الاسم بالفعل", 409)
    os.makedirs(path)
    write_meta(name, {"name": name, "main": "", "autorun": False})
    return jsonify({"success": True, "name": name})


@app.route("/api/bots/<name>/check")
@admin_required
def api_check_bot(name):
    """فحص سريع للملف الرئيسي: موجود؟ فارغ؟ فيه حلقة تشغيل؟"""
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    meta = read_meta(name)
    main = (meta.get("main") or "").strip()
    result = {"success": True, "main": main, "exists": False, "size": 0, "lines": 0,
              "has_loop": False, "problem": ""}
    if not main:
        result["problem"] = "لم يُحدَّد ملف رئيسي بعد — اضغط ★ بجانب ملف .py"
        return jsonify(result)
    try:
        full = safe_path(path, main)
    except ValueError:
        result["problem"] = "مسار الملف غير صالح"
        return jsonify(result)
    if not os.path.isfile(full):
        result["problem"] = "الملف الرئيسي غير موجود في مجلد البوت"
        return jsonify(result)
    result["exists"] = True
    result["size"] = os.path.getsize(full)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        result["problem"] = "تعذر قراءة الملف"
        return jsonify(result)
    result["lines"] = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    code_lines = [ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not code_lines:
        result["problem"] = "الملف فارغ أو تعليقات فقط"
        return jsonify(result)
    loop_markers = ("run_polling", "run_forever", "run_webhook", "asyncio.run",
                    "while True", "idle()", "start_polling", "executor.start")
    result["has_loop"] = any(m in content for m in loop_markers)
    if not result["has_loop"]:
        result["problem"] = (
            "لا توجد حلقة تشغيل دائمة في الملف — سيعمل ثم يخرج فورًا، وستعيد "
            "المراقبة تشغيله تلقائيًا كل دقيقة تقريبًا بدل بقائه شغّالًا. "
            "لتشغيل دائم أضف في نهايته مثل: application.run_polling()"
        )
    return jsonify(result)


@app.route("/api/bots/<name>/delete", methods=["POST"])
@admin_required
def api_delete_bot(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    stop_bot(name, set_autorun=True)
    shutil.rmtree(path, ignore_errors=True)
    logger.info("deleted bot '%s'", name)
    return jsonify({"success": True})


@app.route("/api/bots/<name>/action/<act>", methods=["POST"])
@admin_required
def api_bot_action(name, act):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    if act == "start" or act == "restart":
        # "تشغيل" على بوت يعمل بالفعل = لا شيء (لا نقتل نسخة شغّالة بضغطة مكررة)
        ok, msg = start_bot(name, only_if_stopped=(act == "start"))
        if not ok:
            return fail(msg)
        return jsonify({"success": True})
    if act == "stop":
        stop_bot(name, set_autorun=True)
        return jsonify({"success": True})
    return fail("إجراء غير معروف", 400)


@app.route("/api/bots/<name>/main", methods=["POST"])
@admin_required
def api_set_main(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    if rel:
        try:
            full = safe_path(path, rel)
        except ValueError:
            return fail("مسار غير صالح")
        if not os.path.isfile(full):
            return fail("الملف غير موجود", 404)
        if not rel.lower().endswith(".py"):
            return fail("الملف الرئيسي يجب أن يكون ملف .py")
    meta = read_meta(name)
    meta["main"] = rel
    write_meta(name, meta)
    return jsonify({"success": True})


@app.route("/api/bots/<name>/files")
@admin_required
def api_files(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    rel = request.args.get("path", "") or ""
    try:
        base = safe_path(path, rel)
    except ValueError:
        return fail("مسار غير صالح")
    if not os.path.isdir(base):
        return fail("المجلد غير موجود", 404)

    dirs, files = [], []
    for entry in sorted(os.listdir(base), key=lambda x: x.lower()):
        if rel == "" and entry in ("meta.json", "server.log", ".req_sha", ".pid"):
            continue
        full = os.path.join(base, entry)
        if os.path.isdir(full):
            dirs.append({"name": entry})
        elif os.path.isfile(full):
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            files.append({"name": entry, "size": size, "size_h": human_size(size)})

    meta = read_meta(name)
    return jsonify({"success": True, "path": rel, "dirs": dirs, "files": files, "main": meta.get("main", "")})


@app.route("/api/bots/<name>/file")
@admin_required
def api_file_read(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    rel = request.args.get("path", "") or ""
    try:
        full = safe_path(path, rel)
    except ValueError:
        return fail("مسار غير صالح")
    if not os.path.isfile(full):
        return fail("الملف غير موجود", 404)
    if os.path.getsize(full) > MAX_EDIT_BYTES:
        return fail("الملف كبير جدًا للتحرير داخل المتصفح", 413)
    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return jsonify({"success": True, "content": "", "binary": True, "path": rel})
    except OSError as exc:
        return fail(f"تعذر قراءة الملف: {exc}", 500)
    return jsonify({"success": True, "content": content, "binary": False, "path": rel})


@app.route("/api/bots/<name>/file", methods=["POST"])
@admin_required
def api_file_save(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    data = request.get_json(silent=True) or {}
    rel = (data.get("path") or "").strip()
    content = data.get("content", "")
    if not rel:
        return fail("لم يُحدد الملف")
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        return fail("المحتوى كبير جدًا", 413)
    try:
        full = safe_path(path, rel)
    except ValueError:
        return fail("مسار غير صالح")
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return fail(f"تعذر الحفظ: {exc}", 500)
    return jsonify({"success": True})


@app.route("/api/bots/<name>/delete-file", methods=["POST"])
@admin_required
def api_file_delete(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    data = request.get_json(silent=True) or {}
    rel = (data.get("path") or "").strip()
    kind = (data.get("kind") or "file").lower()
    if not rel:
        return fail("لم يُحدد الملف")
    try:
        full = safe_path(path, rel)
    except ValueError:
        return fail("مسار غير صالح")
    if full == os.path.abspath(path):
        return fail("لا يمكن حذف مجلد البوت نفسه")
    try:
        if kind == "dir":
            shutil.rmtree(full)
        else:
            os.remove(full)
    except OSError as exc:
        return fail(f"تعذر الحذف: {exc}", 500)
    return jsonify({"success": True})


@app.route("/api/bots/<name>/upload", methods=["POST"])
@admin_required
def api_upload(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    rel = (request.form.get("path") or "").strip()
    try:
        target = safe_path(path, rel)
    except ValueError:
        return fail("مسار غير صالح")
    os.makedirs(target, exist_ok=True)

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return fail("لم تُختر أي ملفات")
    saved = []
    for f in files:
        filename = os.path.basename(f.filename.replace("\\", "/")).strip()
        if not filename or filename in (".", ".."):
            continue
        try:
            f.save(os.path.join(target, filename))
            saved.append(filename)
        except OSError:
            continue
    if not saved:
        return fail("تعذر حفظ الملفات", 500)

    meta = read_meta(name)
    main_ok = False
    current_main = meta.get("main") or ""
    if current_main:
        try:
            main_ok = os.path.isfile(safe_path(path, current_main))
        except ValueError:
            main_ok = False
    if not main_ok:
        first_py = next((x for x in saved if x.lower().endswith(".py")), "")
        if first_py:
            meta["main"] = os.path.join(rel, first_py).replace("\\", "/") if rel else first_py
            write_meta(name, meta)
    return jsonify({"success": True, "saved": saved})


@app.route("/api/quick-upload", methods=["POST"])
@admin_required
def api_quick_upload():
    """رفع سريع من الشريط العلوي: ملفات → بوت جديد/موجود باسم أول ملف."""
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return fail("اختر ملفًا واحدًا على الأقل")

    py_names = [os.path.basename(f.filename.replace("\\", "/")) for f in files]
    base = next((os.path.splitext(n)[0] for n in py_names if n.lower().endswith(".py")), "")
    name = sanitize_name(base) or "my-bot"

    path = os.path.join(BOTS_DIR, name)
    if not os.path.exists(path):
        os.makedirs(path)
        write_meta(name, {"name": name, "main": "", "autorun": False})
    return api_upload(name)


@app.route("/api/bots/<name>/log")
@admin_required
def api_log(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    log_path = os.path.join(path, "server.log")
    shrink_log_if_big(log_path)
    log = read_tail(log_path, LOG_TAIL_BYTES)
    return jsonify({"success": True, "log": log, "running": is_running(name)})


@app.route("/api/bots/<name>/log/clear", methods=["POST"])
@admin_required
def api_log_clear(name):
    path = bot_dir(name)
    if not path:
        return fail("البوت غير موجود", 404)
    try:
        with open(os.path.join(path, "server.log"), "w", encoding="utf-8") as f:
            f.write("[SYSTEM] تم تفريغ السجل\n")
    except OSError as exc:
        return fail(f"تعذر التفريغ: {exc}", 500)
    return jsonify({"success": True})


# ---------------------------------------------------------------- أخطاء

@app.errorhandler(413)
def too_large(e):
    return fail("حجم الملفات كبير جدًا (الحد الأقصى 50MB)", 413)


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return fail("غير موجود", 404)
    return redirect("/")


_app_start = time.time()

if __name__ == "__main__":
    logger.info("HostBot — http://0.0.0.0:%s  (admin user: %s)", PORT, ADMIN_USER)
    if ADMIN_PASS == "admin":
        logger.warning("!! تستخدم كلمة المرور الافتراضية admin — عيّن ADMIN_PASS قبل النشر")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
