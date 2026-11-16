// Shrink a phone photo before upload: longest side 1280px, JPEG quality 0.7 (~150–300 KB
// instead of 3–5 MB). Atlas M0 holds 512 MB in total, uploads included, so full-size
// check-in photos would fill it within a few dozen job cards.
export async function compressImage(file, maxSide = 1280, quality = 0.7) {
  if (!file.type.startsWith('image/')) return file;
  const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bmp.width * scale);
  canvas.height = Math.round(bmp.height * scale);
  canvas.getContext('2d').drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close?.();
  const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', quality));
  if (!blob) return file;
  return new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' });
}
