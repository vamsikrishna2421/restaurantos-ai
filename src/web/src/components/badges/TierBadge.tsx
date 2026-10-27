/**
 * TierBadge (CONTRACTS §6–§7, ARCHITECTURE §6.5): who must authorize a GM step. T1 runs automatically under policy
 * (with Undo); T2 waits for a manager or judge; T3 for the owner, judge or admin; FORBIDDEN can never run.
 */

import { Badge } from '../ui/Badge'
import { cx, type Tone } from '../ui/cx'
import type { Tier } from '../../contracts/generated'

export const TIER_INFO: Readonly<Record<Tier, { tone: Tone; text: string; meaning: string }>> = {
  T0: { tone: 'neutral', text: 'T0', meaning: 'No authorization needed: it changes nothing' },
  T1: { tone: 'ok', text: 'T1 · auto', meaning: 'Runs automatically under policy, with Undo' },
  T2: { tone: 'warn', text: 'T2 · manager', meaning: 'Waits for a manager (or judge) to approve' },
  T3: { tone: 'bad', text: 'T3 · owner', meaning: 'Waits for the owner (or judge) to approve' },
  FORBIDDEN: { tone: 'bad', text: 'Forbidden', meaning: 'Never allowed: there is no tool for it' },
}

export interface TierBadgeProps {
  tier: Tier
  className?: string
}

export function TierBadge({ tier, className }: TierBadgeProps) {
  const info = TIER_INFO[tier] ?? TIER_INFO.FORBIDDEN
  return (
    <Badge
      tone={info.tone}
      data-tier={tier}
      title={info.meaning}
      className={cx(tier === 'FORBIDDEN' && 'line-through', className)}
    >
      {info.text}
    </Badge>
  )
}
