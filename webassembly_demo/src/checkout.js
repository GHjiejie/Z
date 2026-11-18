export function quote(payableCents, priceText, quantityText) {
  if (!/^\d{1,6}(?:\.\d{1,2})?$/.test(priceText)) {
    throw new Error('请输入 0 至 999999.99 元的单价，最多保留两位小数。');
  }
  if (!/^\d{1,4}$/.test(quantityText) || Number(quantityText) < 1) {
    throw new Error('件数须为 1 至 9999 的整数。');
  }

  const [yuan, fraction = ''] = priceText.split('.');
  const unitCents = BigInt(yuan) * 100n + BigInt(fraction.padEnd(2, '0'));
  const quantity = Number(quantityText);
  const subtotal = unitCents * BigInt(quantity);
  const payable = payableCents(unitCents, quantity);
  return { subtotal, discount: subtotal - payable, payable };
}

export function formatCents(cents) {
  const yuan = cents / 100n;
  const fraction = String(cents % 100n).padStart(2, '0');
  return `¥${yuan}.${fraction}`;
}
