"""Correct-shift protection: Python never locks a shift that contradicts explicit details in the request."""

from datetime import date

import pytest

import agent
import eligibility
from agent import agent_steps, merge_details, question_details, shift_conflicts
from scripted import S1_FIND, S1_INVESTIGATION, ScriptedProvider, find_args, s1_answer, tools_reply

WEEK = date(2026, 10, 12)


def conflicts(conn, text, shift_id):
    return shift_conflicts(question_details(text, WEEK), eligibility.get_shift(conn, shift_id))


def run(conn, question, replies):
    events = list(agent_steps(question, conn, ScriptedProvider(replies)))
    notes = [e["result"]["target_check"] for e in events if e["type"] == "tool" and "target_check" in e["result"]]
    return events[-1], notes


# AND-1012-EVE is Jordan's Andromeda shift, Mon Oct 12, 6 PM-11 PM.
@pytest.mark.parametrize("question, reason", [
    ("Jordan called out for Andromeda on October 13 evening.", "2026-10-13"),
    ("Jordan called out for Andromeda on October 12 morning.", "Morning"),
    ("Jordan called out of Andromeda on Tuesday evening.", "2026-10-13"),
    ("Jordan can't work AND-1013-EVE.", "AND-1013-EVE"),
    ("Jordan called out of Andromeda on Oct 12 at 1 PM.", "13:00"),
    ("Jordan called out of Andromeda 10/14.", "2026-10-14"),
    ("Jordan called out of Andromeda tonight.", "'tonight'"),
    ("Jordan called out Monday, October 13, evening.", "more than one date"),
    ("Jordan called out for Andromeda on October 12 from 6 PM to 10 PM.", "18:00-22:00"),
    ("Jordan called out for Andromeda on October 12 from 7 PM to 11 PM.", "19:00-23:00"),
    ("Jordan called out of Andromeda Oct 12, 18:00-22:00.", "18:00-22:00"),
    ("Jordan called out of Andromeda Oct 12, 11 PM to 6 PM.", "23:00-18:00"),
    ("Jordan called out of Andromeda Oct 12 starting at 7 PM.", "starts at 19:00"),
    ("Jordan called out of Andromeda Oct 12 until 10 PM.", "ends at 22:00"),
    ("Jordan called out of Andromeda Oct 12, 7 to 11.", "needs AM/PM"),
    ("Jordan called out of Andromeda Oct 12, 11-4 am.", "ambiguous"),
    ("Jordan called out of Helix on Oct 12 evening.", "Helix"),
])
def test_contradictions_are_detected(conn, question, reason):
    assert any(reason in problem for problem in conflicts(conn, question, "AND-1012-EVE"))


@pytest.mark.parametrize("question", [
    "Jordan called out for Andromeda's October 12 evening shift.",
    "Jordan called out of Andromeda on Monday evening.",
    "Jordan can't make the Oct. 12th 6 PM Andromeda shift.",
    "Jordan called out for 2026-10-12 18:00 at Andromeda.",
    "Jordan called out of the 6-11 pm shift on 10/12.",
    "Jordan called out of Andromeda October 12 from 6 PM to 11 PM.",
    "Jordan called out of Andromeda Oct 12, 18:00-23:00.",
    "Jordan called out of Andromeda Oct 12 starting at 6 PM.",
    "Jordan called out of Andromeda Oct 12 ending at 11 PM.",
    "Jordan called out of AND-1012-EVE.",
    "Jordan called out Monday night.",
    "Jordan called out of Andromeda.",
])
def test_matching_requests_have_no_conflicts(conn, question):
    assert conflicts(conn, question, "AND-1012-EVE") == []


@pytest.mark.parametrize("question, shift_id", [
    ("Andromeda October 12 overnight, 11 PM to 4 AM", "AND-1012-OVN"),   # crosses midnight
    ("Andromeda October 13 early morning, 4-8 am", "AND-1012-EM"),       # starts Oct 13, operating day Oct 12
    ("Andromeda October 12 early morning", "AND-1011-EM"),               # one date can mean two early mornings
    ("Andromeda October 12 early morning", "AND-1012-EM"),
    ("Capella October 12 morning, 8 am to 1 pm", "CAP-1012-MOR"),
    ("Capella October 12 afternoon, 1-6 pm", "CAP-1012-AFT"),
])
def test_daytime_and_overnight_ranges_match(conn, question, shift_id):
    assert conflicts(conn, question, shift_id) == []


def test_wrong_daytime_and_overnight_ranges_are_detected(conn):
    assert any("12:00-18:00" in p for p in conflicts(conn, "Capella October 12 afternoon, noon to 6 pm", "CAP-1012-AFT"))
    assert conflicts(conn, "Andromeda October 12 overnight, 11 PM to 3 AM", "AND-1012-OVN") != []
    assert conflicts(conn, "Andromeda October 14 early morning", "AND-1012-EM") != []


