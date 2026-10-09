from datetime import date, datetime, timedelta
from io import BytesIO

from openpyxl import load_workbook

from helpdesk import clock
from helpdesk.db.models import Message, TicketEvent
from helpdesk.stats import export
from helpdesk.stats import service as stats
from tests.factories import make_client, make_staff, make_task, make_ticket


def L(*args):  # локальное время Алматы
    return datetime(*args, tzinfo=clock.LOCAL_TZ)


async def _dataset(s, now):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    tima = await make_staff(s, tg_id=2, name="Тимур")
    c1 = await make_client(s)
    c2 = await make_client(s, phone="77017654321", full_name="Бауыржан Ахметов", location="Склад")
    c3 = await make_client(
        s, phone="77010000003", full_name="Сауле", location="Бухгалтерия, каб. 214"
    )

    def at(**kw):
        return now - timedelta(**kw)

    t1 = await make_ticket(s, c1, status="closed", assignee=erlan)
    t1.created_at, t1.first_taken_at = at(hours=2), at(hours=1, minutes=50)
    t1.closed_at, t1.closed_by_id = at(hours=1), erlan.id
    t2 = await make_ticket(s, c2, status="in_progress", assignee=tima, text="Нет интернета")
    t2.created_at, t2.first_taken_at = at(minutes=30), at(minutes=20)
    t3 = await make_ticket(s, c3, text="Мышь")
    t3.created_at = at(minutes=5)
    old = await make_ticket(s, c1, status="closed", assignee=tima, text="Старое")
    old.created_at = old.first_taken_at = old.closed_at = at(days=40)
    old.closed_by_id = tima.id
    s.add_all(
        [
            TicketEvent(ticket_id=t1.id, type="created", created_at=t1.created_at),
            TicketEvent(
                ticket_id=t1.id, type="taken", staff_id=erlan.id, created_at=t1.first_taken_at
            ),
            TicketEvent(ticket_id=t1.id, type="closed", staff_id=erlan.id, created_at=t1.closed_at),
            TicketEvent(
                ticket_id=t2.id, type="taken", staff_id=tima.id, created_at=t2.first_taken_at
            ),
            TicketEvent(
                ticket_id=old.id, type="taken", staff_id=tima.id, created_at=old.first_taken_at
            ),
        ]
    )
    await s.flush()
    return t1, t2, t3, old


def test_period_bounds_follow_local_calendar(clock_):
    now = clock_.now  # среда 07.10.2026 14:00 по Алматы
    assert stats.period_bounds("today", now) == (L(2026, 10, 7), None)
    assert stats.period_bounds("week", now) == (L(2026, 10, 5), None)
    assert stats.period_bounds("month", now) == (L(2026, 10, 1), None)
    assert stats.period_bounds("prev_month", now) == (L(2026, 9, 1), L(2026, 10, 1))
    assert stats.period_bounds("all", now) == (None, None)


async def test_compute_today(s, clock_):
    await _dataset(s, clock_.now)
    st = await stats.compute(s, *stats.period_bounds("today", clock_.now))
    assert (st.created, st.closed, st.in_progress, st.queued) == (3, 1, 1, 1)
    assert st.avg_reaction_min == 10.0 and st.avg_resolve_min == 60.0
    assert st.per_staff == [
        stats.StaffStat("Ерлан", 1, 1, 60.0),
        stats.StaffStat("Тимур", 1, 0, None),
    ]
    assert st.top_locations == [("Бухгалтерия, каб. 214", 2), ("Склад", 1)]
    text = stats.render_stats(st, "сегодня")
    assert text.splitlines()[:4] == [
        "📊 Статистика: сегодня",
        "Создано: 3 · Закрыто: 1",
        "Сейчас в работе: 1 · в очереди: 1",
        "Ср. время реакции: 10 мин · решения: 60 мин",
    ]
    assert "• Тимур — взял 1, закрыл 0, ср. —" in text


async def test_compute_all_time(s, clock_):
    await _dataset(s, clock_.now)
    st = await stats.compute(s, None, None)
    assert (st.created, st.closed) == (4, 2)


def test_parse_ticket_id():
    assert stats.parse_ticket_id("#12") == 12 and stats.parse_ticket_id(" 125 ") == 125
    assert stats.parse_ticket_id("87011234567") is None
    assert stats.parse_ticket_id("Айгерим") is None
    assert stats.parse_ticket_id("²") is None and stats.parse_ticket_id("١٢") is None


async def test_search(s, clock_):
    t1, t2, t3, old = await _dataset(s, clock_.now)
    assert [t.id for t, _, _ in await stats.search(s, "айгер")] == [old.id, t1.id]
    assert [t.id for t, _, _ in await stats.search(s, "склад")] == [t2.id]
    rows = await stats.search(s, "+7 701 765")
    assert [(t.id, name) for t, _, name in rows] == [(t2.id, "Тимур")]
    assert stats.search_line(*rows[0]) == f"#{t2.id} · 07.10 · Бауыржан Ахметов · в работе · Тимур"


