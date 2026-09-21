// Shared by the Coverage screen: where the shift and Coverage APIs live. The
// friendly shift-picker formatting/filtering itself lives in shiftPicker.js,
// shared with Employee Details' preference picker. Kept separate from
// employees.js because these describe shifts, not workers.

export const SHIFTS_URL = 'http://127.0.0.1:8000/api/shifts'

export function coverageUrl(shiftId, excludeEmployeeCode) {
  const base = `${SHIFTS_URL}/${encodeURIComponent(shiftId)}/coverage`
  if (!excludeEmployeeCode) {
    return base
  }
  return `${base}?exclude_employee_code=${encodeURIComponent(excludeEmployeeCode)}`
}

export const PREFERENCE_LABELS = {
  preferred: 'Preferred',
  low: 'Low preference',
  neutral: 'Neutral',
}

export function preferenceLabel(preference) {
  return PREFERENCE_LABELS[preference] ?? preference
}