def test_supported_forms_are_read():
    for text in ("October 12", "oct 12", "Oct. 12th", "2026-10-12", "10/12", "Monday", "mon"):
        assert question_details(f"Jordan {text} evening", WEEK)["dates"] == {date(2026, 10, 12)}, text
    d = question_details("Elliot's early-morning shift at 4 a.m.", WEEK)
    assert d["periods"] == {"Early Morning"} and d["times"] == {"04:00"}
    assert question_details("Monday night", WEEK)["periods"] == {"Evening", "Overnight"}
    assert question_details("6 pm - 11", WEEK)["ranges"] == {("18:00", "23:00")}  # end inherits PM
    assert question_details("noon or midnight", WEEK)["times"] == {"12:00", "00:00"}
    assert question_details("Friday afternoon", WEEK)["times"] == set()          # "afternoon" is not "noon"
    assert question_details("Feb 30 shift", WEEK)["unclear"]["dates"]
    assert question_details("Who covers Sam?", WEEK)["unclear"] == {}


def test_reply_replaces_only_what_it_mentions():
    first = question_details("Alex called out of Andromeda on Saturday evening.", WEEK)
    name_only = merge_details(first, question_details("Alex Rivera", WEEK))
    assert name_only["dates"] == {date(2026, 10, 17)} and name_only["halls"] == {"Andromeda"}
    assert name_only["periods"] == {"Evening"}
    new_date = merge_details(first, question_details("Sorry, Friday", WEEK))
    assert new_date["dates"] == {date(2026, 10, 16)} and new_date["periods"] == {"Evening"}
    vague = merge_details(first, question_details("tonight", WEEK))
    assert vague["unclear"]["dates"] and vague["dates"] == set()


# ----- through the loop: contradictory resolutions are never locked or answered -----

@pytest.mark.parametrize("question, reason", [
    ("Jordan called out for Andromeda on October 13 evening.", "2026-10-13"),
    ("Jordan called out for Andromeda on October 12 morning.", "Morning"),
    ("Jordan called out for Andromeda on October 12 from 7 PM to 11 PM.", "19:00-23:00"),
    ("Morgan called out of Andromeda Oct 12 evening.", "names Morgan Diaz, not Jordan Lee"),
])
def test_contradicting_resolution_is_not_locked_or_answered(conn, question, reason):
    end, notes = run(conn, question, S1_INVESTIGATION + [s1_answer(), s1_answer()])
    assert notes[0].startswith("not locked") and reason in notes[0]
    assert end["type"] == "stop" and end["status"] == "invalid_answer"
    assert "no shift is locked" in end["message"]


def test_matching_request_locks_and_is_answered(conn):
    end, notes = run(conn, "Jordan called out of Andromeda Oct 12, 6-11 pm.", S1_INVESTIGATION + [s1_answer()])
    assert notes == ["target locked: AND-1012-EVE / W01 Jordan Lee"]
    assert end["type"] == "final" and end["answer"]["status"] == "recommendation"


def test_a_locked_target_never_changes(conn):
    morgan = tools_reply(("find_shift", find_args(shift_id="HEL-1013-MOR", worker="Morgan")))
    morgan_answer = s1_answer(shift_id="HEL-1013-MOR", ask_first="W14", backup=None)
    end, notes = run(conn, "Jordan called out of Andromeda Oct 12 evening.",
                     [S1_FIND, morgan] + S1_INVESTIGATION[1:] + [morgan_answer, morgan_answer])
    assert notes[0].startswith("target locked") and notes[1] == "not changed: this task is locked to AND-1012-EVE / W01"
    assert end["status"] == "invalid_answer" and "locked to AND-1012-EVE" in end["message"]



# ----- review fix 3: an explicit year is honored, never replaced -----

@pytest.mark.parametrize("text, expected", [
    ("October 12, 2026", date(2026, 10, 12)), ("October 12, 2027", date(2027, 10, 12)),
    ("Oct 12 2027", date(2027, 10, 12)), ("October 12", date(2026, 10, 12)), ("10/12/2027", date(2027, 10, 12)),
])
def test_explicit_years(text, expected):
    assert question_details(f"Jordan {text} evening", WEEK)["dates"] == {expected}


def test_wrong_year_is_not_locked(conn):
    assert any("2027-10-12" in p for p in conflicts(conn, "Jordan Andromeda October 12, 2027 evening", "AND-1012-EVE"))
    end, notes = run(conn, "Jordan called out for Andromeda on October 12, 2027 evening.", S1_INVESTIGATION + [s1_answer()] * 2)
    assert notes[0].startswith("not locked") and "2027-10-12" in notes[0]
    assert end["status"] == "invalid_answer"
    ok_end, _ = run(conn, "Jordan called out for Andromeda on October 12, 2026 evening.", S1_INVESTIGATION + [s1_answer()])
    assert ok_end["type"] == "final"
