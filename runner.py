# -*- coding: utf-8 -*-
"""
runner.py — يشغّل ملف البوت مع تثبيت تلقائي للمكتبات الناقصة.

يُستدعى من اللوحة:  python runner.py <الملف-الرئيسي.py>
داخل مجلد البوت (cwd = مجلد البوت).

الوظائف:
  1. تثبيت requirements.txt تلقائيًا عند تغيّره.
  2. إصلاح DNS (IPv4) لبيئة الحاويات.
  3. إذا احتاج البوت مكتبة غير موجودة → pip install ثم إعادة التشغيل.
"""
import hashlib
import os
import re
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import dns_fix  # noqa: F401  (يصلح مشاكل الاتصال داخل حاويات HF)
except Exception:
    pass

MAX_AUTO_INSTALLS = 10

# أسماء استيراد شائعة → اسم الحزمة الحقيقي على pip
IMPORT_TO_PACKAGE = {
    "telegram": "python-telegram-bot",
    "telebot": "pyTelegramBotAPI",
    "discord": "discord.py",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "Crypto": "pycryptodome",
    "flask_cors": "Flask-Cors",
    "dateutil": "python-dateutil",
    "git": "GitPython",
    "serial": "pyserial",
    "magic": "python-magic",
    "TgCrypto": "TgCrypto",
    "pydantic_settings": "pydantic-settings",
    "apscheduler": "APScheduler",
    "pandas": "pandas",
    "numpy": "numpy",
    "requests": "requests",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
}


def pip_install(package):
    print(f"[SETUP] جاري تثبيت: {package} …", flush=True)
    code = subprocess.call([
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input", "-q", package,
    ])
    return code == 0


def ensure_requirements():
    """يثبّت requirements.txt مرة واحدة كل ما يتغيّر."""
    if not os.path.isfile("requirements.txt"):
        return
    try:
        with open("requirements.txt", "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return
    marker = ".req_sha"
    old = ""
    try:
        if os.path.exists(marker):
            with open(marker, "r", encoding="utf-8") as f:
                old = f.read().strip()
    except OSError:
        pass
    if old == sha:
        return
    print("[SETUP] تثبيت requirements.txt …", flush=True)
    code = subprocess.call([
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input", "-q", "-r", "requirements.txt",
    ])
    if code == 0:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(sha)
        except OSError:
            pass


def missing_module_name(exc):
    name = getattr(exc, "name", None)
    if name:
        return name
    text = str(exc)
    m = re.search(r"No module named ['\"]([^'\"]+)['\"]", text)
    if m:
        return m.group(1)
    m = re.search(r"cannot import name ['\"][^'\"]+['\"] from ['\"]([^'\"]+)['\"]", text)
    if m:
        return m.group(1)
    return None


def fix_and_install(module):
    """يثبّت الحزمة المقابلة للاستيراد (مع إزالة التعارض إن وُجد)."""
    top = (module or "").split(".")[0]
    package = IMPORT_TO_PACKAGE.get(top, top)
    if not package:
        return ""
    if top != package:
        # مثلًا استيراد telegram لكن الحزمة الصحيحة python-telegram-bot
        subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y", "-q", top])
    if pip_install(package):
        return package
    return ""


def run_script(script):
    import runpy

    script_dir = os.path.dirname(os.path.abspath(script))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    installed = set()
    attempts = 0
    while True:
        try:
            runpy.run_path(script, run_name="__main__")
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0
        except KeyboardInterrupt:
            return 0
        except (ModuleNotFoundError, ImportError) as exc:
            module = missing_module_name(exc)
            package = IMPORT_TO_PACKAGE.get((module or "").split(".")[0], module)
            if module and package and package not in installed and attempts < MAX_AUTO_INSTALLS:
                attempts += 1
                if fix_and_install(module):
                    installed.add(package)
                    print(f"[SETUP] تم تثبيت {package} — إعادة تشغيل البوت…", flush=True)
                    continue
            traceback.print_exc()
            return 1
        except BaseException:
            traceback.print_exc()
            return 1


def main():
    if len(sys.argv) < 2:
        print("usage: python runner.py <script.py>")
        return 2
    script = sys.argv[1]
    print(f"[SYSTEM] تشغيل {script} (pid={os.getpid()})", flush=True)
    ensure_requirements()
    code = run_script(script)
    print(f"[SYSTEM] انتهى البوت (رمز الخروج {code})", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
