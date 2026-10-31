/**
 * Customer memory on this device (P43): the kiosk's identify (a guest's name and number, given at
 * checkout, links the conversation to their history), the counter's callout names, and the manager's
 * customers + goodwill reads and decisions.
 *
 * The number itself only ever travels one way — from the guest's own typing to the server's book. Every
 * answer below carries names, dish codes and masked numbers (`···0123`), never the number.
 */

import type {
  ConsoleCustomers,
  CustomerOrderName,
  GoodwillFeed,
  IdentifyResult,
  WinbackDraftResult,
  WinbackOutreachView,
  WinbackSendResult,
  WinbackView,
} from '../contracts/generated'
import { api } from './api'

const bearer = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

export const customersApi = {
  /** Link this dialogue session to a guest's number; answers with their usual and any credit waiting. */
  identify(phone: string, name: string | null, sessionId: string): Promise<IdentifyResult> {
    return api<IdentifyResult>('POST', '/api/customers/identify', { phone, name, session_id: sessionId })
  },
  /** The callout names of this tenant's open orders (orders whose guest identified only). */
  names(): Promise<CustomerOrderName[]> {
    return api<CustomerOrderName[]>('GET', '/api/customers/names')
  },
  /** The Customers section: the regulars, the drifted, and the credits waiting. */
  customers(): Promise<ConsoleCustomers> {
    return api<ConsoleCustomers>('GET', '/api/manager/customers')
  },
  /** The goodwill decisions standing in the attention feed right now. */
  goodwill(): Promise<GoodwillFeed> {
    return api<GoodwillFeed>('GET', '/api/manager/goodwill')
  },
  /** The manager on shift answers a suggestion; approving mints the credit against the number. */
  decideGoodwill(token: string, key: string, approve: boolean, itemCode?: string): Promise<unknown> {
    return api('POST', '/api/manager/goodwill/decide',
      { key, approve, item_code: itemCode ?? null }, bearer(token))
  },
  /** The Win back view (P45): contactable lapsed regulars, the not-contactable count, and every
   * draft and send on record — simulated sends labelled, returned guests marked. */
  winback(): Promise<WinbackView> {
    return api<WinbackView>('GET', '/api/manager/winback')
  },
  /** Draft one win-back message per selected guest (their case keys). Drafting sends nothing. */
  draftWinback(token: string, keys: string[]): Promise<WinbackDraftResult> {
    return api<WinbackDraftResult>('POST', '/api/manager/winback/draft', { keys }, bearer(token))
  },
  /** Save the manager's edit of one draft's text. Sent messages are records and refuse edits. */
  editWinback(token: string, outreachId: string, messageText: string): Promise<WinbackOutreachView> {
    return api<WinbackOutreachView>('POST', '/api/manager/winback/edit',
      { outreach_id: outreachId, message_text: messageText }, bearer(token))
  },
  /** Approve + send drafts: the sender is the approver of record; each guest's opt-in is
   * re-checked server-side at this moment, and the promised usual is minted as a credit. */
  sendWinback(token: string, outreachIds: string[]): Promise<WinbackSendResult> {
    return api<WinbackSendResult>('POST', '/api/manager/winback/send',
      { outreach_ids: outreachIds }, bearer(token))
  },
}
