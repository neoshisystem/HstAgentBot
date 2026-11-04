import asyncio
from types import SimpleNamespace

import admin_panel
import rep_panel
import user_portal


class FakeMessage:
    def __init__(self,text=""):
        self.text=text
        self.replies=[]
    async def reply_text(self,text,**kwargs):
        self.replies.append((text,kwargs))


class FakeQuery:
    def __init__(self,data):
        self.data=data
        self.answers=[]
    async def answer(self,text=None,show_alert=False):
        self.answers.append((text,show_alert))
    async def edit_message_text(self,text,**kwargs):
        pass


def text_update(text,uid):
    message=FakeMessage(text)
    update=SimpleNamespace(message=message,effective_user=SimpleNamespace(id=uid,full_name="Tester",first_name="Test",username="tester"),
        effective_chat=SimpleNamespace(id=uid,type="private"),effective_message=SimpleNamespace(message_thread_id=None),callback_query=None)
    return update,message


def callback_update(data,uid):
    query=FakeQuery(data)
    update=SimpleNamespace(callback_query=query,message=FakeMessage(),effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=SimpleNamespace(id=uid,full_name="Tester",first_name="Test",username="tester"),
        effective_chat=SimpleNamespace(id=uid,type="private"))
    return update,query


def callback_values(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_expired_admin_form_consumes_input_without_xui_call(monkeypatch):
    update,msg=text_update("1001",93001)
    context=SimpleNamespace(user_data={"admin_state":{"action":"inspect","ts":admin_panel.time.time()-61,"return_callback":"admin:scope"}})
    monkeypatch.setattr(admin_panel.asyncio,"to_thread",lambda *a,**k: (_ for _ in ()).throw(AssertionError("expired input reached X-UI")))
    assert asyncio.run(admin_panel.process_admin_text_input(update,context)) is True
    assert "admin_state" not in context.user_data
    assert "مهلت زمانی تکمیل این فرم" in msg.replies[0][0]
    assert "admin:scope" in callback_values(msg.replies[0][1]["reply_markup"])


def test_expired_rep_form_consumes_input():
    update,msg=text_update("random text",93002)
    context=SimpleNamespace(user_data={"rep_state":{"action":"awaiting_search_query","rep_id":3,"ts":rep_panel.time.time()-61,"return_callback":"rep:main"}})
    assert asyncio.run(rep_panel.process_rep_text_input(update,context,{"id":3,"scope_filter":"all"})) is True
    assert "rep_state" not in context.user_data
    assert "مهلت زمانی تکمیل این فرم" in msg.replies[0][0]
    assert "rep:main" in callback_values(msg.replies[0][1]["reply_markup"])


def test_expired_user_form_consumes_input():
    update,msg=text_update("random text",93003)
    context=SimpleNamespace(user_data={"user_state":{"action":"awaiting_link_code","user_id":93003,"ts":user_portal.time.time()-61,"return_callback":"user:main"}})
    assert asyncio.run(user_portal.process_user_text_input(update,context)) is True
    assert "user_state" not in context.user_data
    assert "مهلت زمانی تکمیل این فرم" in msg.replies[0][0]
    assert "user:main" in callback_values(msg.replies[0][1]["reply_markup"])


def test_card_parser_pipe_supports_multiword_holder():
    parsed=admin_panel.parse_card_settings_input("6037-9911-2233-4455 | علی رضا محمدی | بانک سامان")
    assert parsed==("6037991122334455","علی رضا محمدی","بانک سامان")


def test_card_parser_supports_newlines_and_smart_spaces():
    lines=admin_panel.parse_card_settings_input("6037 9911 2233 4455\nعلی رضا محمدی\nبانک اقتصاد نوین")
    spaces=admin_panel.parse_card_settings_input("6037-9911-2233-4455 علی رضا محمدی بانک سامان")
    assert lines==("6037991122334455","علی رضا محمدی","بانک اقتصاد نوین")
    assert spaces==("6037991122334455","علی رضا محمدی","بانک سامان")


def test_admin_validation_error_preserves_state_and_offers_controls():
    now=admin_panel.time.time()
    update,msg=text_update("bad-number",93004)
    state={"action":"charge_user","target_user_id":11,"ts":now-1,"return_callback":"admin:users:1"}
    context=SimpleNamespace(user_data={"admin_state":state})
    assert asyncio.run(admin_panel.process_admin_text_input(update,context)) is True
    assert context.user_data["admin_state"] is state and state["attempts"]==1 and state["ts"]>=now
    controls=callback_values(msg.replies[0][1]["reply_markup"])
    assert "admin:retry_form" in controls and "admin:cancel_form" in controls


def test_rep_validation_error_preserves_state_and_offers_controls(monkeypatch):
    now=rep_panel.time.time()
    update,msg=text_update("Customer name",93005)
    state={"action":"awaiting_new_acc_name","intent_id":"test-intent","rep_id":9,"ts":now-1,"return_callback":"rep:new_acc_plans"}
    context=SimpleNamespace(user_data={"rep_state":state})
    monkeypatch.setattr(rep_panel.database,"get_rep_action_intent",lambda *a,**k:{"status":"pending"})
    monkeypatch.setattr(rep_panel.database,"set_rep_action_intent_customer_name",lambda *a,**k:False)
    assert asyncio.run(rep_panel.process_rep_text_input(update,context,{"id":9,"scope_filter":"all"})) is True
    assert context.user_data["rep_state"] is state and state["attempts"]==1
    controls=callback_values(msg.replies[0][1]["reply_markup"])
    assert "rep:retry_form" in controls and "rep:cancel_form" in controls


def test_user_validation_error_preserves_state_and_offers_controls(monkeypatch):
    now=user_portal.time.time()
    update,msg=text_update("NO-SUCH-ACCOUNT",93006)
    state={"action":"awaiting_link_code","user_id":93006,"ts":now-1,"return_callback":"user:main"}
    context=SimpleNamespace(user_data={"user_state":state})
    monkeypatch.setattr(user_portal.database,"link_user_to_account",lambda *a,**k:{"status":"not_found"})
    assert asyncio.run(user_portal.process_user_text_input(update,context)) is True
    assert context.user_data["user_state"] is state and state["attempts"]==1
    controls=callback_values(msg.replies[0][1]["reply_markup"])
    assert "user:retry_form" in controls and "user:cancel_form" in controls


def test_admin_cancel_form_clears_state_and_returns_to_parent(monkeypatch):
    update,q=callback_update("admin:cancel_form",93007)
    context=SimpleNamespace(user_data={"admin_state":{"action":"charge_user","return_callback":"admin:main"}})
    called=[]
    monkeypatch.setattr(admin_panel.database,"get_user_role",lambda *a,**k:("super_admin",None))
    async def dashboard(update,context,edit_message=False): called.append(edit_message)
    monkeypatch.setattr(admin_panel,"show_admin_dashboard",dashboard)
    asyncio.run(admin_panel.handle_admin_callbacks(update,context))
    assert "admin_state" not in context.user_data and q.data=="admin:main" and called==[True]


def test_rep_cancel_form_clears_state_and_returns_to_parent(monkeypatch):
    update,q=callback_update("rep:cancel_form",93008)
    context=SimpleNamespace(user_data={"rep_state":{"action":"awaiting_search_query","return_callback":"rep:main"}})
    called=[]
    async def dashboard(update,context,rep,edit_message=False): called.append(edit_message)
    monkeypatch.setattr(rep_panel,"show_rep_dashboard",dashboard)
    asyncio.run(rep_panel.handle_rep_callbacks(update,context,{"id":9,"name":"Rep"}))
    assert "rep_state" not in context.user_data and q.data=="rep:main" and called==[True]


def test_user_cancel_form_clears_state_and_returns_to_parent(monkeypatch):
    update,q=callback_update("user:cancel_form",93009)
    context=SimpleNamespace(user_data={"user_state":{"action":"awaiting_link_code","return_callback":"user:main"}})
    called=[]
    monkeypatch.setattr(user_portal.database,"is_user_blocked",lambda _uid:False)
    monkeypatch.setattr(user_portal.database,"get_user_role",lambda *a,**k:("end_user",None))
    async def dashboard(update,context,edit_message=False): called.append(edit_message)
    monkeypatch.setattr(user_portal,"show_user_portal",dashboard)
    asyncio.run(user_portal.handle_user_callbacks(update,context))
    assert "user_state" not in context.user_data and q.data=="user:main" and called==[True]
