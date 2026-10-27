/**
 * Optional UI slots (CONTRACTS_API §8): `<Slot name="kitchen-ptt" props={…}/>` lazy-loads
 * `src/components/slots/<name>/index.tsx` (default export) when that folder exists and renders nothing otherwise, so
 * a host surface never depends on an optional package. A slot that fails to load or crashes also renders nothing.
 *
 * Slots: `kitchen-ptt` (P37, KDS header), `shelf-count` (P38, GM inventory panel), `recorded-run` (P35, action page
 * and proof).
 */

import { Suspense, lazy, type ComponentType, type LazyExoticComponent } from 'react'
import { ErrorBoundary } from './router'

export type SlotName = 'kitchen-ptt' | 'shelf-count' | 'recorded-run'

export type SlotProps = Record<string, unknown>

export interface SlotModule {
  default: ComponentType<SlotProps>
}

export type SlotLoaders = Record<string, () => Promise<SlotModule>>

export const SLOT_LOADERS: SlotLoaders = import.meta.glob<SlotModule>('./components/slots/*/index.tsx')

const cache = new WeakMap<object, Map<string, LazyExoticComponent<ComponentType<SlotProps>>>>()

function slotComponent(loaders: SlotLoaders, name: SlotName) {
  const loader = loaders[`./components/slots/${name}/index.tsx`]
  if (!loader) return null
  let byName = cache.get(loaders)
  if (!byName) cache.set(loaders, (byName = new Map()))
  let comp = byName.get(name)
  if (!comp) byName.set(name, (comp = lazy(loader)))
  return comp
}

/** True when the slot's package is present in this build. */
export function hasSlot(name: SlotName, loaders: SlotLoaders = SLOT_LOADERS): boolean {
  return `./components/slots/${name}/index.tsx` in loaders
}

export interface SlotHostProps {
  name: SlotName
  props?: SlotProps
  /** Slot modules by `./components/slots/<name>/index.tsx` (tests); defaults to every folder in the build. */
  loaders?: SlotLoaders
}

export function Slot({ name, props, loaders = SLOT_LOADERS }: SlotHostProps) {
  const Comp = slotComponent(loaders, name)
  if (Comp === null) return null
  return (
    <ErrorBoundary fallback={null}>
      <Suspense fallback={null}>
        <Comp {...(props ?? {})} />
      </Suspense>
    </ErrorBoundary>
  )
}
