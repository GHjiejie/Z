import sampleCsv from '../examples/orders.csv?raw';
import sampleUrl from '../examples/orders.csv?url&no-inline';

const form = document.querySelector('#orders-form');
const status = document.querySelector('#orders-status');
const table = document.querySelector('#orders-table');
const body = table.querySelector('tbody');
const restore = document.querySelector('#restore-orders');
document.querySelector('#sample-csv').href = sampleUrl;

function selectSource() {
  const file = form.elements.csv.files[0];
  table.hidden = true;
  restore.hidden = !file;
  status.textContent = file ? '点击汇总以查询当前文件。' : '已选用内置订单，点击汇总即可查看结果。';
}

form.elements.csv.addEventListener('change', selectSource);
restore.addEventListener('click', () => {
  form.elements.csv.value = '';
  selectSource();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const controls = form.querySelector('fieldset');
  const file = form.elements.csv.files[0];
  controls.disabled = true;
  table.hidden = true;
  body.replaceChildren();
  status.textContent = '正在加载本地查询引擎并汇总订单…';
  try {
    if (file?.size === 0) throw new Error('文件为空。');
    const { summarizeOrders } = await import('./orders-engine.js').catch((cause) => {
      throw new Error('查询引擎加载失败，请刷新页面后重试。', { cause });
    });
    const bytes = file ? new Uint8Array(await file.arrayBuffer()) : new TextEncoder().encode(sampleCsv);
    const rows = await summarizeOrders(bytes);
    for (const { city, orders, revenue } of rows) {
      const row = body.insertRow();
      for (const value of [city, orders, revenue]) row.insertCell().textContent = String(value);
    }
    table.hidden = rows.length === 0;
    status.textContent = rows.length
      ? `${file?.name ?? '示例订单'} · 共 ${rows.length} 个城市，按销售额降序排列。`
      : '文件只有表头，还没有订单。';
  } catch (error) {
    status.textContent = `无法汇总。${error.message}`;
  } finally {
    controls.disabled = false;
  }
});
