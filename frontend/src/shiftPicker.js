// Shared progressive shift picker: residence hall -> day/date -> matching
// shift. Used by the Coverage screen and by Employee Details' general "Set a
// shift preference" action, so the two never carry two different
// implementations of the same friendly formatting and filtering rules.
//
// Every function here is a pure, client-side derivation over an
// already-loaded array of shifts ({id, hall, start_datetime, end_datetime}).
// Nothing here calls the backend - see D025 for the stored 'YYYY-MM-DD HH:MM'
// convention these functions read.

const WEEKDAY_NAMES = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
]

const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

/** 'YYYY-MM-DD' -> 'Monday, October 5, 2026'. */
export function friendlyDate(isoDate) {
  const [year, month, day] = isoDate.split('-').map(Number)
  // Constructed from explicit parts, not `new Date(isoDate)`: the latter
  // parses a bare date as UTC midnight, which can read back as the previous
  // day in a negative-UTC-offset timezone. There is no timezone conversion
  // anywhere else in this project either (D025's single local clock).
  const date = new Date(year, month - 1, day)
  return `${WEEKDAY_NAMES[date.getDay()]}, ${MONTH_NAMES[month - 1]} ${day}, ${year}`
}

/** 'HH:MM' (24-hour) -> '8:00 AM' / '1:00 PM' (12-hour). */
export function friendlyTime(time24) {
  const [hourText, minute] = time24.split(':')
  const hour24 = Number(hourText)
  const period = hour24 >= 12 ? 'PM' : 'AM'
  const hour12 = hour24 % 12 === 0 ? 12 : hour24 % 12
  return `${hour12}:${minute} ${period}`
}

function splitDateTime(datetime) {
  const [date, time] = datetime.split(' ')
  return { date, time }
}

/** The calendar date a shift is grouped under: its own start date. */
export function shiftStartDate(shift) {
  return splitDateTime(shift.start_datetime).date
}

/**
 * The friendly time range for one shift, in 12-hour form. A same-day shift
 * reads as '8:00 AM–1:00 PM'; a cross-midnight shift keeps both its dates,
 * '10:00 PM → Monday, October 12, 3:00 AM', so it never looks like it ends
 * before it starts.
 */
export function shiftTimeLabel(shift) {
  const start = splitDateTime(shift.start_datetime)
  const end = splitDateTime(shift.end_datetime)
  if (start.date === end.date) {
    return `${friendlyTime(start.time)}–${friendlyTime(end.time)}`
  }
  return `${friendlyTime(start.time)} → ${friendlyDate(end.date)}, ${friendlyTime(end.time)}`
}

/** The label for one option in the final (matching-shift) picker. */
export function shiftPickerLabel(shift) {
  return `${shiftTimeLabel(shift)} · Shift #${shift.id}`
}

/** Hall, date and time together - the summary shown once a shift is picked. */
export function shiftSummaryLabel(shift) {
  return `${shift.hall}, ${friendlyDate(shiftStartDate(shift))}, ${shiftTimeLabel(shift)}`
}

/** Every hall that has at least one shift, alphabetical. */
export function hallsFromShifts(shifts) {
  return [...new Set(shifts.map((shift) => shift.hall))].sort((a, b) => a.localeCompare(b))
}

/** Every date one hall has a shift starting on, chronological. */
export function datesForHall(shifts, hall) {
  const dates = new Set(
    shifts.filter((shift) => shift.hall === hall).map(shiftStartDate),
  )
  // ISO 'YYYY-MM-DD' strings sort chronologically as plain strings.
  return [...dates].sort()
}

/** Every shift at one hall starting on one date, ordered by start, end, id. */
export function shiftsForHallAndDate(shifts, hall, date) {
  return shifts
    .filter((shift) => shift.hall === hall && shiftStartDate(shift) === date)
    .sort((a, b) => {
      if (a.start_datetime !== b.start_datetime) {
        return a.start_datetime < b.start_datetime ? -1 : 1
      }
      if (a.end_datetime !== b.end_datetime) {
        return a.end_datetime < b.end_datetime ? -1 : 1
      }
      return a.id - b.id
    })
}
