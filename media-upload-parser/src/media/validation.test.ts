import { describe, expect, it } from "vitest";
import type { MediaEntry, MediaMetadata, ReadyMediaEntry } from "./types";
import { createUploadFormData, getVideoDurationSeconds, validateAddition } from "./validation";

function metadata(durationSeconds: number, kind: "video" | "audio" = "video"): MediaMetadata {
  return { kind, durationSeconds, container: "MPEG-4", audioTracks: [], videoTracks: [] };
}

function ready(seconds: number, kind: "video" | "audio" = "video"): ReadyMediaEntry {
  return {
    id: crypto.randomUUID(),
    file: new File(["fixture"], `${seconds}.${kind === "video" ? "mp4" : "wav"}`),
    status: "ready",
    metadata: metadata(seconds, kind),
  };
}

describe("video duration budget", () => {
  it("accepts exactly 30 seconds across multiple files", () => {
    expect(validateAddition([ready(20)], metadata(10))).toBeNull();
    expect(getVideoDurationSeconds([ready(20), ready(10)])).toBe(30);
  });

  it("rejects a single video over the limit and a cumulative overflow", () => {
    expect(validateAddition([], metadata(30.001))).toContain("不能超过 30 秒");
    expect(validateAddition([ready(20)], metadata(10.001))).toContain("剩余 10 秒");
  });

  it("does not round away a sub-millisecond excess", () => {
    expect(validateAddition([ready(20)], metadata(10.0000001))).not.toBeNull();
    expect(validateAddition([], metadata(30.000000000000004))).not.toBeNull();
  });

  it("avoids floating-point addition noise at the exact boundary", () => {
    expect(validateAddition([ready(29.9)], metadata(0.1))).toBeNull();
    expect(getVideoDurationSeconds([ready(29.9), ready(0.1)])).toBe(30);
    expect(validateAddition([ready(29.1)], metadata(0.9))).toBeNull();
  });

  it("does not charge audio duration to the video budget", () => {
    expect(validateAddition([ready(30)], metadata(600, "audio"))).toBeNull();
    expect(getVideoDurationSeconds([ready(30), ready(600, "audio")])).toBe(30);
  });

  it("does not charge parsing, failed or rejected files", () => {
    const file = new File(["bad"], "bad.mp4");
    const entries: MediaEntry[] = [
      ready(20),
      { id: "1", file, status: "parsing" },
      { id: "2", file, status: "error", error: "invalid" },
      { id: "3", file, status: "rejected", metadata: metadata(20), error: "overflow" },
    ];
    expect(getVideoDurationSeconds(entries)).toBe(20);
    expect(validateAddition(entries, metadata(10))).toBeNull();
  });

  it("rejects missing, infinite, zero and negative durations", () => {
    for (const value of [NaN, Infinity, 0, -1]) {
      expect(() => validateAddition([], metadata(value))).toThrow("有效的正数");
    }
  });
});

describe("upload boundary", () => {
  it("creates multipart data with only accepted files and aligned metadata", () => {
    const video = ready(20);
    const audio = ready(120, "audio");
    const data = createUploadFormData([
      video,
      audio,
      { ...ready(20), status: "rejected", error: "overflow" },
    ]);
    const files = data.getAll("files");
    expect(files).toHaveLength(2);
    expect(files.every((value) => value instanceof File)).toBe(true);
    expect(JSON.parse(String(data.get("metadata")))).toEqual([
      { id: video.id, fileName: video.file.name, size: video.file.size, ...metadata(20) },
      { id: audio.id, fileName: audio.file.name, size: audio.file.size, ...metadata(120, "audio") },
    ]);
  });

  it("rechecks the limit even if an entry is marked ready", () => {
    expect(() => createUploadFormData([ready(20), ready(20)])).toThrow("不能超过");
  });

  it("blocks exporting while a file is still parsing or no file is accepted", () => {
    expect(() => createUploadFormData([])).toThrow("至少一个");
    expect(() => createUploadFormData([
      ready(10),
      { id: "pending", file: new File(["data"], "pending.mp4"), status: "parsing" },
    ])).toThrow("解析完成");
  });
});
