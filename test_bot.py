# -*- coding: utf-8 -*-
import sys
print("Python:", sys.version)
print("Тест кодування OK")

try:
    exec(open('jkbms_bot.py', encoding='utf-8').read())
except Exception as e:
    print("ПОМИЛКА:", type(e).__name__, str(e)[:200])
