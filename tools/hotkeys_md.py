"""The README's table of hotkeys, made from the window's own actions (the Hotkeys window shows the same):

    python tools/hotkeys_md.py ru|en        → the Markdown table on stdout
    python tools/hotkeys_md.py --readme     → put both tables into README.md between the <!-- hotkeys:ru / en --> marks
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ARTCORE_AUDIO", "null")
os.environ.setdefault("ARTCORE_SOUND_DATA", tempfile.mkdtemp())


def table(lang: str) -> str:
    from PySide6.QtWidgets import QApplication
    from sound import config, keys, store
    from sound.i18n import set_lang, t
    from sound.window import MainWindow
    app = QApplication.instance() or QApplication([])
    set_lang(lang)
    s = store.Session(tempfile.mkdtemp())
    w = MainWindow(config.load(), s)
    out = [f"| {t('ks_command')} | {t('ks_keys')} |", "|---|---|"]
    last = None
    for menu, cmd, k in keys.table(w.menuBar()):
        if menu != last:
            out.append(f"| **{menu}** | |")
            last = menu
        out.append(f"| {cmd} | {k.replace('|', chr(92) + '|')} |")
    out.append(f"| **{t('ks_mouse')}** | |")
    for key in keys.MOUSE:
        what, _sep, how = t(key).partition("|")
        out.append(f"| {what} | {how} |")
    w.close()
    s.close()
    del app
    return "\n".join(out) + "\n"


def readme() -> None:
    p = os.path.join(ROOT, "README.md")
    text = open(p, encoding="utf-8").read()
    for lang in ("ru", "en"):
        a, b = f"<!-- hotkeys:{lang} -->", f"<!-- /hotkeys:{lang} -->"
        text = re.sub(re.escape(a) + r".*?" + re.escape(b), lambda _m, lang=lang: f"{a}\n{table(lang)}{b}", text,
                      flags=re.S)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--readme":
        readme()
    else:
        sys.stdout.write(table(sys.argv[1] if len(sys.argv) > 1 else "ru"))
