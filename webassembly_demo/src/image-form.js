import { drawSamplePoster } from './sample-poster.js';

const form = document.querySelector('#image-form');
const poster = document.querySelector('#poster');
const preview = document.querySelector('#image-preview');
const caption = document.querySelector('#image-caption');
const status = document.querySelector('#image-status');
const download = document.querySelector('#download-image');
const restore = document.querySelector('#restore-image');
const qualityLabel = document.querySelector('#quality-value');
let previewUrl;
drawSamplePoster(poster);

function setPreview(blob, description) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  preview.removeAttribute('src');
  previewUrl = blob ? URL.createObjectURL(blob) : undefined;
  if (previewUrl) preview.src = previewUrl;
  preview.hidden = !blob;
  poster.hidden = Boolean(blob);
  caption.textContent = description;
}

function showSource() {
  const file = form.elements.image.files[0];
  restore.hidden = !file;
  download.hidden = true;
  download.removeAttribute('href');
  setPreview(file, file ? `原图 · ${file.name}` : '内置 PNG · 周末市集海报');
  status.textContent = '';
}

function posterBlob() {
  return new Promise((resolve, reject) => {
    poster.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error('示例海报生成失败。'));
    }, 'image/png');
  });
}

async function encodeWebp(source, quality) {
  const worker = new Worker(new URL('./encode.worker.js', import.meta.url), { type: 'module' });
  try {
    return await new Promise((resolve, reject) => {
      worker.onmessage = ({ data }) => {
        if (data.error) reject(new Error(data.error));
        else resolve(data);
      };
      worker.onerror = () => reject(new Error('图像编码器加载失败，请重试。'));
      worker.postMessage({ source, quality });
    });
  } finally {
    worker.terminate();
  }
}

form.elements.image.addEventListener('change', showSource);
restore.addEventListener('click', () => {
  form.elements.image.value = '';
  showSource();
});
preview.addEventListener('error', () => {
  if (!preview.getAttribute('src')) return;
  preview.hidden = true;
  caption.textContent = '无法预览所选图片。';
});
form.elements.quality.addEventListener('input', () => {
  qualityLabel.value = form.elements.quality.value;
  if (!download.hidden) showSource();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = form.elements.image.files[0];
  const controls = form.querySelector('fieldset');
  controls.disabled = true;
  download.hidden = true;
  status.textContent = '正在本机编码…';
  try {
    const source = file ?? await posterBlob();
    const { bytes, width, height } = await encodeWebp(source, Number(form.elements.quality.value));
    const result = new Blob([bytes], { type: 'image/webp' });
    setPreview(result, `WebP 预览 · ${width} × ${height} 像素`);
    download.href = previewUrl;
    download.download = `${file?.name.replace(/\.[^.]+$/, '') || '周末市集'}.webp`;
    download.hidden = false;
    status.textContent = `原图 ${(source.size / 1024).toFixed(1)} KiB → WebP ${(result.size / 1024).toFixed(1)} KiB`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    controls.disabled = false;
  }
});
