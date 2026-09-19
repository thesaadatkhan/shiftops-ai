import { useEffect, useState } from 'react'

const HEALTH_URL = 'http://127.0.0.1:8000/api/health'

function BackendStatus() {
  const [status, setStatus] = useState('loading')
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false

    async function checkBackend() {
      try {
        const response = await fetch(HEALTH_URL)

        if (!response.ok) {
          throw new Error(`Backend responded with status ${response.status}`)
        }

        const data = await response.json()

        if (!cancelled) {
          setStatus('success')
          setMessage(data.status)
        }
      } catch {
        if (!cancelled) {
          setStatus('error')
          setMessage(
            'Backend health check failed. The backend may be offline or ' +
              'returned an unexpected response.',
          )
        }
      }
    }

    checkBackend()

    return () => {
      cancelled = true
    }
  }, [])

  if (status === 'loading') {
    return (
      <p className="backend-status backend-status-loading">
        Checking backend connection...
      </p>
    )
  }

  if (status === 'error') {
    return <p className="backend-status backend-status-error">{message}</p>
  }

  return (
    <p className="backend-status backend-status-success">
      Backend connected (status: {message})
    </p>
  )
}

export default BackendStatus
