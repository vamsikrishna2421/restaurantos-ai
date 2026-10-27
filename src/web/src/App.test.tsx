import { render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import App from './App'

afterEach(() => vi.unstubAllGlobals())

test('renders brand and backend status', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'ok', ai_mode: 'replay' }) }))
  render(<App />)
  expect(screen.getByRole('heading', { name: /RestaurantOS/ })).toBeInTheDocument()
  expect(await screen.findByText('Backend: ok (replay)')).toBeInTheDocument()
})
