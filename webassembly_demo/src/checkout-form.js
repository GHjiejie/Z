import checkoutUrl from '../build/checkout.wasm?url&no-inline';
import { formatCents, quote } from './checkout.js';

const form = document.querySelector('#checkout-form');
const status = document.querySelector('#checkout-status');
const receipt = document.querySelector('#receipt');
let payableCents;

async function calculate(event) {
  event?.preventDefault();
  const controls = form.querySelector('fieldset');
  controls.disabled = true;
  status.textContent = '正在计算…';
  try {
    if (!payableCents) {
      const { instance } = await WebAssembly.instantiateStreaming(fetch(checkoutUrl));
      payableCents = instance.exports.payableCents;
    }
    const result = quote(payableCents, form.elements.price.value, form.elements.quantity.value);
    for (const [name, cents] of Object.entries(result)) {
      receipt.querySelector(`[data-amount="${name}"]`).textContent = formatCents(cents);
    }
    status.textContent = '';
    receipt.hidden = false;
  } catch (error) {
    receipt.hidden = true;
    status.textContent = payableCents ? error.message : '折扣模块加载失败，请重新计算。';
  } finally {
    controls.disabled = false;
  }
}

form.addEventListener('input', () => {
  receipt.hidden = true;
  status.textContent = '点击计算以更新金额。';
});
form.addEventListener('submit', calculate);
calculate();
