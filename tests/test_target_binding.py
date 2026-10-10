"""The run's target must agree with explicit shift details in the question (review recheck, P1).

A first find_shift resolution that contradicts a stated shift ID, date, weekday, shift period, or time is
not bound; uncertain wording also requires a clarification. Matching requests still bind and succeed."""

from datetime import date

import pytest

import agent
import eligibility
from agent import agent_steps, question_constraints, question_mismatches
from scripted import ScriptedProvider, final_reply, tools_reply
from test_agent import GOOD_FINAL, INVESTIGATION

WEEK = date(2026, 10, 12)


def mismatches(conn, question, shift_id):
    return question_mismatches(question_constraints(question, WEEK), eligibility.get_shift(conn, shift_id))


def run_s1(conn, question):
    """INVESTIGATION resolves Jordan's AND-1012-EVE (Mon Oct 12, 18:00-23:00) on the first lookup."""
    events = list(agent_steps(question, conn, ScriptedProvider(INVESTIGATION + [final_reply(**GOOD_FINAL)] * 2)))
    notes = [e["result"]["target_check"] for e in events if e["type"] == "observation" and "target_check" in e["result"]]
    return events[-1], notes[0]


# ----- full loop: the reviewer's failing questions and other contradictions are refused -----

@pytest.mark.parametrize("question, reason", [
    ("Jordan called out for Andromeda on October 13 evening. Who should I ask first?", "says 2026-10-13"),
    ("Jordan called out for Andromeda on October 12 morning. Who should I ask first?", "says Morning"),
    ("Jordan called out of Andromeda on Tuesday evening.", "says 2026-10-13"),
    ("Jordan can't work AND-1013-EVE.", "names shift ['AND-1013-EVE']"),
    ("Jordan called out of Andromeda on Oct 12 at 1 PM.", "mentions 13:00"),
    ("Jordan called out of Andromeda 10/14.", "says 2026-10-14"),
    ("Jordan called out of Andromeda tonight.", "relative day 'tonight'"),
    ("Jordan called out of Andromeda Monday, October 13, evening.", "different dates"),
])
def test_contradicting_first_resolution_is_not_bound(conn, question, reason):
    end, note = run_s1(conn, question)
    assert note.startswith("target conflict") and reason in note
    assert end["status"] == "invalid_recommendation"
    assert any("target conflict" in p for p in end["problems"])


def test_clarification_is_still_accepted_after_a_mismatch(conn):
    clarify = final_reply(status="clarification", rationale="Jordan works Oct 12 evening, not Oct 13.",
                          clarification_question="Did you mean Jordan's October 12 evening shift?",
                          evidence_refs=["SHIFT:AND-1012-EVE"])
    events = list(agent_steps("Jordan called out for Andromeda on October 13 evening.", conn,
                              ScriptedProvider(INVESTIGATION[:1] + [clarify])))
    assert events[-1]["status"] == "clarification"


# ----- full loop: matching requests in supported forms bind and succeed -----

@pytest.mark.parametrize("question", [
    "Jordan called out for Andromeda's October 12 evening shift. Who should I ask first?",
    "Jordan called out of Andromeda on Monday evening.",
    "Jordan can't make the Oct. 12th 6 PM Andromeda shift.",
    "Jordan called out for 2026-10-12 18:00 at Andromeda.",
    "Jordan called out of the 6-11 pm shift on 10/12.",
    "Jordan called out of AND-1012-EVE.",
    "Jordan called out Monday night.",
    "Jordan called out of Andromeda.",  # no explicit details: nothing to contradict
])
def test_matching_request_binds_and_recommendation_is_accepted(conn, question):
    end, note = run_s1(conn, question)
    assert note == "target bound: AND-1012-EVE / W01 Jordan Lee"
    assert end["type"] == "final" and end["status"] == "recommendation"


