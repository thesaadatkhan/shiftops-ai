"""Interactive context and factual schedule answers, with scripted replies only."""

import json

import pytest

import agent
import eligibility
import tools
from scripted import ScriptedProvider, final_reply, tools_reply
from test_agent import GOOD_FINAL, INVESTIGATION


def ask(conn, state, question, replies, limits=None):
    provider = ScriptedProvider(replies)
    events = list(agent.agent_steps(question, conn, provider, limits, session=state))
    return events, provider


def recommendation(conn, shift, outgoing):
    eligible = eligibility.check_candidates(conn, shift, outgoing)["eligible"]
    chosen = eligible[0]["worker_id"]
    pref = tools.get_preferences(conn, {"shift_id":shift,"candidate_ids":[chosen]})
    final = final_reply(shift_id=shift,outgoing_worker_id=outgoing,recommended_worker_id=chosen,
                       rationale="Eligible, with the retrieved preference.",tradeoff="A recommendation, subject to acceptance.",
                       evidence_refs=[f"ELIG:{shift}:{chosen}",pref["preferences"][0]["evidence_id"]])
    return [tools_reply(("find_shift",{"shift_id":shift,"worker":outgoing})),
            tools_reply(("get_eligible_candidates",{"shift_id":shift,"outgoing_worker_id":outgoing}),
                        ("get_preferences",{"shift_id":shift,"candidate_ids":[chosen]})),final]


def ambiguity():
    return [tools_reply(("find_shift",{"date":"2026-10-17","hall":"Andromeda","worker":"Alex"})),
            final_reply(status="clarification",rationale="Two workers named Alex.",
                        clarification_question="Alex Chen or Alex Rivera?")]


def test_clarification_reply_retains_request_and_narrows_worker(conn):
    state=agent.ChatSession()
    first,_=ask(conn,state,"Alex called out of Andromeda on Saturday.",ambiguity())
    assert first[-1]["status"]=="clarification"
    second,provider=ask(conn,state,"Alex Chen",recommendation(conn,"AND-1017-MOR","W02"))
    assert second[-1]["status"]=="recommendation"
    assert second[-1]["result"]["outgoing_worker_id"]=="W02"
    messages=provider.requests[0]["input"]
    assert any("Saturday" in item.get("content","") for item in messages)
    assert any("Alex Chen or Alex Rivera?" in item.get("content","") for item in messages)
    assert second[-1]["usage"]["model_calls"]==5


def test_clarification_cannot_resolve_the_other_alex(conn):
    state=agent.ChatSession()
    ask(conn,state,"Alex called out of Andromeda on Saturday.",ambiguity())
    replies=recommendation(conn,"AND-1017-EVE","W03")
    events,_=ask(conn,state,"Alex Chen",replies+[replies[-1]])
    assert events[-1]["status"]=="invalid_recommendation"
    assert "clarification names workers" in str(events[-1]["problems"])


def test_followup_explanation_retains_target_evidence_and_budget(conn):
    state=agent.ChatSession()
    first,_=ask(conn,state,"Jordan called out of Andromeda Oct 12 evening",INVESTIGATION+[final_reply(**GOOD_FINAL)])
    follow,provider=ask(conn,state,"Why that worker rather than Riley?",[final_reply(**GOOD_FINAL)])
    assert follow[-1]["status"]=="recommendation"
    assert follow[-1]["usage"]["model_calls"]==5
    assert follow[-1]["usage"]["input_tokens"]==first[-1]["usage"]["input_tokens"]+1500
    assert any("Skyler" in item.get("content","") for item in provider.requests[0]["input"])
    assert "Session totals" in agent.render(follow[-1],conn)


def test_session_new_topic_does_not_reset_spent_budget(conn):
    state=agent.ChatSession()
    ask(conn,state,"Alex called out of Andromeda on Saturday.",ambiguity())
    state.new_topic()
    assert not state.messages and not state.evidence and state.target["shift_id"] is None
    assert state.usage["model_calls"]==2
    assert state.usage["input_tokens"]>0


def test_session_call_cap_cannot_be_reset_by_new_topic(conn):
    state=agent.ChatSession()
    ask(conn,state,"Alex called out of Andromeda on Saturday.",ambiguity())
    state.new_topic()
    events,provider=ask(conn,state,"Another question",[],agent.Limits(max_model_calls=2))
    assert events[-1]["status"]=="call_limit" and not provider.requests


