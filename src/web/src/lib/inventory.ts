/**
 * The inventory workflow API (pass 13): opening-stock entry, the nightly closing check, the
 * purchase-bill scan and the supplier receipts it files. Pass 16 adds the recipe review: the
 * dishes whose ingredient lists are still inferred drafts, and approving them (with edits). The
 * stock list itself stays the plain `GET /api/inventory` the Settings editor already uses. Every
 * call rides the tenant session; a staff token is not needed here because the server also admits
 * the Inventory Manager's token — the console's session is the manager's.
 */

import type {
  BillConfirmLine,
  BillDraft,
  ClosingCheckView,
  InventorySetupView,
  RecipeDishReview,
  RecipeLineEdit,
  RecipeReviewsView,
  SupplierReceipt,
} from '../contracts/generated'
import { api } from './api'

export const inventoryApi = {
  /** Opening-stock entry: every ingredient, and whether the opening counts were entered. */
  setup(): Promise<InventorySetupView> {
    return api<InventorySetupView>('GET', '/api/inventory/setup')
  },
  /** Save the opening counts (each becomes an auditable `initial` adjustment on the server). */
  saveSetup(counts: Record<string, number>): Promise<InventorySetupView> {
    return api<InventorySetupView>('POST', '/api/inventory/setup', { counts })
  },
  /** Tonight's closing check, evaluated when read: what the forecast says will not last. */
  closingCheck(): Promise<ClosingCheckView> {
    return api<ClosingCheckView>('GET', '/api/inventory/closing-check')
  },
  /** Finish the check: counted quantities that differ are applied; the rest count as matching. */
  completeClosingCheck(counts: Record<string, number>): Promise<ClosingCheckView> {
    return api<ClosingCheckView>('POST', '/api/inventory/closing-check/complete', { counts })
  },
  /** Read one purchase bill (a photo or a text-based PDF) into a draft stock-in. */
  scanBill(file: File): Promise<BillDraft> {
    const form = new FormData()
    form.append('file', file, file.name)
    return api<BillDraft>('POST', '/api/perception/bill', form)
  },
  /** Receive a bill draft's confirmed lines into stock; the bill is filed as a receipt. */
  confirmBill(draftId: string, lines: BillConfirmLine[]): Promise<SupplierReceipt> {
    return api<SupplierReceipt>('POST', `/api/perception/bill/${encodeURIComponent(draftId)}/confirm`, { lines })
  },
  /** The bills already scanned and received, newest first. */
  receipts(): Promise<SupplierReceipt[]> {
    return api<SupplierReceipt[]>('GET', '/api/supplier/receipts')
  },
  /** Pass 16: the dishes whose ingredient lists are still drafts awaiting approval, with lines. */
  proposedRecipes(): Promise<RecipeReviewsView> {
    return api<RecipeReviewsView>('GET', '/api/inventory/recipes/proposed')
  },
  /** Pass 16: approve a dish's list — the edited lines when the reviewer changed them, or none
   *  (approve exactly as proposed). The server records who approved and when. */
  approveRecipe(itemCode: string, lines: RecipeLineEdit[] | null): Promise<RecipeDishReview> {
    return api<RecipeDishReview>('POST', `/api/inventory/recipes/${encodeURIComponent(itemCode)}/approve`,
      lines === null ? {} : { lines })
  },
}
