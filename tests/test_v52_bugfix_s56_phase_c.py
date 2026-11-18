"""Phase C regression tests — Persian transparency, readable references, delivery.

Seven defects are pinned here:

1. ``ARCH-UX-002`` — the super-admin menu still carried a raw English label
   (``(root_admin_rep)``) and two same-named plan buttons; the settings centre
   printed the raw English key of every parameter and rendered a bare ``0``/``1``
   with no explanation of what changing it does.
2. An abuse report was identified by a 24-character random token while support
   tickets had already moved to short sequential codes.
3. Service-layer English messages reached customers and representatives through
   ``render_error``.
4. ``backup.py`` forwarded only the topic id to the reporting facade, which ignores
   it — so the group case silently depended on topic registration and the
   super-admin direct-message fallback could never work.
5. ``rep_panel.py`` carried two invalid escape sequences.
6. ``tests/test_backup_reporting.py`` failed whenever the deployment's ``.env``
   set ``BACKUP_ENCRYPTION_KEY``.
7. The UI audit harness left the persona switcher on whatever it last clicked.

Offline only: no panel, no Telegram, no network.
"""
import asyncio
import base64
import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import abuse_service
import admin_panel
import backup
import config
import database
import internal_reporting
import report_router
import service_errors_fa
import support_service

ROOT_TG_ID = 101258148
BOT_ID = 9004
NOW = 1794600000
REPO_ROOT = Path(__file__).resolve().parent.parent


def _enable_crypto(monkeypatch):
    """A synthetic 32-byte master key, so encrypted columns and archives work."""
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_phasec_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


def _migrated_db(monkeypatch, tmp_path, *, filename):
    return helpers.make_db(database, monkeypatch, tmp_path, filename=filename)


def _apply_migrations(conn):
    from migrations_runner import apply_migrations
    apply_migrations(conn, REPO_ROOT / "migrations", now=NOW)


def _callbacks(rows):
    return [button.callback_data for row in rows for button in row]


def _labels(rows):
    return [button.text for row in rows for button in row]


def _seed_bot(conn):
    conn.execute(
        "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,"
        "status,verified_at,revision) VALUES(?,NULL,1,'x','bot','active',?,1)",
        (BOT_ID, NOW))
    conn.commit()


# --------------------------------------------------------------------------
# 1. Persian transparency (ARCH-UX-002)
# --------------------------------------------------------------------------

def test_the_super_admin_menu_carries_no_technical_english_label(monkeypatch):
    monkeypatch.setattr(admin_panel, "_v52_admin_enabled", lambda: True)
    keyboard = admin_panel.build_admin_main_keyboard()
    for label in _labels(keyboard.inline_keyboard):
        assert not re.search(r"\([a-z_]{4,}\)", label), label
        assert service_errors_fa.is_persian(label), label
    assert "root_admin_rep" not in str(keyboard)


def test_the_two_plan_buttons_are_unambiguous(monkeypatch):
    """S57 merged the two tariff menus: one entry, one name, both routes alive."""
    monkeypatch.setattr(admin_panel, "_v52_admin_enabled", lambda: True)
    rows = admin_panel.build_admin_main_keyboard().inline_keyboard
    by_callback = {button.callback_data: button.text for row in rows for button in row}
    assert by_callback["v52:plans"] == "🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها"
    # The duplicate entry point is gone from the v5.2 menu, so the operator can no
    # longer reach two differently-rendered tariff pages.
    assert "admin:plans" not in by_callback
    # The legacy keyboard must name the same surface the same way.
    monkeypatch.setattr(admin_panel, "_v52_admin_enabled", lambda: False)
    legacy = {button.callback_data: button.text
              for row in admin_panel.build_admin_main_keyboard().inline_keyboard
              for button in row}
    assert legacy["admin:plans"] == "🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها"


