import { readFile } from 'node:fs/promises';
import { formatCents, quote } from '../src/checkout.js';

try {
  const args = process.argv.slice(2);
  if (args.length !== 2) throw new Error('用法：npm run checkout -- <单价> <件数>');

  const bytes = await readFile(new URL('../build/checkout.wasm', import.meta.url));
  const { instance } = await WebAssembly.instantiate(bytes);
  const result = quote(instance.exports.payableCents, ...args);
  console.log(`商品小计 ${formatCents(result.subtotal)}`);
  console.log(`已优惠   ${formatCents(result.discount)}`);
  console.log(`应付金额 ${formatCents(result.payable)}`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
