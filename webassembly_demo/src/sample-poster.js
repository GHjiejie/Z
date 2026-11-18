// 生成一张有文字、渐变和细线的素材，便于观察有损编码对不同细节的影响。
export function drawSamplePoster(canvas) {
  const ctx = canvas.getContext('2d');
  const { width, height } = canvas;
  const background = ctx.createLinearGradient(0, 0, width, height);
  background.addColorStop(0, '#123d48');
  background.addColorStop(0.65, '#187a71');
  background.addColorStop(1, '#bed987');
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = '#c6eeb0';
  for (let radius = 32; radius <= 320; radius += 9) {
    ctx.beginPath();
    ctx.arc(width - 90, height + 12, radius, 0, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.fillStyle = '#f6f4d9';
  ctx.font = '600 24px sans-serif';
  ctx.fillText('SATURDAY MARKET', 44, 68);
  ctx.font = 'bold 68px sans-serif';
  ctx.fillText('周末市集', 40, 173);
  ctx.font = '26px sans-serif';
  ctx.fillText('每周六  10:00 — 18:00', 44, 235);
}
