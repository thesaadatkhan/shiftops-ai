// One dependency-free icon vocabulary for the application shell and shared
// controls. Labels remain visible, so these SVGs stay hidden from assistive
// technology and never replace the text that explains an action.

const PATHS = {
  logo: (
    <>
      <path d="M12 2.75 14 8l5.25 2-5.25 2-2 5.25L10 12l-5.25-2L10 8l2-5.25Z" />
      <path d="m18.25 15 .9 2.35 2.35.9-2.35.9-.9 2.35-.9-2.35-2.35-.9 2.35-.9.9-2.35Z" />
    </>
  ),
  dashboard: <path d="M4 4h6v7H4V4Zm10 0h6v4h-6V4ZM4 15h6v5H4v-5Zm10-3h6v8h-6v-8Z" />,
  employees: (
    <>
      <path d="M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0" />
      <path d="M16 7.5a3 3 0 0 1 0 6M16.5 15.5A5 5 0 0 1 21.5 20" />
    </>
  ),
  schedule: (
    <>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M7 3v4m10-4v4M3 10h18M7 14h3m4 0h3m-10 4h3" />
    </>
  ),
  coverage: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m8 12 2.5 2.5L16.5 8.5" />
    </>
  ),
  planning: <path d="M4 20V10m5 10V4m6 16v-7m5 7V7" />,
  assistant: (
    <>
      <path d="M12 3 13.6 8.4 19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3Z" />
      <path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" />
    </>
  ),
  'chevron-left': <path d="m15 18-6-6 6-6" />,
  'chevron-right': <path d="m9 18 6-6-6-6" />,
}

export default function AppIcon({ name, className = '' }) {
  return (
    <svg
      className={`app-icon ${className}`.trim()}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  )
}
