// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, expect, test, vi } from 'vitest'
import { Badge, Button, Card, Chip, clearToasts, cx, Modal, TabPanel, Tabs, toast, Toaster } from '.'

afterEach(() => {
  cleanup()
  clearToasts()
  vi.useRealTimers()
})

test('Button never submits by accident and shows a busy state', () => {
  const onClick = vi.fn()
  render(
    <form onSubmit={() => { throw new Error('submitted') }}>
      <Button variant="primary" onClick={onClick}>Confirm</Button>
      <Button loading>Saving</Button>
    </form>,
  )
  const confirm = screen.getByRole('button', { name: 'Confirm' })
  expect(confirm).toHaveAttribute('type', 'button')
  expect(confirm).toHaveAttribute('data-variant', 'primary')
  fireEvent.click(confirm)
  expect(onClick).toHaveBeenCalledOnce()
  const saving = screen.getByRole('button', { name: 'Saving' })
  expect(saving).toBeDisabled()
  expect(saving).toHaveAttribute('aria-busy', 'true')
})

test('Card, Badge and Chip', () => {
  const onToggle = vi.fn()
  render(
    <Card title="Paneer" actions={<Badge tone="bad">ACT</Badge>} footer="free 800 g" tone="bad" className="mt-2">
      <Chip tone="ai">deterministic forecast — no LLM</Chip>
      <Chip onClick={onToggle} selected>te</Chip>
    </Card>,
  )
  const card = screen.getByRole('heading', { name: 'Paneer' }).closest('section') as HTMLElement
  expect(card.className).toMatch(/\bborder-l-bad\b/)
  expect(card.className).toMatch(/\bmt-2\b/)
  expect(within(card).getByText('ACT')).toHaveAttribute('data-tone', 'bad')
  expect(within(card).getByText('free 800 g').tagName).toBe('FOOTER')
  expect(screen.getByText('deterministic forecast — no LLM').tagName).toBe('SPAN')
  const toggle = screen.getByRole('button', { name: 'te' })
  expect(toggle).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(toggle)
  expect(onToggle).toHaveBeenCalledOnce()
  expect(cx('px-4 text-muted', 'px-6', false, 'text-accent')).toBe('px-6 text-accent') // the caller's class wins
})

function ModalHarness({ dismissible = true }: { dismissible?: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open</Button>
      <Modal open={open} onClose={() => setOpen(false)} title="Approve plan #2" dismissible={dismissible}
        footer={<Button variant="ok">Approve</Button>}>
        <p>Short code 7F3A-91C2</p>
      </Modal>
    </>
  )
}

test('Modal: focus moves in and back, Escape and the backdrop close it, Tab stays inside', () => {
  render(<ModalHarness />)
  const opener = screen.getByRole('button', { name: 'Open' })
  opener.focus()
  fireEvent.click(opener)
  const dialog = screen.getByRole('dialog', { name: 'Approve plan #2' })
  expect(dialog).toHaveAttribute('aria-modal', 'true')
  expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Close' }))
  expect(document.body.style.overflow).toBe('hidden')

  const approve = within(dialog).getByRole('button', { name: 'Approve' })
  approve.focus()
  fireEvent.keyDown(document, { key: 'Tab' })
  expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Close' }))
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(approve)

  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(opener)
  expect(document.body.style.overflow).toBe('')

  fireEvent.click(opener)
  fireEvent.mouseDown(document.querySelector('[data-modal-backdrop]') as Element)
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('Modal: a step that must be answered ignores Escape and the backdrop', () => {
  render(<ModalHarness dismissible={false} />)
  fireEvent.click(screen.getByRole('button', { name: 'Open' }))
  fireEvent.keyDown(document, { key: 'Escape' })
  fireEvent.mouseDown(document.querySelector('[data-modal-backdrop]') as Element)
  expect(screen.getByRole('dialog')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
})

test('toasts show once, close themselves, and alert on bad news', () => {
  vi.useFakeTimers()
  render(
    <>
      <Toaster />
      <Toaster />
    </>,
  )
  act(() => {
    toast('Saved')
    toast('PO refused: the supplier cutoff passed', { tone: 'bad', durationMs: 0 })
  })
  expect(screen.getAllByText('Saved')).toHaveLength(1) // two Toasters mounted, one renders
  expect(screen.getByRole('alert')).toHaveTextContent('PO refused')
  expect(screen.getByRole('status')).toHaveTextContent('Saved')
  act(() => {
    vi.advanceTimersByTime(4000)
  })
  expect(screen.queryByText('Saved')).toBeNull()
  expect(screen.getByRole('alert')).toBeInTheDocument() // sticky until dismissed
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
  expect(screen.queryByRole('alert')).toBeNull()
})

function TabsHarness() {
  const [station, setStation] = useState('STN_CURRY')
  const items = [
    { id: 'STN_BIRYANI', label: 'Biryani', badge: 1 },
    { id: 'STN_CURRY', label: 'Curry', badge: 5 },
    { id: 'STN_TANDOOR', label: 'తందూర్', lang: 'te' },
    { id: 'STN_BAR', label: 'Bar', disabled: true },
  ]
  return (
    <>
      <Tabs items={items} value={station} onChange={setStation} label="Stations" idPrefix="kds" />
      {items.map((t) => <TabPanel key={t.id} idPrefix="kds" id={t.id} value={station}>{t.id} tickets</TabPanel>)}
    </>
  )
}

test('Tabs: arrow keys move between enabled tabs and select them', () => {
  render(<TabsHarness />)
  const curry = screen.getByRole('tab', { name: /Curry/ })
  expect(screen.getByRole('tablist', { name: 'Stations' })).toBeInTheDocument()
  expect(curry).toHaveAttribute('aria-selected', 'true')
  expect(curry).toHaveAttribute('tabindex', '0')
  expect(screen.getByRole('tabpanel')).toHaveTextContent('STN_CURRY tickets')
  expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', curry.id)

  fireEvent.keyDown(curry, { key: 'ArrowRight' })
  const tandoor = screen.getByRole('tab', { name: 'తందూర్' })
  expect(tandoor).toHaveAttribute('aria-selected', 'true')
  expect(tandoor).toHaveAttribute('lang', 'te')
  expect(document.activeElement).toBe(tandoor)
  fireEvent.keyDown(tandoor, { key: 'ArrowRight' }) // skips the disabled Bar tab, wraps to Biryani
  expect(screen.getByRole('tab', { name: /Biryani/ })).toHaveAttribute('aria-selected', 'true')
  fireEvent.keyDown(document.activeElement as Element, { key: 'End' })
  expect(screen.getByRole('tabpanel')).toHaveTextContent('STN_TANDOOR tickets')
})
