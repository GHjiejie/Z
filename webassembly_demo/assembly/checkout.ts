// 金额单位为分。宿主负责校验输入，Wasm 对整笔商品小计打折。
export function payableCents(unitCents: i64, quantity: i32): i64 {
  let percent: i64 = 100;
  if (quantity >= 10) {
    percent = 80;
  } else if (quantity >= 3) {
    percent = 90;
  }

  const subtotal = unitCents * i64(quantity);
  // 对非负金额，除以 100 前加 50 就能把半分向上舍入。
  return (subtotal * percent + 50) / 100;
}
