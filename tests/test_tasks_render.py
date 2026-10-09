from datetime import date

from helpdesk.db.models import TaskComment
from helpdesk.tasks import render, rules
from tests.factories import make_staff, make_task

TODAY = date(2026, 10, 7)


def test_parse_task_id():
    for q in ("З-12", "з12", "З 12", " Z-12 ", "z12"):
        assert render.parse_task_id(q) == 12, q
    for q in ("12", "#12", "З-", "З-1234567", "Зина", "З-12а"):
        assert render.parse_task_id(q) is None, q


def test_parse_due():
    assert render.parse_due("15.10", TODAY) == date(2026, 10, 15)
    assert render.parse_due("07.10", TODAY) == TODAY
    assert render.parse_due("01.03", TODAY) == date(2027, 3, 1)  # без года — ближайшая будущая
    assert render.parse_due("15.10.2027", TODAY) == date(2027, 10, 15)
    assert render.parse_due("06.10.2026", TODAY) is None  # прошлое
    assert render.parse_due("31.02", TODAY) is None
    assert render.parse_due("завтра", TODAY) is None
    assert render.parse_due("1.1.26", TODAY) is None


def test_due_from_choice():
    assert render.due_from_choice("today", TODAY) == TODAY
    assert render.due_from_choice("tomorrow", TODAY) == date(2026, 10, 8)
    assert render.due_from_choice("week", TODAY) == date(2026, 10, 14)


async def test_card_text(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_task(
        s,
        erlan,
        requester="Иванова, каб. 214",
        due_date=date(2026, 10, 10),
        description="Заменить картридж в принтере HP",
    )
    names = {erlan.id: "Ерлан"}
    assert render.card(t, names, TODAY) == (
        f"📌 З-{t.id} · Helpdesk · срок 10.10\n"
        "Для: Иванова, каб. 214\n"
        "Заменить картридж в принтере HP\n"
        "Создал: Ерлан · Исполнитель: Ерлан"
    )
    t.kind, t.status, t.assignee_id, t.due_date, t.requester = (
        "coordinator",
        "queued",
        None,
        date(2026, 10, 6),
        None,
    )
    assert render.card(t, names, TODAY).splitlines() == [
        f"📌 З-{t.id} · 🧭 Координатор · ⚠️ срок 06.10",
        "Заменить картридж в принтере HP",
        "Создал: Ерлан · Исполнитель: — (ищем)",
    ]
    t.status, t.closed_by_id, t.due_date = "closed", erlan.id, None
    text = render.card(t, names, TODAY)
    assert text.splitlines()[0] == f"📌 З-{t.id} · 🧭 Координатор"
    assert text.endswith("✔️ Закрыта: Ерлан")


async def test_buttons_for(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    t = await make_task(s, erlan)
    n = t.id
    close, help_ = ("✅ Закрыть", f"tk_close:{n}"), ("🆘 Нужна помощь", f"tk_help:{n}")
    assert render.buttons_for(t, erlan, can_help=True) == [[close, help_]]
    assert render.buttons_for(t, erlan, can_help=False) == [[close]]
    assert render.buttons_for(t, tima, can_help=False) is None
    t.assignee_id = tima.id  # создатель, но не исполнитель
    assert render.buttons_for(t, erlan, can_help=False) == [[close]]
    t.status, t.assignee_id, t.taken_at = "queued", None, None
    assert render.buttons_for(t, tima, can_help=False) == [[("✅ Принять", f"tk_take:{n}")]]
    t.taken_at = clock_.now  # уже кто-то брал — теперь это «Взять»
    assert render.buttons_for(t, erlan, can_help=False) == [
        [("✋ Взять", f"tk_take:{n}")],
        [close],
    ]
    t.kind = "coordinator"
    assert render.buttons_for(t, tima, can_help=False) is None
    assert render.buttons_for(t, coord, can_help=False) == [[("✋ Взять", f"tk_take:{n}")]]
    t.status = "closed"
    assert render.buttons_for(t, coord, can_help=False) is None


async def test_rules(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    off = await make_staff(s, tg_id=4, name="Ушёл", role="coordinator", status="disabled")
    assert rules.can_do(erlan, "helpdesk") and not rules.can_do(erlan, "coordinator")
    assert rules.can_do(coord, "coordinator") and not rules.can_do(off, "coordinator")
    assert [st.id for st in await rules.eligible(s, "helpdesk")] == [erlan.id, coord.id]
    assert [st.id for st in await rules.coordinators(s)] == [coord.id]
    t = await make_task(s, erlan, kind="coordinator", status="queued", assignee=None)
    assert [st.id for st in await rules.participants(s, t)] == [erlan.id, coord.id]
    assert rules.can_see(erlan, t) and rules.can_see(coord, t)
    tima = await make_staff(s, tg_id=2, name="Тимур")
    assert not rules.can_see(tima, t)


async def test_summary_and_lines(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    assert render.summary(
        {
            "kind": "coordinator",
            "description": "Ноутбук",
            "requester": "Бухгалтерия",
            "due": "2026-10-10",
            "file_name": "photo_x.jpg",
        }
    ) == (
        "Проверьте задачу:\nТип: 🧭 Координатор\nОписание: Ноутбук\nДля: Бухгалтерия\n"
        "Срок: 10.10.2026\nВложение: photo_x.jpg"
    )
    assert render.summary({"kind": "helpdesk", "description": "X", "due": None}).endswith(
        "Срок: без срока"
    )
    t = await make_task(s, erlan, due_date=date(2026, 10, 6), description="a" * 80)
    assert render.list_line(t, TODAY) == f"⚠️ З-{t.id} · до 06.10 · {'a' * 49}…"
    t.due_date = None
    assert render.list_line(t, TODAY) == f"З-{t.id} · без срока · {'a' * 49}…"
    names = {erlan.id: "Ерлан"}
    assert render.open_line(t, names, TODAY) == f"З-{t.id} · Helpdesk · Ерлан · {'a' * 49}…"
    assert render.search_line(t, names) == f"З-{t.id} · 07.10 · {'a' * 49}… · в работе · Ерлан"
    c = TaskComment(
        task_id=t.id, staff_id=erlan.id, kind="image", text="скрин", created_at=clock_.now
    )
    d = render.details(t, names, [c], TODAY)
    assert d.startswith(f"📌 З-{t.id} · Helpdesk")
    assert d.endswith("Комментарии:\n07.10 14:00 Ерлан: [🖼 фото] скрин")


async def test_details_long_comments_keep_card_header(s, clock_):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    t = await make_task(s, erlan)
    comments = [TaskComment(task_id=t.id, staff_id=erlan.id, text="к" * 300) for _ in range(30)]
    for c in comments:
        c.created_at = clock_.now
    out = render.details(t, {erlan.id: "Ерлан"}, comments, date(2026, 10, 7))
    assert out.startswith(f"📌 З-{t.id}")
    assert len(out) <= 4000
