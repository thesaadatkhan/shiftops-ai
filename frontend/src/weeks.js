// Shared reporting-week date math (Phase 7 increment 4). Pure, client-side,
// and read-only - nothing here calls the backend or mutates anything.
//
// Dates are built from explicit y/m/d parts, exactly like shiftPicker.js's
// friendlyDate, never `new Date(isoString)` (which parses as UTC and can
// read back a day early in a negative-UTC-offset timezone). There is no
// timezone conversion anywhere else in this project either (D025's single
// local simulation clock).

export const DEFAULT_WEEK_START = '2026-10-05'

function toParts(isoDate) {
  const [year, month, day] = isoDate.split('-').map(Number)
  return { year, month, day }
}

function toIso(date) {
  const yyyy = date.getFullYear()
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd}`
}

/** 'YYYY-MM-DD' shifted by a signed number of days. */
export function addDays(isoDate, days) {
  const { year, month, day } = toParts(isoDate)
  const date = new Date(year, month - 1, day)
  date.setDate(date.getDate() + days)
  return toIso(date)
}

/** The Monday of the week containing this date (inclusive; today counts). */
export function mondayOf(isoDate) {
  const { year, month, day } = toParts(isoDate)
  const date = new Date(year, month - 1, day)
  // getDay(): 0 = Sunday .. 6 = Saturday. Distance back to Monday.
  const offset = (date.getDay() + 6) % 7
  date.setDate(date.getDate() - offset)
  return toIso(date)
}

export function previousWeek(weekStart) {
  return addDays(weekStart, -7)
}

export function nextWeek(weekStart) {
  return addDays(weekStart, 7)
}

/** The Sunday six days after this Monday. */
export function weekEnd(weekStart) {
  return addDays(weekStart, 6)
}
