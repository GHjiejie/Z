import encode from '@jsquash/webp/encode.js';

self.onmessage = async ({ data: { source, quality } }) => {
  let bitmap;
  try {
    bitmap = await createImageBitmap(source);
    const { width, height } = bitmap;
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext('2d');
    context.drawImage(bitmap, 0, 0);
    const pixels = context.getImageData(0, 0, width, height);
    const bytes = await encode(pixels, { quality });
    self.postMessage({ bytes, width, height }, [bytes]);
  } catch {
    const error = bitmap
      ? 'WebP 编码失败，请重试或选择尺寸较小的图片。'
      : '无法读取图片，请选择有效的静态 PNG、JPEG 或 WebP 文件。';
    self.postMessage({ error });
  } finally {
    bitmap?.close();
  }
};