def test_followup_new_explicit_date_cannot_reuse_old_target(conn):
    state=agent.ChatSession()
    ask(conn,state,"Alex called out of Andromeda on Saturday.",ambiguity())
    replies=recommendation(conn,"AND-1017-MOR","W02")
    events,_=ask(conn,state,"Alex Chen on October 18 morning",replies+[replies[-1]])
    assert events[-1]["status"]=="invalid_recommendation"
    assert "2026-10-18" in str(events[-1]["problems"])


def test_separate_sessions_share_no_context(conn):
    first=agent.ChatSession()
    ask(conn,first,"Alex called out of Andromeda on Saturday.",ambiguity())
    second=agent.ChatSession()
    assert second.messages==[] and second.usage["model_calls"]==0


def schedule_replies(conn, hall="Capella", period="Morning"):
    args={"week_start":None,"date":None,"hall":hall,"shift_name":period,"start_time":None}
    record=tools.get_schedule(conn,args)
    return [tools_reply(("get_schedule",args)),
            final_reply(status="schedule",rationale="Assignments from the stored demo week.",
                        evidence_refs=[record["evidence_id"]])]


def test_capella_morning_schedule_has_seven_exact_assignments(conn):
    record=tools.get_schedule(conn,{"hall":"Capella","shift_name":"Morning"})
    assert record["count"]==7
    assert [s["start"][:10] for s in record["shifts"]]==[f"2026-10-{d}" for d in range(12,19)]
    assert all(s["start"].endswith("08:00") and s["assigned_worker"] for s in record["shifts"])


def test_schedule_tool_bounds_and_validation(conn):
    assert "narrow" in tools.dispatch(conn,"get_schedule","{}")["error"]
    for args in ({"hall":"Unknown"},{"week_start":"2026-10-05"},{"start_time":"8am"}):
        assert "error" in tools.dispatch(conn,"get_schedule",json.dumps(args))


def test_schedule_final_displays_python_facts(conn):
    events=list(agent.agent_steps("Which workers are covering all morning shifts at Capella this week?",
                                   conn,ScriptedProvider(schedule_replies(conn))))
    end=events[-1]
    assert end["status"]=="schedule"
    text=agent.render(end,conn)
    assert text.count("8 AM") ==7 and "Sage Turner" in text
    assert "Scheduled assignments (from SQLite)" in text


@pytest.mark.parametrize("hall,period", [("Helix","Morning"),("Capella","Afternoon")])
def test_schedule_cannot_answer_with_wrong_filters(conn,hall,period):
    replies=schedule_replies(conn,hall,period)
    events=list(agent.agent_steps("Which workers are covering all morning shifts at Capella this week?",
                                   conn,ScriptedProvider(replies+[replies[-1]])))
    assert events[-1]["status"]=="invalid_recommendation"


def test_schedule_followup_retains_hall_and_changes_period(conn):
    state=agent.ChatSession()
    ask(conn,state,"Which workers cover all morning shifts at Capella this week?",schedule_replies(conn))
    events,provider=ask(conn,state,"And the afternoon shifts?",schedule_replies(conn,period="Afternoon"))
    assert events[-1]["status"]=="schedule"
    assert all(s["hall"]=="Capella" and s["name"]=="Afternoon" for s in events[-1]["schedule"]["shifts"])
    assert any("Capella" in item.get("content","") for item in provider.requests[0]["input"])


def test_schedule_requires_retrieved_source(conn):
    final=final_reply(status="schedule",rationale="Made up",
                      evidence_refs=["SCHEDULE:2026-10-12:Capella:Morning:*"])
    events=list(agent.agent_steps("Who is covering Capella morning shifts this week?",conn,
                                   ScriptedProvider([final,final])))
    assert events[-1]["status"]=="invalid_recommendation"


