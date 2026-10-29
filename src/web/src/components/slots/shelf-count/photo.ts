/**
 * The shelf photo, made small in the browser before it leaves the device (P38; ARCHITECTURE_VOICE_MEDIA §10 step 1).
 *
 * The camera's file is decoded as an `<img>` (which applies its EXIF orientation), drawn into a canvas at most
 * `MAX_SIDE` (1280) px on its long side and re-encoded as JPEG at `QUALITY` (0.85). A canvas export carries no EXIF,
 * so the location and camera details never leave the phone; the server strips them again anyway. The returned size is
 * the photo's own pixels, the coordinate space of SAM 3.1's outlines.
 */

export const MAX_SIDE = 1280
export const QUALITY = 0.85

export interface ShrunkPhoto {
  blob: Blob
  width: number
  height: number
}

/** The size that fits within `maxSide` on the long side, keeping the aspect ratio (never enlarged). */
export function fitWithin(width: number, height: number, maxSide = MAX_SIDE): { width: number; height: number } {
  const scale = Math.min(1, maxSide / Math.max(width, height, 1))
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) }
}

async function loadImage(file: Blob): Promise<{ image: HTMLImageElement; release: () => void }> {
  const url = URL.createObjectURL(file)
  const image = new Image()
  image.src = url
  try {
    await image.decode()
  } catch (err) {
    URL.revokeObjectURL(url)
    throw err
  }
  return { image, release: () => URL.revokeObjectURL(url) }
}

/** The photo at most `maxSide` px on its long side, as a fresh JPEG without metadata. */
export async function shrinkPhoto(file: Blob, maxSide = MAX_SIDE, quality = QUALITY): Promise<ShrunkPhoto> {
  const { image, release } = await loadImage(file)
  try {
    const size = fitWithin(image.naturalWidth, image.naturalHeight, maxSide)
    const canvas = document.createElement('canvas')
    canvas.width = size.width
    canvas.height = size.height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('This browser cannot draw the photo')
    ctx.drawImage(image, 0, 0, size.width, size.height)
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality))
    if (!blob) throw new Error('This browser cannot encode the photo')
    return { blob, ...size }
  } finally {
    release()
  }
}