def test_every_settings_category_page_hides_the_raw_english_key(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-settings.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        seen = 0
        for cat_key, _title, _keys in admin_panel.SETTINGS_CATEGORIES:
            text, rows = admin_panel.build_settings_category_page(cat_key)
            members = admin_panel._settings_category_members(cat_key)
            for setting in members:
                seen += 1
                key = setting["key"]
                # The key may live in the callback data, never in what is rendered.
                assert key not in text, f"{key} leaked into the {cat_key} page body"
                assert key not in " ".join(_labels(rows)), f"{key} leaked into a button"
            assert service_errors_fa.is_persian(text)
        assert seen >= 20, "the scan must cover the whole settings centre"


def test_every_visible_setting_has_a_persian_title_and_explanation(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-metadata.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        visible = [s for s in database.get_all_settings()
                   if s["key"] not in admin_panel.HIDDEN_SETTINGS]
        assert visible
        for setting in visible:
            assert service_errors_fa.is_persian(admin_panel.setting_label(setting)), setting
            assert service_errors_fa.is_persian(
                admin_panel.setting_description(setting)), setting
            # No label may be the raw key.
            assert admin_panel.setting_label(setting) != setting["key"]


def test_an_internal_counter_is_not_offered_as_a_setting(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-hidden.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            # The counter is created on the first ticket code, so create it here:
            # an absent row would make this test prove nothing.
            conn.execute(
                "INSERT INTO system_settings(key,value,label,description) "
                "VALUES('ticket_code_seq','10002','','') "
                "ON CONFLICT(key) DO UPDATE SET value='10002'")
            conn.commit()
        all_keys = {s["key"] for s in database.get_all_settings()}
        assert "ticket_code_seq" in all_keys, "the counter must exist"
        for cat_key in [c for c, _t, _k in admin_panel.SETTINGS_CATEGORIES] + ["other"]:
            assert "ticket_code_seq" not in {
                s["key"] for s in admin_panel._settings_category_members(cat_key)}


def test_a_switch_reads_as_active_or_inactive():
    on = {"key": "v52_ui_enabled", "value": "1", "label": "رابط", "description": "توضیح"}
    off = dict(on, value="0")
    assert admin_panel.setting_value_display("v52_ui_enabled", "1") == "🟢 فعال (1)"
    assert admin_panel.setting_value_display("v52_ui_enabled", "0") == "🔴 غیرفعال (0)"
    assert admin_panel.setting_value_display("v52_persona_switcher_enabled", "1") == "🟢 فعال (1)"
    # A topic id of 0 is an identifier, not a switch.
    assert admin_panel.setting_value_display("backups_topic_id", "0") == "`0`"
    assert admin_panel.setting_value_display("main_group_id", "-1004377897090") == "`-1004377897090`"
    # A keyword setting renders its Persian meaning.
    assert "باز" in admin_panel.setting_value_display("registration_mode", "open")
    assert setting_label_is_persian(on) and setting_label_is_persian(off)


def setting_label_is_persian(setting):
    return service_errors_fa.is_persian(admin_panel.setting_label(setting))


def test_a_setting_without_persian_metadata_still_renders_persian():
    bare = {"key": "some_new_flag", "value": "1", "label": "", "description": ""}
    assert admin_panel.setting_label(bare) == admin_panel.SETTING_LABEL_FALLBACK
    assert service_errors_fa.is_persian(admin_panel.setting_label(bare))
    assert service_errors_fa.is_persian(admin_panel.setting_description(bare))
    english = dict(bare, label="Persona Switcher")
    assert admin_panel.setting_label(english) == admin_panel.SETTING_LABEL_FALLBACK
    assert "Persona" not in admin_panel.setting_label(english)


def test_the_declared_registration_keys_all_resolve(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-regkeys.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        declared = dict((k, c) for k, _t, keys in admin_panel.SETTINGS_CATEGORIES
                        for c in [None] for k in keys)
        live = {s["key"] for s in database.get_all_settings()}
        missing = sorted(k for k in declared if k not in live)
        assert missing == [], f"declared but absent from system_settings: {missing}"


def test_the_setting_prompt_page_hides_the_raw_key(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-prompt.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()

        class FakeMessage:
            def __init__(self):
                self.text = "page"
                self.photo = None
                self.document = None
                self.caption = None
                self.edits = []

            async def edit_message_text(self, text, **kwargs):
                self.edits.append((text, kwargs))
                self.text = text

        class FakeQuery:
            def __init__(self):
                self.data = "admin:prompt_setting:v52_ui_enabled:features"
                self.message = FakeMessage()

            async def answer(self, *a, **k):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.message.edits.append((text, kwargs))
                self.message.text = text

        message = FakeMessage()
        query = FakeQuery()
        query.message = message
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=ROOT_TG_ID, full_name="Root", username="r"),
            effective_chat=SimpleNamespace(id=ROOT_TG_ID, type="private"),
            effective_message=message, message=message,
        )
        context = SimpleNamespace(bot=SimpleNamespace(id=BOT_ID, username="b"),
                                  user_data={}, chat_data={}, bot_data={})
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
        asyncio.run(admin_panel._handle_admin_callbacks_impl(update, context))

        assert message.edits, "the prompt must render"
        body = message.edits[-1][0]
        assert "v52_ui_enabled" not in body
        assert "کلید سیستمی" not in body
        assert "🟢 فعال (1)" in body
        assert service_errors_fa.is_persian(body)


def test_the_live_ticket_status_fallback_labels_carry_no_english():
    for code, label in support_service.TICKET_STATUS_LABELS.items():
        assert service_errors_fa.is_persian(label), (code, label)
        assert not re.search(r"\([A-Za-z]{3,}\)", label), (code, label)


# --------------------------------------------------------------------------
# 2. A readable abuse-report reference
# --------------------------------------------------------------------------

def test_an_abuse_report_gets_a_five_digit_sequential_code(monkeypatch, tmp_path):
    _enable_crypto(monkeypatch)
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-abuse.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        ctx = {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID}
        first = abuse_service.create_abuse_report(ctx, "first", now=NOW)
        second = abuse_service.create_abuse_report(ctx, "second", now=NOW + 1)
        for report in (first, second):
            code = report["ticket_id"]
            assert code.isdigit(), code
            assert len(code) == 5, code
            assert int(code) >= support_service.TICKET_CODE_FLOOR
        assert int(second["ticket_id"]) == int(first["ticket_id"]) + 1


def test_an_abuse_code_shares_the_ticket_sequence_without_colliding(monkeypatch, tmp_path):
    _enable_crypto(monkeypatch)
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-abuse-seq.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            ticket_code = support_service.next_ticket_code(conn)
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        report = abuse_service.create_abuse_report(
            {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID}, "body", now=NOW)
        assert report["ticket_id"] != ticket_code
        assert int(report["ticket_id"]) == int(ticket_code) + 1


def test_a_legacy_random_abuse_code_still_resolves(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-abuse-legacy.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            legacy = "AbCdEf0123456789-_xy"
            conn.execute(
                "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
                "ticket_type,assigned_key,status,created_at,updated_at) "
                "VALUES(?,?,?,NULL,NULL,'abuse_report','root','unseen',?,?)",
                (legacy, ROOT_TG_ID, BOT_ID, NOW, NOW))
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        reports = abuse_service.list_abuse_reports(
            {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID, "persona": "admin", "is_root": True})
        assert legacy in {r["ticket_id"] for r in reports}


# --------------------------------------------------------------------------
# 3. No English reaches a page
# --------------------------------------------------------------------------

_RAISE_RE = re.compile(r"""raise\s+\w*Error\(\s*f?["']([^"']{12,})["']""")


def _english_service_messages():
    found = set()
    for name in ("access_service.py", "support_service.py", "access_ui.py"):
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        for match in _RAISE_RE.finditer(source):
            message = match.group(1)
            if not service_errors_fa.is_persian(message):
                found.add(message)
    return found


def test_the_scan_finds_the_messages_it_claims_to_cover():
    messages = _english_service_messages()
    assert len(messages) >= 20, sorted(messages)
    assert "actor cannot grant access to this account" in messages
    assert "self-grants are not supported" in messages
    assert "active grant not found" in messages


def test_the_translator_never_returns_english_for_any_service_message():
    for message in _english_service_messages():
        rendered = service_errors_fa.translate(message)
        assert service_errors_fa.is_persian(rendered), (message, rendered)
        assert not re.search(r"[A-Za-z]{4,}", rendered), (message, rendered)


def test_the_named_messages_get_a_real_translation_not_the_generic_fallback():
    for message in ("actor cannot grant access to this account",
                    "actor cannot revoke access to this account",
                    "self-grants are not supported",
                    "active grant not found",
                    "grant target does not match",
                    "grant access_level is invalid",
                    "support ticket does not exist",
                    "actor cannot reply to this support ticket"):
        rendered = service_errors_fa.translate(message)
        assert rendered != service_errors_fa.DEFAULT_FALLBACK, message
        assert service_errors_fa.is_persian(rendered)


def test_a_message_that_appends_detail_still_translates():
    # access_service raises f"unsupported account action: {action}".
    rendered = service_errors_fa.translate("unsupported account action: issue")
    assert rendered != service_errors_fa.DEFAULT_FALLBACK
    assert service_errors_fa.is_persian(rendered)


def test_a_persian_message_passes_through_and_an_unknown_one_degrades():
    persian = "این حساب در دسترس نیست."
    assert service_errors_fa.translate(persian) == persian
    unknown = service_errors_fa.translate("some brand new internal failure")
    assert unknown == service_errors_fa.DEFAULT_FALLBACK
    assert service_errors_fa.is_persian(unknown)
    assert service_errors_fa.translate(None) == service_errors_fa.DEFAULT_FALLBACK


def test_the_access_page_renders_a_persian_error(monkeypatch, tmp_path):
    import access_ui
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-access-ui.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        page = access_ui.preview_revoke(
            {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID},
            account_id=999999, grantee_tg_id=ROOT_TG_ID)
        assert page.kind == "error"
        assert service_errors_fa.is_persian(page.text)
        assert not re.search(r"[A-Za-z]{4,}", page.text), page.text


def test_the_support_page_renders_a_persian_error(monkeypatch, tmp_path):
    import support_ui
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-support-ui.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        page = support_ui.reply_or_escalate(
            {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID}, "999999", body="x",
            confirm=True)
        assert page.kind == "error"
        assert service_errors_fa.is_persian(page.text)
        assert not re.search(r"[A-Za-z]{4,}", page.text), page.text


# --------------------------------------------------------------------------
# 4. The backup destination
# --------------------------------------------------------------------------

def test_a_group_topic_destination_carries_chat_and_thread(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-dest-group.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            conn.execute("UPDATE system_settings SET value='-1004377897090' "
                         "WHERE key='main_group_id'")
            conn.execute("UPDATE system_settings SET value='8' WHERE key='backups_topic_id'")
            conn.commit()
        monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", 0)
        destination = backup.resolve_backup_destination()
        assert destination["configured"] is True
        assert destination["target"] == "group_topic"
        assert destination["chat_id"] == -1004377897090
        assert destination["topic_id"] == 8


def test_the_direct_message_fallback_does_not_require_a_group(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-dest-dm.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            conn.execute("UPDATE system_settings SET value='0' WHERE key='main_group_id'")
            conn.execute("UPDATE system_settings SET value='8' WHERE key='backups_topic_id'")
            conn.execute("UPDATE system_settings SET value=? WHERE key='admin_telegram_id'",
                         (str(ROOT_TG_ID),))
            conn.commit()
        monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", 0)
        destination = backup.resolve_backup_destination()
        # No group configured, but a super-admin exists: the backup must still go.
        assert destination["configured"] is True
        assert destination["target"] == "admin_dm"
        assert destination["chat_id"] == ROOT_TG_ID
        assert destination["topic_id"] is None


def test_the_backup_job_forwards_the_resolved_destination(monkeypatch, tmp_path):
    captured = {}
    _enable_crypto(monkeypatch)

    class Uploader:
        def send_telegram_document(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"ok": True, "message_id": 1}

    monkeypatch.setattr(backup, "backup_and_report", Uploader())
    monkeypatch.setattr(backup, "resolve_backup_destination", lambda: {
        "configured": True, "target": "group_topic",
        "chat_id": -1004377897090, "topic_id": 8})
    monkeypatch.setattr(backup, "DB_PATH", str(tmp_path / "live.sqlite3"))
    conn = sqlite3.connect(backup.DB_PATH)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path / "backups"))

    backup.run_backup()

    assert captured["kwargs"]["chat_id"] == -1004377897090
    assert captured["kwargs"]["thread_id"] == 8
    assert captured["kwargs"]["parse_mode"] == "HTML"


def test_an_explicit_destination_reaches_the_outbox_with_its_thread(monkeypatch, tmp_path):
    _enable_crypto(monkeypatch)
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-queue.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            conn.commit()

        archive = tmp_path / "synthetic.tar.gz.enc"
        archive.write_bytes(b"ciphertext-marker")
        monkeypatch.setattr(backup, "verify_backup_archive", lambda _p: {"sha256": "a" * 64})

        result = internal_reporting.send_telegram_document(
            None, str(archive), "caption", parse_mode="HTML",
            chat_id=-1004377897090, thread_id=8)
        assert result["ok"] is True and result["queued"] is True, result

        with database.get_db() as conn:
            row = dict(conn.execute(
                "SELECT bot_id,chat_id,thread_id,purpose,resource_type "
                "FROM delivery_outbox ORDER BY id DESC LIMIT 1").fetchone())
        assert row["chat_id"] == -1004377897090
        assert row["thread_id"] == 8
        assert row["bot_id"] == BOT_ID
        assert row["purpose"] == "backup_document"
        assert row["resource_type"] == "encrypted_backup"


def test_a_direct_message_destination_reaches_the_outbox_without_a_thread(monkeypatch, tmp_path):
    _enable_crypto(monkeypatch)
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-queue-dm.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            conn.commit()

        archive = tmp_path / "synthetic.tar.gz.enc"
        archive.write_bytes(b"ciphertext-marker")
        monkeypatch.setattr(backup, "verify_backup_archive", lambda _p: {"sha256": "a" * 64})

        result = internal_reporting.send_telegram_document(
            None, str(archive), "caption", chat_id=ROOT_TG_ID, thread_id=None)
        assert result["ok"] is True

        with database.get_db() as conn:
            row = dict(conn.execute(
                "SELECT chat_id,thread_id FROM delivery_outbox "
                "ORDER BY id DESC LIMIT 1").fetchone())
        assert row["chat_id"] == ROOT_TG_ID
        assert row["thread_id"] is None


def test_the_router_path_still_works_when_no_destination_is_given(monkeypatch, tmp_path):
    """The privacy property: without an explicit destination the router decides."""
    _enable_crypto(monkeypatch)
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-queue-router.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            conn.execute(
                "INSERT INTO forum_topics(bot_id,owner_key,topic_key,title_fa,group_chat_id,"
                "thread_id,state,generation,updated_at) "
                "VALUES(?,'root','backupfile','بکاپ‌ها',-1004377897090,8,'active',1,?)",
                (BOT_ID, NOW))
            conn.commit()

        archive = tmp_path / "synthetic.tar.gz.enc"
        archive.write_bytes(b"ciphertext-marker")
        monkeypatch.setattr(backup, "verify_backup_archive", lambda _p: {"sha256": "a" * 64})

        result = internal_reporting.send_telegram_document(None, str(archive), "caption")
        assert result["ok"] is True, result

        with database.get_db() as conn:
            row = dict(conn.execute(
                "SELECT bot_id,thread_id FROM delivery_outbox "
                "ORDER BY id DESC LIMIT 1").fetchone())
        assert row["bot_id"] == BOT_ID
        assert row["thread_id"] == 8


def test_the_primary_bot_helper_reads_the_registry(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phasec-primary.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_bot(conn)
            assert report_router.primary_bot_id(conn) == BOT_ID
            conn.execute("UPDATE bot_registry SET status='disabled'")
            conn.commit()
            try:
                report_router.primary_bot_id(conn)
            except report_router.ReportRouteError:
                pass
            else:
                raise AssertionError("a disabled primary bot must not be usable")


# --------------------------------------------------------------------------
# 5. No module emits a SyntaxWarning
# --------------------------------------------------------------------------

def test_no_module_emits_a_syntax_warning():
    offenders = []
    for path in sorted(REPO_ROOT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(source, str(path), "exec")
            except SyntaxError as exc:
                offenders.append(f"{path.name}: SyntaxError {exc}")
                continue
        for item in caught:
            if issubclass(item.category, SyntaxWarning):
                offenders.append(f"{path.name}: {item.message}")
    assert offenders == [], offenders


def test_the_rep_panel_invoice_line_is_a_plain_string():
    source = (REPO_ROOT / "rep_panel.py").read_text(encoding="utf-8")
    assert "\\`" not in source
    assert "🧾 **فاکتور:** `" in source


# --------------------------------------------------------------------------
# 6. The backup suite is independent of the deployment environment
# --------------------------------------------------------------------------

def test_the_backup_reporting_suite_passes_with_the_deployment_key_set():
    """The historical plaintext harness must not depend on the ambient .env."""
    environment = dict(os.environ)
    environment["BACKUP_ENCRYPTION_KEY"] = "synthetic-deployment-key-for-this-check"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_backup_reporting.py"],
        cwd=str(REPO_ROOT), env=environment, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]


# --------------------------------------------------------------------------
# 7. The audit harness restores the persona
# --------------------------------------------------------------------------
# The re-pin lives in the Hermes-side audit harness
# (skills/hst-agentbot/hstagentbot-v52-sandbox-development/scripts/v52_ui_audit.py),
# which is not part of this repository and is not readable from a CI runner. It is
# verified by running the harness, not by a product test: with the re-pin in place
# the permission-error lines in the audit log dropped from 81 to 67 and no
# PermissionError remained, while the board stayed at 238/238 OK.
