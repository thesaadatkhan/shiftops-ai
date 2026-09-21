// Shared by the two Employees views: where the employee API lives, and how
// stored values are turned into text a supervisor reads. Both the list and
// the details view show student types and timetable readiness, and they must
// not be able to drift apart by each keeping their own copy of the wording.

export const EMPLOYEES_URL = 'http://127.0.0.1:8000/api/employees'

export const STUDENT_TYPE_LABELS = {
  undergraduate: 'Undergraduate',
  masters: "Master's",
}

export function studentTypeLabel(employee) {
  return STUDENT_TYPE_LABELS[employee.student_type] ?? employee.student_type
}

// Timetable readiness FOR THE DISPLAYED WEEK, which is NOT shift eligibility
// and NOT active status. Each label answers a different question, and they
// are deliberately not collapsed:
//
//   Not set up          nobody has entered any semester for this worker.
//   Other semester only they have a timetable, but for a different period -
//                       an expired one, or one still ahead of this week.
//   Awaiting confirm.   something covers this week but nobody has checked it.
//   Part of week        confirmed for some days of this week, not all seven.
//   Confirmed           confirmed for the whole week. With zero class blocks
//                       that is a deliberate "no classes this week", which
//                       stays distinct from "Not set up".
//
// This is the readiness of the week on screen. An individual semester's own
// confirmation is a separate thing, shown per semester in the details view.
export const TIMETABLE_LABELS = {
  missing: 'Not set up',
  outside_period: 'Other semester only',
  unconfirmed: 'Awaiting confirmation',
  partial: 'Part of week',
  confirmed: 'Confirmed',
}

export function timetableLabel(status) {
  return TIMETABLE_LABELS[status] ?? status
}

// `scheduling_ready` (Codex review finding 6) is the stricter fact backend
// eligibility actually requires: confirmed AND non-provisional dates, not
// merely "confirmed". A semester the Phase 5C migration left confirmed but
// still provisional (nobody has accepted its assumed dates) reads as plain
// "Confirmed" under `timetableLabel` alone, which is misleading - Coverage
// would still correctly refuse that worker as `timetable_not_confirmed`.
// This annotates ONLY that one genuinely ambiguous case, so `timetableLabel`
// itself is not changed and every other status keeps its plain wording.
export function timetableStatusDisplay(employee) {
  const label = timetableLabel(employee.timetable_status)
  if (employee.timetable_status === 'confirmed' && !employee.scheduling_ready) {
    return `${label} (provisional dates - not scheduling-ready)`
  }
  return label
}

// Class blocks are stored as a weekday number with 0 = Monday (D025), which
// is what Python's datetime.weekday() produces. Read back into words here;
// the number is the stored value and is never shown.
export const WEEKDAY_NAMES = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
]

export function weekdayName(dayOfWeek) {
  return WEEKDAY_NAMES[dayOfWeek] ?? `Day ${dayOfWeek}`
}
