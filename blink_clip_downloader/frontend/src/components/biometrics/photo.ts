// The server shrinks a photo to this longest side before looking for faces
// (vision/faces.py's _MAX_DETECTION_SIDE), so sending more is wasted upload
// — and a modern phone's 48-megapixel photo, base64-encoded, is past the
// server's 10 MB request limit.
const MAX_SIDE = 1280

function readAsDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(reader.error ?? new Error('Could not read the file'))
    reader.readAsDataURL(file)
  })
}

/**
 * *file* as a data URL, shrunk to at most MAX_SIDE pixels on its longest
 * side. createImageBitmap applies the photo's EXIF rotation as it decodes,
 * so what is sent is upright. Anything the browser cannot decode (an
 * iPhone's HEIC outside Safari) is sent as-is, for the server to explain.
 */
export async function photoDataUrl(file: File): Promise<string> {
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    return readAsDataUrl(file)
  }
  try {
    const scale = MAX_SIDE / Math.max(bitmap.width, bitmap.height)
    if (scale >= 1) return await readAsDataUrl(file)
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(bitmap.width * scale)
    canvas.height = Math.round(bitmap.height * scale)
    const context = canvas.getContext('2d')
    if (!context) return await readAsDataUrl(file)
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    return canvas.toDataURL('image/jpeg', 0.92)
  } finally {
    bitmap.close()
  }
}
