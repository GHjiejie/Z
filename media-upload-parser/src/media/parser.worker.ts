import mediaInfoFactory, { type MediaInfo } from "mediainfo.js";
import wasmUrl from "mediainfo.js/MediaInfoModule.wasm?url";
import { normalizeMediaInfo } from "./normalize";
import type { ParseRequest, ParseResponse } from "./types";

let instance: MediaInfo<"object"> | undefined;

async function parseFile(file: File) {
  if (!instance) {
    try {
      instance = await mediaInfoFactory({ format: "object", locateFile: () => wasmUrl });
    } catch {
      throw new Error("媒体解析器加载失败，请刷新页面或重试。");
    }
  }
  const result = await instance.analyzeData(file.size, async (chunkSize, offset) => {
    const buffer = await file.slice(offset, offset + chunkSize).arrayBuffer();
    return new Uint8Array(buffer);
  });
  return normalizeMediaInfo(result);
}

// The main-thread client serializes requests. A cancelled worker is terminated,
// so a WASM instance never analyzes two files at the same time.
self.onmessage = async (event: MessageEvent<ParseRequest>) => {
  const { id, file } = event.data;
  let response: ParseResponse;
  try {
    response = { id, ok: true, metadata: await parseFile(file) };
  } catch (error) {
    instance?.close();
    instance = undefined;
    response = {
      id,
      ok: false,
      error: error instanceof Error ? error.message : "媒体解析失败，请重试。",
    };
  }
  self.postMessage(response);
};
