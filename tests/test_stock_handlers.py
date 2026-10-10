from io import BytesIO
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from openpyxl import Workbook

from helpdesk.stock import service as stock
from helpdesk.telegram import menu, stock_handlers
from helpdesk.telegram.stock_handlers import StockForm
from tests.factories import make_staff, payloads


class Msg:
    def __init__(self, text=None, **kw):
        self.text = text
        self.document = kw.get("document")
        self.answers = []  # (text, reply_markup)
        self.edits = []
        self.docs = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=1)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, **kw):
        pass

    async def answer_document(self, document, caption=None, **kw):
        self.docs.append((document, caption))


class Cq:
    def __init__(self, data):
        self.data = data
        self.message = Msg()
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def kb(markup_):
    if markup_ is None or not hasattr(markup_, "inline_keyboard"):
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup_.inline_keyboard]


def datas(markup_):
    return [d for row in kb(markup_) or [] for _, d in row]


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _setup(s):
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    hp = await stock.create_item(s, coord, "cartridges", "Картридж HP 85A", min_qty=3)
    await stock.receive(s, coord, hp.id, "new", 7)
    return coord, erlan, hp


def test_parse_int():
    assert stock_handlers.parse_int(" 12 ", 1, 1000) == 12
    for bad in ("0", "1001", "1.5", "-1", "два", "", "²"):
        assert stock_handlers.parse_int(bad, 1, 1000) is None, bad


async def test_home_specialist_vs_coordinator(s, clock_, state):
    coord, erlan, _ = await _setup(s)
    m = Msg(menu.STOCK)
    await stock_handlers.stock_home(m, state, s, erlan)
    text, markup_ = m.answers[-1]
    assert text == "📦 Склад"
    rows = kb(markup_)
    assert ("🖋 Картриджи · 7", "sk_cat:cartridges:0") in [b for row in rows for b in row]
    assert ("🖱 Мыши · 0", "sk_cat:mice:0") in [b for row in rows for b in row]
    assert rows[-1] == [("🔍 Найти", "sk_find"), ("📥 Склад.xlsx", "sk_xlsx")]
    m = Msg(menu.STOCK)
    await stock_handlers.stock_home(m, state, s, coord)
    assert kb(m.answers[-1][1])[-1] == [
        ("➕ Новая позиция", "sk_new"),
        ("📤 Загрузить xlsx", "sk_upl"),
    ]


