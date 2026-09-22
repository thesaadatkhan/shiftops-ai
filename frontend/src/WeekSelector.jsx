// The one shared reporting-week control (Phase 7 increment 4). Owned by
// App.jsx and passed down to both Employees and Schedule, so navigating
// between them never resets the selected week. Changing the week here is
// pure client-side state - it never calls the backend itself; the screens
// that read `weekStart` are what fetch data for it.

import AppIcon from './AppIcon.jsx'
import { friendlyDate } from './shiftPicker.js'
import { mondayOf, nextWeek, previousWeek, weekEnd } from './weeks.js'

export default function WeekSelector({ weekStart, onChange }) {
  return (
    <div className="week-selector">
      <button type="button" onClick={() => onChange(previousWeek(weekStart))}>
        <AppIcon name="chevron-left" />
        Previous week
      </button>
      <span className="week-selector-range">
        {friendlyDate(weekStart)} – {friendlyDate(weekEnd(weekStart))}
      </span>
      <button type="button" onClick={() => onChange(nextWeek(weekStart))}>
        Next week
        <AppIcon name="chevron-right" />
      </button>
      <label className="week-selector-jump">
        Jump to week containing
        <input
          type="date"
          value={weekStart}
          onChange={(event) => {
            if (event.target.value) {
              onChange(mondayOf(event.target.value))
            }
          }}
        />
      </label>
    </div>
  )
}
