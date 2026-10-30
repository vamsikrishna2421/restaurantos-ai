// @vitest-environment jsdom
// The front door (P41): a brand splash with one Enter button, then the three ways in — customer, staff, and
// setting up a new restaurant. No other surface is reachable before the choice.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test } from 'vitest'
import WelcomeSurface from '.'

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  cleanup()
})

test('splash_first_then_the_three_ways_in', () => {
  render(<WelcomeSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByRole('heading', { name: 'RestaurantOS AI' })).toBeInTheDocument()
  expect(screen.getByText(/operating system for your restaurant/)).toBeInTheDocument()
  // the modes wait behind Enter
  expect(screen.queryByRole('button', { name: /Customer/ })).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
  expect(screen.getByRole('button', { name: /Customer/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Staff/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Set up a new restaurant/ })).toBeInTheDocument()
})

test('each_mode_goes_to_its_door', () => {
  const { unmount } = render(<WelcomeSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
  fireEvent.click(screen.getByRole('button', { name: /Customer/ }))
  expect(window.location.pathname).toBe('/kiosk')
  unmount()

  window.history.replaceState(null, '', '/')
  render(<WelcomeSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
  fireEvent.click(screen.getByRole('button', { name: /Staff/ }))
  expect(window.location.pathname).toBe('/staff')
})

test('setup_goes_to_onboarding', () => {
  render(<WelcomeSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
  fireEvent.click(screen.getByRole('button', { name: /Set up a new restaurant/ }))
  expect(window.location.pathname).toBe('/onboard')
})
