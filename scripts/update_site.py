"""Обновить стили и классы ИТД из собранных файлов сайта.

Использование:
    python scripts/update_site.py path/to/index-XXXX.js path/to/index-XXXX.css

Сайт собран через vite + CSS modules, поэтому после каждого обновления
хешированные имена классов меняются. Скрипт находит в JS объекты CSS-модулей
(например `{post:"OAX3",postInner:"qJiW",...}`), узнаёт нужные по набору ключей
и сохраняет их в itdshot/templates/classes.json, а CSS копирует в
itdshot/templates/site.css.
"""

import json
import re
import sys
from pathlib import Path

templates = Path(__file__).parent.parent / "itdshot" / "templates"

# имя модуля в шаблоне -> ключи, по которым модуль однозначно узнаётся
MODULES = {
    "post": {"postInner", "originalPostMedia", "textWrapper"},
    "header": {"headerMain", "authorLink", "moreDropdown"},
    "actions": {"actionsLeft", "capturedEmoji", "views"},
    "media": {"mediaWrapper", "singleVideo", "isFeed"},
    "user_name": {"userName", "pinBadge", "nukstaGlow"},
    "avatar": {"avatar", "emoji", "onlineDot"},
    "text": {"spoiler", "mention", "hashtag", "monospace"},
    "dropdown": {"dropdownWrapper", "trigger", "menuItem"},
    "tool": {"signature", "stamp", "signatureText", "actor"},
    "red_pen": {"textLayer", "overlay", "strike", "handwriting"},
}

# картинки из подписей корректора и красной ручки
ASSETS = {
    "corrector_stamp": r"/assets/icon-37-[\w-]+\.svg",
    "red_pen_icon": r"/assets/icon2-44-[\w-]+\.svg",
}

IDENT = r"[A-Za-z_$][\w$]*"


def parse_modules(js: str) -> list[dict[str, str]]:
    variables = dict(re.findall(rf'(?<![\w$.])({IDENT})="([\w-]+)"', js))
    modules = []
    for match in re.finditer(rf"\{{((?:{IDENT}:(?:{IDENT}|\"[\w-]+\"),?)+)\}}", js):
        module = {}
        for pair in match.group(1).rstrip(",").split(","):
            key, value = pair.split(":", 1)
            value = value.strip('"') if value.startswith('"') else variables.get(value)
            if value is None:
                break
            module[key] = value
        else:
            modules.append(module)
    return modules


def main(js_path: str, css_path: str):
    js = Path(js_path).read_text()
    css = Path(css_path).read_text()

    found = {}
    for module in parse_modules(js):
        for name, keys in MODULES.items():
            if keys <= module.keys() and name not in found:
                found[name] = module

    missing = MODULES.keys() - found.keys()
    if missing:
        sys.exit(f"не найдены модули: {', '.join(sorted(missing))}")

    for name, module in found.items():
        if name in ("assets", "corrector_textures"):
            continue
        absent = [key for key, cls in module.items() if cls not in css]
        if absent:
            print(f"warning: {name}: классов нет в CSS: {', '.join(absent)}")

    assets = {}
    for name, pattern in ASSETS.items():
        match = re.search(pattern, js)
        if not match:
            sys.exit(f"не найдена картинка {name}")
        assets[name] = match.group(0)

    # формы пятен корректора: массив пар svg-путей перед генерацией data:url
    textures = re.search(r'=(\[\["M[^=]*?\]\]),\w+=\w+\.map\(\(\[\w+,\w+\]\)=>\{const \w+=`<svg', js)
    if not textures:
        sys.exit("не найдены текстуры корректора")

    found["assets"] = assets
    found["corrector_textures"] = json.loads(textures.group(1))
    (templates / "classes.json").write_text(
        json.dumps(found, indent=4, ensure_ascii=False) + "\n"
    )
    (templates / "site.css").write_text(css)
    print(f"saved {len(found)} modules and {len(css)} bytes of css")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
