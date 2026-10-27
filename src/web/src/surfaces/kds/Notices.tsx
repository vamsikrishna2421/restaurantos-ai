/**
 * Staff notices (ARCHITECTURE §6.5 and §9; CONTRACTS §7 `notify_staff`). Each shows the code-rendered action line
 * in the cook's language, with any model-written note beneath it, labelled "AI-drafted" and shown only once it is
 * untainted or approved. Rendering a notice acks `delivered` once; Seen acks `seen`. The GM verifies its step on
 * both.
 */

import { Check, CheckCheck, Eye, Megaphone, Sparkles } from 'lucide-react'
import { useEffect } from 'react'
import { Button, Card, cx } from '../../components/ui'
import type { MenuSnapshot, SpeakLang, StaffNotice } from '../../contracts/generated'
import { kitchenLabel, kitchenLang, stationName } from '../../lib/i18n'
import { draftShown } from './model'

export interface NoticesPanelProps {
  notices: readonly StaffNotice[]
  menu: MenuSnapshot
  /** The station's display language (the panel title); null on the All tab. */
  lang: SpeakLang | null
  busy: ReadonlySet<string>
  onRendered: (notice: StaffNotice) => void
  onSeen: (notice: StaffNotice) => void
}

export function NoticesPanel({ notices, menu, lang, busy, onRendered, onSeen }: NoticesPanelProps) {
  if (notices.length === 0) return null
  const title = (
    <span className="inline-flex items-center gap-2" lang={lang ?? 'en'}>
      <Megaphone aria-hidden className="size-5 text-accent" />
      {kitchenLabel(menu, 'notice', lang ?? 'en')}
    </span>
  )
  return (
    <Card title={title} panel="notices" tone="accent" bodyClassName="space-y-3 p-3">
      {notices.map((n) => (
        <NoticeCard key={n.id} notice={n} menu={menu} english={lang === 'en'} busy={busy.has(n.id)}
          onRendered={onRendered} onSeen={onSeen} />
      ))}
    </Card>
  )
}

interface NoticeCardProps {
  notice: StaffNotice
  menu: MenuSnapshot
  /** The station reads English: show the English text. Otherwise the cook's own text, glossed in English. */
  english: boolean
  busy: boolean
  onRendered: (notice: StaffNotice) => void
  onSeen: (notice: StaffNotice) => void
}

function NoticeCard({ notice, menu, english, busy, onRendered, onSeen }: NoticeCardProps) {
  const { status } = notice
  useEffect(() => {
    if (status === 'sent') onRendered(notice)
  }, [notice, status, onRendered])

  const own = kitchenLang(notice.language)
  const inEnglish = english || own === 'en'
  const lineLang = inEnglish ? 'en' : own
  const draft = draftShown(notice) ? (inEnglish ? notice.ai_draft_en || notice.ai_draft : notice.ai_draft) : null
  const open = status === 'sent' || status === 'delivered'
  const where = notice.station_id ? stationName(menu, notice.station_id, 'en') : 'every station'
  return (
    <article
      aria-label={`Notice for ${where}`}
      data-notice={notice.id}
      data-status={status}
      className={cx('rounded-xl border p-3', open ? 'border-accent/60 bg-accent/10' : 'border-line bg-panel-2')}
    >
      <p lang={lineLang} className="text-xl font-bold leading-snug">{inEnglish ? notice.message_en : notice.message}</p>
      {!inEnglish && <p lang="en" className="mt-1 text-sm text-muted">{notice.message_en}</p>}
      {draft && (
        <div className="mt-3 rounded-lg border-l-4 border-ai bg-ai/10 px-3 py-2">
          <p className="inline-flex items-center gap-1 text-xs font-bold uppercase tracking-wider text-ai">
            <Sparkles aria-hidden className="size-3.5" />
            AI-drafted
          </p>
          <p lang={lineLang} className="mt-1 text-lg leading-snug">{draft}</p>
          {!inEnglish && notice.ai_draft_en && (
            <p lang="en" className="mt-1 text-sm text-muted">{notice.ai_draft_en}</p>
          )}
        </div>
      )}
      {notice.ai_draft_status === 'pending_approval' && (
        <p className="mt-2 text-xs font-semibold text-ai">An AI-drafted note is waiting for a manager's approval.</p>
      )}
      <footer className="mt-3 flex items-center justify-between gap-3">
        <span className={cx('inline-flex items-center gap-1 text-sm font-semibold',
          status === 'sent' ? 'text-muted' : 'text-ok')}>
          {status === 'seen' && <CheckCheck aria-hidden className="size-4" />}
          {status === 'delivered' && <Check aria-hidden className="size-4" />}
          {status === 'seen' ? 'Seen' : status === 'delivered' ? 'Delivered' : 'Sending…'}
        </span>
        {open && (
          <Button variant="primary" size="lg" icon={<Eye />} loading={busy} lang={lineLang}
            onClick={() => onSeen(notice)}>
            {kitchenLabel(menu, 'seen', lineLang)}
          </Button>
        )}
      </footer>
    </article>
  )
}