def test_interactive_cli_accepts_short_followup(monkeypatch,capsys,seed_db):
    conn=agent.database.connect(seed_db)
    try:
        replies=ambiguity()+recommendation(conn,"AND-1017-MOR","W02")
    finally:
        conn.close()
    answers=iter(["Alex called out of Andromeda on Saturday.","Alex Chen","/quit"])
    monkeypatch.setattr("builtins.input",lambda prompt:next(answers))
    monkeypatch.setattr(agent.database,"DB_PATH",seed_db)
    monkeypatch.setattr(agent,"provider_from_env",lambda:(ScriptedProvider(replies),"scripted"))
    assert agent.main(["--chat"])==0
    output=capsys.readouterr().out
    assert "Alex Chen" in output and "Chat closed." in output and "Step-5" in output


def test_schedule_and_chat_leave_database_unchanged(conn,seed_db):
    import hashlib
    before=hashlib.sha256(seed_db.read_bytes()).hexdigest()
    state=agent.ChatSession()
    ask(conn,state,"Who is covering Capella morning shifts this week?",schedule_replies(conn))
    assert before==hashlib.sha256(seed_db.read_bytes()).hexdigest()


@pytest.mark.parametrize("question",[
    "Which workers are covering Capella shifts from 1 PM to 6 PM this week?",
    "Which workers are covering Capella morning shifts on October 14?",
    "Which workers are covering Capella morning shifts in the week of October 5?",
])
def test_schedule_rejects_wrong_times_day_or_week(conn,question):
    replies=schedule_replies(conn)
    events=list(agent.agent_steps(question,conn,ScriptedProvider(replies+[replies[-1]])))
    assert events[-1]["status"]=="invalid_recommendation"


def test_schedule_date_filter_returns_one_day_and_validates(conn):
    args={"week_start":None,"date":"2026-10-14","hall":"Capella","shift_name":"Morning","start_time":None}
    record=tools.get_schedule(conn,args)
    assert record["count"]==1 and record["shifts"][0]["start"]=="2026-10-14 08:00"
    final=final_reply(status="schedule",rationale="This day's assignment.",evidence_refs=[record["evidence_id"]])
    events=list(agent.agent_steps("Who is covering Capella morning shifts on October 14?",conn,
                                   ScriptedProvider([tools_reply(("get_schedule",args)),final])))
    assert events[-1]["status"]=="schedule"


def test_fully_named_followup_retains_same_callout(conn):
    state=agent.ChatSession()
    ask(conn,state,"Jordan called out for Andromeda's October 12 evening shift.",INVESTIGATION+[final_reply(**GOOD_FINAL)])
    events,provider=ask(conn,state,"Why that worker for Andromeda on October 12?",[final_reply(**GOOD_FINAL)])
    assert events[-1]["status"]=="recommendation"
    assert len(provider.requests[0]["input"])>2


def test_schedule_revalidates_assignment_freshness(seed_db,tmp_path):
    import shutil
    import database
    path=tmp_path/"changed.db"
    shutil.copyfile(seed_db,path)
    conn=database.connect(path,read_only=False)
    try:
        record=tools.get_schedule(conn,{"hall":"Capella","shift_name":"Morning"})
        conn.execute("UPDATE assignments SET worker_id='W30' WHERE shift_id=?",(record["shifts"][0]["shift_id"],))
        conn.commit()
        final=json.loads(final_reply(status="schedule",rationale="facts",evidence_refs=[record["evidence_id"]]).text)
        problems=agent.validate_final(conn,final,tools.evidence_ids(record),agent.new_target(),
                                     {record["evidence_id"]:record},
                                     "Who is covering Capella morning shifts this week?")
        assert any("changed since retrieval" in p for p in problems)
    finally:
        conn.close()


def test_followup_revalidates_current_eligibility(seed_db,tmp_path):
    import shutil
    import database
    path=tmp_path/"changed.db"
    shutil.copyfile(seed_db,path)
    conn=database.connect(path,read_only=False)
    try:
        state=agent.ChatSession()
        ask(conn,state,"Jordan called out of Andromeda Oct 12 evening",INVESTIGATION+[final_reply(**GOOD_FINAL)])
        conn.execute("UPDATE workers SET active=0 WHERE id='W14'")
        conn.commit()
        events,_=ask(conn,state,"Why that worker?",[final_reply(**GOOD_FINAL)]*2)
        assert events[-1]["status"]=="invalid_recommendation"
        assert "inactive" in str(events[-1]["problems"])
    finally:
        conn.close()