def test_later_switch_after_binding_is_a_conflict(conn):
    morgan = tools_reply(("find_shift", {"shift_id": "HEL-1013-MOR", "date": None, "hall": None, "shift_name": None,
                                         "start_time": None, "worker": "Morgan"}))
    question = "Jordan and Morgan both called out. Who covers?"
    events = list(agent_steps(question, conn, ScriptedProvider(
        INVESTIGATION[:1] + [morgan] + INVESTIGATION[1:] + [final_reply(**GOOD_FINAL)] * 2)))
    notes = [e["result"]["target_check"] for e in events if e["type"] == "observation" and "target_check" in e["result"]]
    assert notes[0].startswith("target bound") and "after the run was bound" in notes[1]
    assert events[-1]["status"] == "invalid_recommendation"


# ----- the reader itself -----

def test_supported_date_forms():
    for text in ("October 12", "oct 12", "Oct. 12th", "12 October", "12th of Oct", "2026-10-12", "10/12",
                 "10/12/2026", "Monday", "mon"):
        assert question_constraints(f"Jordan {text} evening", WEEK)["dates"] == {date(2026, 10, 12)}, text
    assert question_constraints("Jordan on Sunday", WEEK)["dates"] == {date(2026, 10, 18)}
    assert question_constraints("Feb 30 shift", WEEK)["uncertain"]


def test_periods_and_times():
    c = question_constraints("Elliot's early-morning shift at 4 a.m.", WEEK)
    assert c["periods"] == {"Early Morning"} and c["times"] == {"04:00"}
    assert question_constraints("Monday night", WEEK)["periods"] == {"Evening", "Overnight"}
    assert question_constraints("from 1 PM to 6:00 pm", WEEK)["ranges"] == {("13:00", "18:00")}
    assert question_constraints("the 23:00 overnight", WEEK)["times"] == {"23:00"}
    assert question_constraints("noon or midnight", WEEK)["times"] == {"12:00", "00:00"}
    assert question_constraints("Who covers Sam?", WEEK) == {"shift_ids": set(), "dates": set(), "periods": set(),
                                                             "ranges": set(), "starts": set(), "ends": set(),
                                                             "times": set(), "uncertain": []}
    # "afternoon" contains "noon" but is not a time.
    assert question_constraints("Friday afternoon", WEEK)["times"] == set()


def test_early_morning_keeps_calendar_and_operating_date_ambiguity(conn):
    # AND-1011-EM starts Oct 12 04:00 (operating day Oct 11); AND-1012-EM starts Oct 13 04:00 (operating day Oct 12).
    question = "Andromeda October 12 early morning"
    assert mismatches(conn, question, "AND-1011-EM") == []
    assert mismatches(conn, question, "AND-1012-EM") == []
    assert mismatches(conn, "Andromeda October 13 early morning", "AND-1012-EM") == []
    assert mismatches(conn, "Andromeda October 14 early morning", "AND-1012-EM") != []


def test_overnight_end_time_counts_as_a_stated_time(conn):
    # AND-1012-OVN runs Oct 12 23:00 - Oct 13 04:00.
    assert mismatches(conn, "the overnight ending at 4 AM on October 12", "AND-1012-OVN") == []
    assert mismatches(conn, "the overnight at 10 PM on October 12", "AND-1012-OVN") != []


def test_week_comes_from_seed_metadata(conn):
    assert eligibility.evaluated_week(conn) == WEEK
    assert agent.WEEKDAYS["friday"] == 4


# ----- explicit time ranges: both endpoints must match in their roles (review recheck) -----

