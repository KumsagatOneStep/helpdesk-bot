from helpdesk.stock import catalog


def test_categories_fixed_order():
    assert list(catalog.CATEGORIES) == [
        "printers",
        "pc",
        "monitors",
        "cartridges",
        "cables",
        "storage",
        "adapters",
        "hubs",
        "headsets",
        "batteries",
        "mice",
        "keyboards",
        "other",
    ]
    assert catalog.CATEGORIES["cartridges"] == "🖋 Картриджи"
    assert catalog.plain("cartridges") == "Картриджи"


def test_parse_category_accepts_code_label_and_plain():
    for v in ("cartridges", "🖋 Картриджи", "картриджи", "  КАРТРИДЖИ "):
        assert catalog.parse_category(v) == "cartridges", v
    assert catalog.parse_category("Принтер") is None
    assert catalog.parse_category(None) is None


def test_parse_condition():
    for v, code in [
        ("new", "new"),
        ("Новые", "new"),
        ("новое", "new"),
        ("новый", "new"),
        (None, "new"),
        ("", "new"),
        ("б/у", "used"),
        ("БУ", "used"),
        ("used", "used"),
        ("после ремонта", "repaired"),
        ("repaired", "repaired"),
    ]:
        assert catalog.parse_condition(v) == code, v
    assert catalog.parse_condition("сломан") is None
