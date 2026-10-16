import { MAX_VIDEO_DURATION_SECONDS } from "./config";
import type { MediaEntry, MediaMetadata, ReadyMediaEntry } from "./types";

// Convert the shortest decimal representation to integer picoseconds. This
// removes summation noise (e.g. 29.9 + 0.1), without rounding to display seconds.
const TICKS_PER_SECOND = 1_000_000_000_000n;

function durationTicks(seconds: number): bigint {
  if (!Number.isFinite(seconds) || seconds <= 0) throw new Error("媒体时长必须为有效的正数。");
  const [coefficient = "", exponent = "0"] = seconds.toString().toLowerCase().split("e");
  const [integer = "0", decimal = ""] = coefficient.split(".");
  const digits = BigInt(integer + decimal);
  const shift = 12 + Number(exponent) - decimal.length;
  if (shift >= 0) return digits * 10n ** BigInt(shift);
  const divisor = 10n ** BigInt(-shift);
  // Round up below one picosecond; never permit a positive excess at the limit.
  return (digits + divisor - 1n) / divisor;
}

export function getReadyEntries(entries: MediaEntry[]): ReadyMediaEntry[] {
  return entries.filter((entry): entry is ReadyMediaEntry => entry.status === "ready");
}

export function getVideoDurationSeconds(entries: MediaEntry[]): number {
  const ticks = getReadyEntries(entries)
    .filter((entry) => entry.metadata.kind === "video")
    .reduce((sum, entry) => sum + durationTicks(entry.metadata.durationSeconds), 0n);
  return Number(ticks) / Number(TICKS_PER_SECOND);
}

export function validateAddition(
  entries: MediaEntry[],
  metadata: MediaMetadata,
  maxSeconds = MAX_VIDEO_DURATION_SECONDS,
): string | null {
  const currentTicks = getReadyEntries(entries)
    .filter((entry) => entry.metadata.kind === "video")
    .reduce((sum, entry) => sum + durationTicks(entry.metadata.durationSeconds), 0n);
  const newTicks = durationTicks(metadata.durationSeconds);
  if (metadata.kind === "audio") return null;
  const limitTicks = durationTicks(maxSeconds);
  if (currentTicks + newTicks <= limitTicks) return null;

  const available = Math.max(0, Number(limitTicks - currentTicks) / Number(TICKS_PER_SECOND));
  return `视频累计时长不能超过 ${maxSeconds} 秒。当前已加入 ${getVideoDurationSeconds(entries)} 秒，剩余 ${available} 秒；本视频 ${metadata.durationSeconds} 秒。`;
}

// Revalidates at the submission boundary, rather than trusting a disabled button.
export function createUploadFormData(entries: MediaEntry[]): FormData {
  if (entries.some((entry) => entry.status === "parsing")) throw new Error("请等待文件解析完成。");
  const ready = getReadyEntries(entries);
  if (ready.length === 0) throw new Error("请先加入至少一个有效的音视频文件。");
  const checked: MediaEntry[] = [];
  for (const entry of ready) {
    const error = validateAddition(checked, entry.metadata);
    if (error) throw new Error(error);
    checked.push(entry);
  }

  const data = new FormData();
  for (const entry of ready) data.append("files", entry.file, entry.file.name);
  data.append("metadata", JSON.stringify(ready.map((entry) => ({
    id: entry.id,
    fileName: entry.file.name,
    size: entry.file.size,
    ...entry.metadata,
  }))));
  return data;
}