@pytest.mark.parametrize("question, reason", [
    ("Jordan called out for Andromeda on October 12 from 6 PM to 10 PM. Who should I ask first?", "gives 18:00-22:00"),
    ("Jordan called out for Andromeda on October 12 from 7 PM to 11 PM. Who should I ask first?", "gives 19:00-23:00"),
    ("Jordan called out of Andromeda Oct 12, 18:00-22:00.", "gives 18:00-22:00"),
    ("Jordan called out of Andromeda Oct 12, 11 PM to 6 PM.", "gives 23:00-18:00"),  # endpoints reversed
    ("Jordan called out of Andromeda Oct 12 starting at 7 PM.", "starts at 19:00"),
    ("Jordan called out of Andromeda Oct 12 until 10 PM.", "ends at 22:00"),
    ("Jordan called out of Andromeda Oct 12, 7 to 11.", "needs AM/PM"),
    ("Jordan called out of Andromeda Oct 12, 11-4 am.", "ambiguous"),
])
def test_range_or_role_contradiction_is_not_bound(conn, question, reason):
    end, note = run_s1(conn, question)
    assert note.startswith("target conflict") and reason in note
    assert end["status"] == "invalid_recommendation"


@pytest.mark.parametrize("question", [
    "Jordan called out for Andromeda on October 12 from 6 PM to 11 PM.",
    "Jordan called out of Andromeda Oct 12, 6-11 pm.",
    "Jordan called out of Andromeda Oct 12, 18:00-23:00.",
    "Jordan called out of Andromeda Oct 12 between 6 pm and 11 pm.",
    "Jordan called out of Andromeda Oct 12, 6 PM until 11 PM.",
    "Jordan called out of Andromeda Oct 12 starting at 6 PM.",
    "Jordan called out of Andromeda Oct 12 ending at 11 PM.",
])
def test_matching_range_or_role_binds_and_succeeds(conn, question):
    end, note = run_s1(conn, question)
    assert note == "target bound: AND-1012-EVE / W01 Jordan Lee"
    assert end["type"] == "final" and end["status"] == "recommendation"


@pytest.mark.parametrize("question, shift_id", [
    ("Andromeda October 12 overnight, 11 PM to 4 AM", "AND-1012-OVN"),     # crosses midnight
    ("Andromeda October 12 overnight from 23:00 to 04:00", "AND-1012-OVN"),
    ("Andromeda October 13 early morning, 4-8 am", "AND-1012-EM"),
    ("Capella October 12 morning, 8 am to 1 pm", "CAP-1012-MOR"),
    ("Capella October 12 afternoon, 1-6 pm", "CAP-1012-AFT"),
    ("Capella October 12 afternoon, noon to 6 pm", None),                 # 12:00-18:00 contradicts 13:00-18:00
])
def test_daytime_and_overnight_ranges(conn, question, shift_id):
    if shift_id is None:
        assert any("gives 12:00-18:00" in p for p in mismatches(conn, question, "CAP-1012-AFT"))
    else:
        assert mismatches(conn, question, shift_id) == []
        assert mismatches(conn, question.replace("4 AM", "3 AM").replace("04:00", "03:00")
                          .replace("8 am", "7 am").replace("1 pm", "2 pm").replace("6 pm", "5 pm"), shift_id) != []


def test_range_reader_forms():
    def times(text):
        c = question_constraints(text, WEEK)
        return c["ranges"], c["starts"], c["ends"], c["times"], bool(c["uncertain"])
    assert times("6-11 pm") == ({("18:00", "23:00")}, set(), set(), set(), False)  # start inherits PM
    assert times("6 pm - 11") == ({("18:00", "23:00")}, set(), set(), set(), False)  # end inherits PM
    assert times("11 PM to 4 AM") == ({("23:00", "04:00")}, set(), set(), set(), False)
    assert times("between 4 and 8 am") == ({("04:00", "08:00")}, set(), set(), set(), False)
    assert times("noon to 5 pm") == ({("12:00", "17:00")}, set(), set(), set(), False)
    assert times("starting at 6 PM") == (set(), {"18:00"}, set(), set(), False)
    assert times("until midnight") == (set(), set(), {"00:00"}, set(), False)
    assert times("the 6 PM shift") == (set(), set(), set(), {"18:00"}, False)
    assert times("7 to 11")[4] and times("11-4 am")[4] and times("6 pm to 6 pm")[4] and times("13 pm-2 pm")[4]
