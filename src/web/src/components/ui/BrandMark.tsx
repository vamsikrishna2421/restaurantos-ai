/**
 * BrandMark: the hat logo beside the "RestaurantOS AI" wordmark. `BrandHat` is the hat alone, before a surface title.
 *
 * The hat is `/favicon.png` (the tab icon, so already loaded) and decorative (`alt=""`): the accessible name stays the
 * wordmark or the title beside it. This module imports nothing else from the kit (no `cx`), so the router can use it
 * without pulling tailwind-merge into the entry chunk.
 */

const HAT_SRC = '/favicon.png'

export interface BrandHatProps {
  /** The hat's size in CSS px (default 28). */
  size?: number
  className?: string
}

export function BrandHat({ size = 28, className }: BrandHatProps) {
  // the corner radius is a share of the size, so the mark keeps its app-icon proportions at 28px and at hero size
  return <img src={HAT_SRC} alt="" width={size} height={size} className={join('shrink-0 rounded-[22%]', className)} />
}

export interface BrandMarkProps {
  /** The hat's size in CSS px (default 28). */
  size?: number
  /** Classes for the whole mark, e.g. the wordmark's type size and weight. */
  className?: string
}

export function BrandMark({ size = 28, className }: BrandMarkProps) {
  return (
    <span className={join('inline-flex items-center gap-2', className)}>
      <BrandHat size={size} />
      <span>
        RestaurantOS{' '}
        <span className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-transparent">AI</span>
      </span>
    </span>
  )
}

function join(base: string, extra: string | undefined): string {
  return extra ? `${base} ${extra}` : base
}