async def test_category_and_card(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    cq = Cq("sk_cat:cartridges:0")
    await stock_handlers.cb_category(cq, s)
    text, markup_ = cq.message.edits[-1]
    assert text == "🖋 Картриджи"
    assert kb(markup_) == [
        [("Картридж HP 85A — новые 7", f"sk_item:{hp.id}")],
        [("⬅️ Категории", "sk_home")],
    ]
    cq = Cq(f"sk_item:{hp.id}")
    await stock_handlers.cb_item(cq, s, erlan)
    text, markup_ = cq.message.edits[-1]
    assert text == (
        "Картридж HP 85A\n🖋 Картриджи\n\nновые: 7\nб/у: 0\nпосле ремонта: 0\nминимум: 3"
    )
    assert kb(markup_) == [[("➖ Взять", f"sk_take:{hp.id}")], [("⬅️ Назад", "sk_cat:cartridges:0")]]
    cq = Cq(f"sk_item:{hp.id}")
    await stock_handlers.cb_item(cq, s, coord)
    assert datas(cq.message.edits[-1][1]) == [
        f"sk_take:{hp.id}",
        f"sk_in:{hp.id}",
        f"sk_fix:{hp.id}",
        f"sk_min:{hp.id}",
        f"sk_arch:{hp.id}",
        "sk_cat:cartridges:0",
    ]


async def test_bad_category_code_is_stale(s, clock_):
    cq = Cq("sk_cat:nope:0")
    await stock_handlers.cb_category(cq, s)
    assert cq.alerts == ["Кнопка устарела"]


async def test_take_flow_single_condition_and_shortage(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    cq = Cq(f"sk_take:{hp.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)  # одно состояние — сразу количество
    assert await state.get_state() == StockForm.take_qty.state
    text, markup_ = cq.message.answers[-1]
    assert text == "Картридж HP 85A (новый), есть 7. Сколько взять?"
    assert kb(markup_) == [
        [("1", "sk_tq:1"), ("2", "sk_tq:2"), ("3", "sk_tq:3"), ("Другое", "sk_tq:other")]
    ]
    await stock.take(s, coord, hp.id, "new", 6)  # кто-то успел раньше: осталась 1
    cq = Cq("sk_tq:2")
    await stock_handlers.cb_take_qty(cq, state, s, erlan)
    assert cq.message.answers[0][0] == "Осталось только 1."
    assert await state.get_state() == StockForm.take_qty.state
    m = Msg("1")
    await stock_handlers.got_take_qty(m, state, s, erlan)
    assert m.answers[-1][0] == "✅ Взято: Картридж HP 85A ×1 (новый). Осталось 0."
    assert await state.get_state() is None
    assert await payloads(s, "wa") == []


async def test_take_flow_choose_condition_and_other(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    await stock.receive(s, coord, hp.id, "used", 2)
    cq = Cq(f"sk_take:{hp.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)
    assert kb(cq.message.answers[-1][1]) == [
        [
            ("новые · 7", f"sk_tc:{hp.id}:new"),
            ("б/у · 2", f"sk_tc:{hp.id}:used"),
        ]
    ]
    cq = Cq(f"sk_tc:{hp.id}:used")
    await stock_handlers.cb_take_cond(cq, state, s, erlan)
    assert kb(cq.message.answers[-1][1]) == [
        [("1", "sk_tq:1"), ("2", "sk_tq:2"), ("Другое", "sk_tq:other")]
    ]
    cq = Cq("sk_tq:other")
    await stock_handlers.cb_take_qty(cq, state, s, erlan)
    assert cq.message.answers[-1][0] == "Введите число от 1 до 2."
    bad = Msg("пять")
    await stock_handlers.got_take_qty(bad, state, s, erlan)
    assert bad.answers[-1][0] == "Введите число от 1 до 2."
    ok = Msg("2")
    await stock_handlers.got_take_qty(ok, state, s, erlan)
    assert ok.answers[-1][0] == "✅ Взято: Картридж HP 85A ×2 (б/у). Осталось 0."


async def test_take_nothing_and_archived(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    empty = await stock.create_item(s, coord, "mice", "Мышь")
    cq = Cq(f"sk_take:{empty.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)
    assert cq.alerts == ["Нет в наличии"]
    cq = Cq(f"sk_take:{hp.id}")
    await stock_handlers.cb_take(cq, state, s, erlan)
    await stock.archive(s, coord, hp.id)
    m = Msg("1")
    await stock_handlers.got_take_qty(m, state, s, erlan)
    assert m.answers[-1][0] == "Позиция в архиве."
    assert await state.get_state() is None


async def test_search(s, clock_, state):
    coord, erlan, hp = await _setup(s)
    cq = Cq("sk_find")
    await stock_handlers.cb_find(cq, state)
    assert await state.get_state() == StockForm.search.state
    m = Msg("85a")
    await stock_handlers.got_search(m, state, s, erlan)
    assert kb(m.answers[-1][1]) == [[("Картридж HP 85A — новые 7", f"sk_item:{hp.id}")]]
    assert await state.get_state() is None
    await stock_handlers.cb_find(Cq("sk_find"), state)
    m = Msg("hdmi")
    await stock_handlers.got_search(m, state, s, erlan)
    assert m.answers[-1][0] == "Ничего не найдено."


def _message(**kw) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "E"},
            **kw,
        }
    )


async def _matched_names(msg, raw_state, staff):
    out = []
    for h in stock_handlers.router.message.handlers:
        ok, _ = await h.check(msg, staff=staff, raw_state=raw_state)
        if ok:
            out.append(h.callback.__name__)
    return out


async def test_stock_dialog_routing():
    staff = SimpleNamespace(status="active", is_admin=False, role="helpdesk")
    photo = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    for st in StockForm.__all_states__:
        for text in (menu.MY, "/my", menu.NEW_TASK):
            assert await _matched_names(_message(text=text), st.state, staff) == ["leave_dialog"], (
                st,
                text,
            )
        assert await _matched_names(_message(photo=photo), st.state, staff), st
        assert await _matched_names(_message(text=menu.CANCEL), st.state, staff) == [], st
        assert (await _matched_names(_message(text=menu.STOCK), st.state, staff))[0] == (
            "stock_home"
        ), st


async def test_stale_buttons(s, clock_, state):
    cq = Cq("sk_tq:1")  # нет диалога
    await stock_handlers.stale(cq)
    assert cq.alerts == ["Диалог уже завершён"]


async def test_receive_and_fix(s, clock_, state):
    coord, _, hp = await _setup(s)
    cq = Cq(f"sk_in:{hp.id}")
    await stock_handlers.cb_in(cq, state, s, coord)
    assert kb(cq.message.answers[-1][1]) == [
        [
            ("новые", f"sk_ic:{hp.id}:new"),
            ("б/у", f"sk_ic:{hp.id}:used"),
            ("после ремонта", f"sk_ic:{hp.id}:repaired"),
        ]
    ]
    await stock_handlers.cb_in_cond(Cq(f"sk_ic:{hp.id}:repaired"), state, s, coord)
    assert await state.get_state() == StockForm.in_qty.state
    bad = Msg("1001")
    await stock_handlers.got_in_qty(bad, state, s, coord)
    assert bad.answers[-1][0] == "Введите число от 1 до 1000."
    m = Msg("2")
    await stock_handlers.got_in_qty(m, state, s, coord)
    assert m.answers[-1][0] == "➕ Картридж HP 85A +2 (после ремонта). Теперь 2."
    await stock_handlers.cb_fix(Cq(f"sk_fix:{hp.id}"), state, s, coord)
    await stock_handlers.cb_fix_cond(Cq(f"sk_fc:{hp.id}:new"), state, s, coord)
    assert await state.get_state() == StockForm.fix_qty.state
    m = Msg("0")
    await stock_handlers.got_fix_qty(m, state, s, coord)
    assert m.answers[-1][0] == "✏️ Картридж HP 85A (новый): теперь 0."
    assert await stock.levels(s, hp.id) == {"new": 0, "used": 0, "repaired": 2}


async def test_min_and_archive(s, clock_, state):
    coord, _, hp = await _setup(s)
    await stock_handlers.cb_min(Cq(f"sk_min:{hp.id}"), state, s, coord)
    m = Msg("5")
    await stock_handlers.got_min(m, state, s, coord)
    assert m.answers[-1][0] == "🔔 Минимум для «Картридж HP 85A»: 5."
    await stock_handlers.cb_min(Cq(f"sk_min:{hp.id}"), state, s, coord)
    cq = Cq("sk_minx")
    await stock_handlers.cb_min_clear(cq, state, s, coord)
    assert cq.message.answers[-1][0] == "🔔 Минимум для «Картридж HP 85A» убран."
    assert (await stock.get_item(s, hp.id)).min_qty is None
    cq = Cq(f"sk_arch:{hp.id}")
    await stock_handlers.cb_arch(cq, s, coord)
    assert kb(cq.message.answers[-1][1]) == [
        [("Да, в архив", f"sk_archok:{hp.id}"), ("Отмена", "sk_cancel")]
    ]
    cq = Cq(f"sk_archok:{hp.id}")
    await stock_handlers.cb_arch_ok(cq, s, coord)
    assert cq.message.edits[-1][0] == "🗄 Картридж HP 85A — в архиве."
    assert (await stock.get_item(s, hp.id)).archived


async def test_new_item_flow_and_duplicate(s, clock_, state):
    coord, _, hp = await _setup(s)
    cq = Cq("sk_new")
    await stock_handlers.cb_new(cq, state, coord)
    assert ("🖱 Мыши", "sk_nc:mice") in [b for row in kb(cq.message.answers[-1][1]) for b in row]
    await stock_handlers.cb_new_cat(Cq("sk_nc:cartridges"), state, coord)
    assert await state.get_state() == StockForm.new_name.state
    dup = Msg("картридж hp 85a")
    await stock_handlers.got_new_name(dup, state, s, coord)
    assert dup.answers[-1][0] == "Уже есть: Картридж HP 85A"
    assert kb(dup.answers[-1][1]) == [[("Открыть", f"sk_item:{hp.id}")]]
    assert await state.get_state() is None
    await stock_handlers.cb_new_cat(Cq("sk_nc:cartridges"), state, coord)
    long = Msg("x" * 201)
    await stock_handlers.got_new_name(long, state, s, coord)
    assert long.answers[-1][0] == "Название от 1 до 200 символов."
    await stock_handlers.got_new_name(Msg("Canon 728"), state, s, coord)
    assert await state.get_state() == StockForm.new_min.state
    m = Msg("2")
    await stock_handlers.got_new_min(m, state, s, coord)
    item = await stock.find_active(s, "cartridges", "canon 728")
    assert item.min_qty == 2
    text, markup_ = m.answers[-1]
    assert text == "Позиция «Canon 728» создана. Что пришло?"
    assert datas(markup_) == [
        f"sk_ic:{item.id}:new",
        f"sk_ic:{item.id}:used",
        f"sk_ic:{item.id}:repaired",
        f"sk_item:{item.id}",
    ]
    await stock_handlers.cb_new_cat(Cq("sk_nc:mice"), state, coord)
    await stock_handlers.got_new_name(Msg("Мышь"), state, s, coord)
    cq = Cq("sk_skipmin")
    await stock_handlers.cb_new_skip_min(cq, state, s, coord)
    assert (await stock.find_active(s, "mice", "Мышь")).min_qty is None


async def test_coordinator_callbacks_rejected_for_specialist(s, clock_, state):
    _, erlan, hp = await _setup(s)
    calls = [
        (stock_handlers.cb_in, Cq(f"sk_in:{hp.id}"), (state, s, erlan)),
        (stock_handlers.cb_in_cond, Cq(f"sk_ic:{hp.id}:new"), (state, s, erlan)),
        (stock_handlers.cb_fix, Cq(f"sk_fix:{hp.id}"), (state, s, erlan)),
        (stock_handlers.cb_fix_cond, Cq(f"sk_fc:{hp.id}:new"), (state, s, erlan)),
        (stock_handlers.cb_min, Cq(f"sk_min:{hp.id}"), (state, s, erlan)),
        (stock_handlers.cb_arch, Cq(f"sk_arch:{hp.id}"), (s, erlan)),
        (stock_handlers.cb_arch_ok, Cq(f"sk_archok:{hp.id}"), (s, erlan)),
        (stock_handlers.cb_new, Cq("sk_new"), (state, erlan)),
        (stock_handlers.cb_new_cat, Cq("sk_nc:mice"), (state, erlan)),
    ]
    for fn, cq, args in calls:
        await fn(cq, *args)
        assert cq.alerts == ["Только для координатора"], fn.__name__
        assert cq.message.answers == [] and cq.message.edits == [], fn.__name__
    assert await state.get_state() is None
    assert not (await stock.get_item(s, hp.id)).archived
    # текстовый ввод в «координаторском» состоянии от специалиста тоже не проходит
    await state.set_state(StockForm.in_qty)
    await state.update_data(item_id=hp.id, condition="new")
    m = Msg("5")
    await stock_handlers.got_in_qty(m, state, s, erlan)
    assert m.answers[-1][0] == "Только для координатора."
    assert (await stock.levels(s, hp.id))["new"] == 7


def _xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Категория", "Название", "Состояние", "Количество", "Минимум"])
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _Bot:
    def __init__(self, data: bytes):
        self.data = data

    async def download(self, obj, destination):
        destination.write(self.data)


def _doc(name="Склад.xlsx", size=100):
    return SimpleNamespace(file_name=name, file_size=size, file_id="f", file_unique_id="u")


async def test_export_button(s, clock_):
    _, erlan, _ = await _setup(s)
    cq = Cq("sk_xlsx")
    await stock_handlers.cb_export(cq, s, erlan)
    doc, caption = cq.message.docs[-1]
    assert doc.filename == "Склад.xlsx" and caption == "Склад: остатки и движение"


async def test_upload_flow(s, clock_, state):
    coord, _, hp = await _setup(s)
    cq = Cq("sk_upl")
    await stock_handlers.cb_upload(cq, state, coord)
    assert await state.get_state() == StockForm.import_file.state
    assert cq.message.docs[-1][0].filename == "Склад-шаблон.xlsx"
    data = _xlsx(
        [
            ["Картриджи", "Картридж HP 85A", "новые", 3, None],
            ["Мыши", "Мышь USB", "б/у", 4, 1],
            ["Принтер", "X", "новые", 1, None],
        ]
    )
    m = Msg(document=_doc(size=len(data)))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(data))
    text, markup_ = m.answers[-1]
    assert text == (
        "Новых позиций: 1, приход по существующим: 1, ошибки: 1\n"
        "строка 4: неизвестная категория «Принтер»"
    )
    assert kb(markup_) == [
        [("✅ Загрузить (без ошибочных строк)", "sk_uplok"), ("✖️ Отмена", "sk_cancel")]
    ]
    assert await state.get_state() == StockForm.import_confirm.state
    assert (await stock.levels(s, hp.id))["new"] == 7  # ничего не записано до подтверждения
    cq = Cq("sk_uplok")
    await stock_handlers.cb_upload_ok(cq, state, s, coord)
    assert cq.message.answers[-1][0] == "✅ Загружено: новых позиций 1, строк с приходом 2."
    assert (await stock.levels(s, hp.id))["new"] == 10
    assert await state.get_state() is None


async def test_upload_rejects_bad_files(s, clock_, state):
    coord, _, _ = await _setup(s)
    await stock_handlers.cb_upload(Cq("sk_upl"), state, coord)
    m = Msg(document=_doc(name="склад.csv"))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(b""))
    assert m.answers[-1][0] == "Нужен файл .xlsx."
    m = Msg(document=_doc(size=3 * 1024 * 1024))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(b""))
    assert m.answers[-1][0] == "Файл больше 2 МБ."
    m = Msg(document=_doc())
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(b"garbage"))
    assert m.answers[-1][0] == "Не удалось прочитать файл — нужен .xlsx."
    data = _xlsx([["Принтер", "X", "новые", 1, None]])
    m = Msg(document=_doc(size=len(data)))
    await stock_handlers.got_import_file(m, state, s, coord, _Bot(data))
    assert m.answers[-1][0].startswith("Нет строк для загрузки.\nстрока 2:")
    assert await state.get_state() == StockForm.import_file.state  # можно прислать другой


async def test_upload_rejected_for_specialist(s, clock_, state):
    _, erlan, _ = await _setup(s)
    cq = Cq("sk_upl")
    await stock_handlers.cb_upload(cq, state, erlan)
    assert cq.alerts == ["Только для координатора"] and cq.message.docs == []
    await state.set_state(StockForm.import_confirm)
    await state.update_data(
        rows=[
            {
                "line": 2,
                "category": "mice",
                "name": "Мышь",
                "condition": "new",
                "qty": 5,
                "min_qty": None,
            }
        ]
    )
    cq = Cq("sk_uplok")
    await stock_handlers.cb_upload_ok(cq, state, s, erlan)
    assert cq.alerts == ["Только для координатора"]
    assert await stock.find_active(s, "mice", "Мышь") is None


async def test_document_in_import_state_routed_to_import():
    staff = SimpleNamespace(status="active", is_admin=False, role="coordinator")
    doc = {"file_id": "f", "file_unique_id": "u", "file_name": "a.xlsx", "file_size": 10}
    names = await _matched_names(_message(document=doc), StockForm.import_file.state, staff)
    assert names[0] == "got_import_file"
