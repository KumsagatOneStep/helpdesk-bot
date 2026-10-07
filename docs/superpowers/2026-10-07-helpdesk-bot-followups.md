# Helpdesk-бот v1 — отложенные замечания ревью

Отложено при ревью ветки feature/helpdesk-v1 (2026-10-07). Не блокируют запуск.

- Task 1: minor (deferred): tests/test_config.py mutates clock.LOCAL_TZ without restore (sets default value, harmless); config.admin_ids bare ValueError on malformed ADMIN_TG_IDS.
- Task 2: minor (deferred): test_defaults_use_clock checks only Ticket defaults; conftest alembic subprocess failure gives raw CalledProcessError (no "run make test-db" hint).
- Task 3: minor (deferred): worker delete_card treats transient tg.delete errors as permanent (edits+marks); send-then-DB-fail resends (at-least-once, add comment); test_card_for_taken_ticket_is_skipped doesn't assert status skipped; _on_failed silent if ticket missing.
- Task 4: minor (deferred): download leaves truncated dest on mid-stream failure; send_file sync read_bytes; parse KeyError on missing idMessage (poller catches & drops after 3 — verify in T12); no test for deletedMessage / send_file without caption.
- Task 5: minor (deferred): _forward doesn't guard missing assignee; broadcast with nobody on shift deletes cards & warns admins on every client message (no test); no kk/keep_staff_id tests; rebroadcast test doesn't assert delete targets card 500.
- Task 6: minor (deferred): lost-take fallback text "🔒 #N уже взята" vs "взял <имя>"; send_staff_reply can enqueue empty wa_text; missing tests (admin close foreign, take closed, reply to foreign in_progress); uneven flushing in take/set_active/link.
- Task 7: minor (deferred): dedup is select-then-insert (safe because poller is serial; IntegrityError would be retried/dropped by poller); first-contact sticker gets no unsupported reply; no tests for bad_location/unsupported during intake.
- Task 8: minor (deferred): env admin re-enabled on /start; enable() doesn't deliver queue; missing tests (deliver_queue_to skips non-new, non-admin disable/toggle, stf_en buttons, disabled staff_line).
- Task 9: minor (deferred): run loop untested; waited measured from created_at; empty-shift reminder still bumps seq.
- Task 10: minor (deferred): parse_ticket_id isdigit accepts unicode digits (`/find ²` → ValueError); ILIKE doesn't escape %/_; open_tickets_text [:4000] cuts mid-line, ticket_details uncapped; reaction avg over created-in-period; test gaps (week/month compute, empty export, prev_month, search by id); header wording vs spec.
- Task 10: minor (deferred): staff name column not passed through _safe (Telegram profile names); _safe ignores leading tab/CR; NUL (\x00) in WhatsApp text would fail the Postgres insert (poller drops after 3 attempts) — consider stripping NUL in inbound.
- Task 11: minor (deferred): no behavioral tests for IsActive/IsAdmin/DbMiddleware/_reply_to_client/AiogramSender; missing file_size skips 20MB check and TelegramBadRequest on download is uncaught; transaction open during slow I/O; setup() mutates module-level router; fallback answers in group chats.
- Task 12: minor (deferred): POSTGRES_PASSWORD with URL-special chars breaks DATABASE_URL (README should say letters/digits); background task crash leaves bot half-alive (no done-callback); poller re-downloads file per retry (orphans); uv:latest unpinned; poller.run untested.
- Final: parked — set_shift(off) card updates unordered vs reminders (Postgres-detected deadlock, very unlikely) — Ruling: defer; fix = order_by(TgCard.ticket_id) — cost if wrong: one aborted transaction, retried by user action.
- Final: parked — card in flight during /off survives until next /on (narrow window); missing file on disk retries ~42 min instead of failing permanently — Ruling: defer — cost if wrong: occasional duplicate card / delayed chat.
