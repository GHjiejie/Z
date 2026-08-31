import type { Run } from '../types'

export function CancellationNotice({ run }: { run: Run | null }) {
  if (!run || run.status !== 'CANCELLING') return null
  const retrying = run.cancellation?.status === 'PENDING'
  return <div className="policy-callout cancellation-notice" role="status">
    <strong>{retrying ? 'Cancellation cleanup will retry' : 'Cancellation in progress'}</strong>
    <p>Agent execution permission has been revoked. The platform is stopping commands and preserving files and change artifacts. This task is not fully cancelled until cleanup finishes.</p>
    {retrying && <small>Attempt {run.cancellation?.attempts}. Automatic retry is scheduled; do not start a replacement task in this thread yet.</small>}
  </div>
}
