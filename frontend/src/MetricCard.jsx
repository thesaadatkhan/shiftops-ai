// One small labeled figure, shared by Dashboard.jsx and
// WorkforcePlanning.jsx so the two screens' metric cards stay visually and
// structurally identical rather than each keeping its own copy.

export default function MetricCard({ label, value, note }) {
  return (
    <div className="metric-card">
      <p className="metric-label">{label}</p>
      <p className="metric-value">{value}</p>
      {note && <p className="metric-note">{note}</p>}
    </div>
  )
}