async def test_ticket_details_shows_progress(s, clock_):
    t1, *_ = await _dataset(s, clock_.now)
    text = await stats.ticket_details(s, t1.id)
    assert text.splitlines()[0] == f"📄 Заявка #{t1.id} — закрыта · Ерлан"
    assert "07.10 12:00 создана" in text
    assert "07.10 12:10 взял Ерлан" in text and "07.10 13:00 закрыл Ерлан" in text
    assert await stats.ticket_details(s, 999) is None


async def test_conversation_lists_files(s, clock_):
    _, t2, *_ = await _dataset(s, clock_.now)
    s.add(
        Message(
            client_id=t2.client_id,
            ticket_id=t2.id,
            direction="in",
            kind="image",
            text="скрин",
            file_path="/f/a.jpg",
        )
    )
    await s.flush()
    conv = await stats.conversation(s, t2.id)
    assert conv.chunks[0].startswith(f"💬 Переписка по заявке #{t2.id}")
    assert "Нет интернета" in conv.chunks[0]
    assert conv.files == [("/f/a.jpg", "photo", "скрин")]


async def test_open_tickets_text(s, clock_):
    _, t2, t3, _ = await _dataset(s, clock_.now)
    text = await stats.open_tickets_text(s)
    assert f"#{t2.id} · Бауыржан Ахметов · Тимур · 30 мин" in text
    assert f"#{t3.id} · Сауле · в очереди · 5 мин" in text


async def test_export_xlsx(s, clock_):
    t1, *_ = await _dataset(s, clock_.now)
    data = await export.export_xlsx(s, *stats.period_bounds("today", clock_.now))
    ws = load_workbook(BytesIO(data)).active
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == export.HEADERS
    assert len(rows) == 4
    assert list(rows[1]) == [
        t1.id,
        datetime(2026, 10, 7, 12, 0),
        "Айгерим Сериккызы",
        "Бухгалтерия, каб. 214",
        "+77011234567",
        "Не печатает принтер",
        "Ерлан",
        "закрыта",
        datetime(2026, 10, 7, 12, 10),
        datetime(2026, 10, 7, 13, 0),
        10,
        60,
    ]


async def test_export_xlsx_neutralizes_formulas_and_control_chars(s, clock_):
    c = await make_client(s, full_name="=cmd\x0b", location="@x")
    await make_ticket(s, c, text="=1+1\x01")
    data = await export.export_xlsx(s, *stats.period_bounds("all", clock_.now))
    ws = load_workbook(BytesIO(data)).active
    row = list(ws.iter_rows(min_row=2, values_only=True))[0]
    assert row[5] == "'=1+1" and row[2] == "'=cmd" and row[3] == "'@x"
    cell = ws.cell(row=2, column=6)
    assert cell.data_type != "f"


async def _tasks_dataset(s, now):
    erlan = await make_staff(s, tg_id=1, name="Ерлан")
    coord = await make_staff(s, tg_id=3, name="Айжан", role="coordinator")
    on_time = await make_task(s, erlan, status="closed", due_date=date(2026, 10, 7))
    on_time.closed_at, on_time.closed_by_id = now, erlan.id
    late = await make_task(
        s,
        erlan,
        kind="coordinator",
        assignee=coord,
        status="closed",
        due_date=date(2026, 10, 6),
        description="=SUM(A1)",
    )
    late.closed_at, late.closed_by_id = now, coord.id
    overdue = await make_task(s, erlan, due_date=date(2026, 10, 5), requester="каб. 1")
    old = await make_task(s, erlan)
    old.created_at = now - timedelta(days=40)
    await s.flush()
    return on_time, late, overdue, old


async def test_compute_tasks(s, clock_):
    await _tasks_dataset(s, clock_.now)
    start, end = stats.period_bounds("today", clock_.now)
    ts = await stats.compute_tasks(s, start, end, date(2026, 10, 7))
    assert (ts.created, ts.closed, ts.overdue_now, ts.closed_late) == (3, 2, 1, 1)
    assert ts.by_kind == {"helpdesk": (2, 1), "coordinator": (1, 1)}
    assert ts.per_staff == [("Айжан", 1), ("Ерлан", 1)]
    text = stats.render_task_stats(ts)
    assert text.splitlines()[:3] == [
        "📌 Задачи",
        "Создано: 3 · Закрыто: 2 (с опозданием: 1)",
        "Просрочено сейчас: 1",
    ]
    assert "• 🧭 Координатор — создано 1, закрыто 1" in text


async def test_export_has_tasks_sheet(s, clock_):
    on_time, late, overdue, old = await _tasks_dataset(s, clock_.now)
    data = await export.export_xlsx(s, *stats.period_bounds("today", clock_.now))
    ws = load_workbook(BytesIO(data))["Задачи"]
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == export.TASK_HEADERS
    assert [r[0] for r in rows[1:]] == [f"З-{on_time.id}", f"З-{late.id}", f"З-{overdue.id}"]
    assert list(rows[2]) == [
        f"З-{late.id}",
        "Координатор",
        "закрыта",
        "'=SUM(A1)",
        None,
        "Ерлан",
        "Айжан",
        datetime(2026, 10, 6),
        datetime(2026, 10, 7, 14, 0),
        datetime(2026, 10, 7, 14, 0),
        "да",
    ]
    assert rows[1][-1] == "нет" and rows[3][-1] == "да" and rows[3][4] == "каб. 1"
