import { PARSE_TIMEOUT_MS } from "./config";
import type { MediaMetadata, ParseRequest, ParseResponse } from "./types";

export class MediaParser {
  private worker: Worker | null = null;
  private pending: Promise<void> = Promise.resolve();

  parse(file: File, signal?: AbortSignal): Promise<MediaMetadata> {
    const task = this.pending.then(() => this.analyze(file, signal));
    this.pending = task.then(() => undefined, () => undefined);
    return task;
  }

  private analyze(file: File, signal?: AbortSignal): Promise<MediaMetadata> {
    if (signal?.aborted) return Promise.reject(new DOMException("解析已取消", "AbortError"));
    if (file.size === 0) return Promise.reject(new Error("文件为空，请选择有效的音视频文件。"));

    return new Promise((resolve, reject) => {
      const worker = this.worker ?? new Worker(new URL("./parser.worker.ts", import.meta.url), { type: "module" });
      this.worker = worker;
      const id = crypto.randomUUID();

      const cleanup = () => {
        window.clearTimeout(timeout);
        signal?.removeEventListener("abort", onAbort);
        worker.removeEventListener("message", onMessage);
        worker.removeEventListener("error", onError);
        worker.removeEventListener("messageerror", onError);
      };
      const fail = (error: Error) => {
        cleanup();
        worker.terminate();
        if (this.worker === worker) this.worker = null;
        reject(error);
      };
      const onAbort = () => fail(new DOMException("解析已取消", "AbortError"));
      const onError = () => fail(new Error("媒体解析器加载失败，请刷新页面后重试。"));
      const onMessage = (event: MessageEvent<ParseResponse>) => {
        if (event.data.id !== id) return;
        cleanup();
        if (event.data.ok) resolve(event.data.metadata);
        else reject(new Error(event.data.error));
      };
      const timeout = window.setTimeout(
        () => fail(new Error("文件解析超时，可能已损坏或格式暂不支持，请重试。")),
        PARSE_TIMEOUT_MS,
      );

      worker.addEventListener("message", onMessage);
      worker.addEventListener("error", onError);
      worker.addEventListener("messageerror", onError);
      signal?.addEventListener("abort", onAbort, { once: true });
      try {
        worker.postMessage({ id, file } satisfies ParseRequest);
      } catch (error) {
        fail(error instanceof Error ? error : new Error("无法读取该文件。"));
      }
    });
  }

  dispose() {
    this.worker?.terminate();
    this.worker = null;
  }
}
