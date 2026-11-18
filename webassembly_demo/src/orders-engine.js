import { AsyncDuckDB, VoidLogger } from '@duckdb/duckdb-wasm';
import workerUrl from '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url';
import wasmUrl from '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url';
import query from './orders.sql?raw';

export async function summarizeOrders(bytes) {
  const worker = new Worker(workerUrl);
  const db = new AsyncDuckDB(new VoidLogger(), worker);
  try {
    try {
      await db.instantiate(wasmUrl);
    } catch (cause) {
      throw new Error('数据库模块加载失败，请在现代浏览器中重新运行。', { cause });
    }
    await db.registerFileBuffer('orders.csv', bytes);
    const connection = await db.connect();
    try {
      const table = await connection.query(query).catch((cause) => {
        const message = String(cause.message ?? cause);
        const detail = message.match(/城市不能为空|金额须为[^\n]+/u);
        throw new Error(detail ? `${detail[0]}。` : '请检查 CSV 的 UTF-8 编码、city 和 amount 表头以及各行的列数。', { cause });
      });
      return table.toArray().map(({ city, orders, revenue }) => ({ city, orders, revenue }));
    } finally {
      await connection.close();
    }
  } finally {
    await db.terminate();
  }
}
