CATEGORIES: dict[str, str] = {
    "printers": "🖨 Принтеры и оргтехника",
    "pc": "🖥 Системные блоки",
    "monitors": "📺 Мониторы",
    "cartridges": "🖋 Картриджи",
    "cables": "🔌 Кабели (VGA, HDMI)",
    "storage": "💾 Диски (SSD, HDD)",
    "adapters": "🔀 Переходники (сеть→USB и др.)",
    "hubs": "🔗 USB-хабы",
    "headsets": "🎧 Наушники",
    "batteries": "🔋 Батарейки",
    "mice": "🖱 Мыши",
    "keyboards": "⌨️ Клавиатуры",
    "other": "📦 Прочее",
}
CONDITIONS: dict[str, str] = {"new": "новые", "used": "б/у", "repaired": "после ремонта"}
# для «×1 (новый)»
CONDITION_ONE: dict[str, str] = {"new": "новый", "used": "б/у", "repaired": "после ремонта"}

_CONDITION_ALIASES = {
    "new": ("new", "новые", "новое", "новый", "новая"),
    "used": ("used", "б/у", "бу", "б у", "старые", "старое"),
    "repaired": ("repaired", "после ремонта", "отремонтированные"),
}


def _norm(v) -> str:
    return " ".join(str(v).split()).lower() if v is not None else ""


def plain(code: str) -> str:
    """Подпись категории без эмодзи."""
    return CATEGORIES[code].split(" ", 1)[1]


def parse_category(v) -> str | None:
    t = _norm(v)
    for code, label in CATEGORIES.items():
        if t in (code, label.lower(), plain(code).lower()):
            return code
    return None


def parse_condition(v) -> str | None:
    t = _norm(v)
    if not t:
        return "new"
    for code, aliases in _CONDITION_ALIASES.items():
        if t in aliases:
            return code
    return None
